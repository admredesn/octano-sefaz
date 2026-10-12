# -*- coding: utf-8 -*-
"""
CARTÕES — extrato EDI do PagBank na nuvem (11/10/2026, pedido do Ronan: aba Cartões = pré-conferência
das taxas antes da Conciliação).

Baixa o extrato transacional do EDI (a mesma consulta que o núcleo faz no posto, aqui para a REDE,
já que a conta PagBank é única) e grava uma linha por venda em oct_cartao_transacoes, com bruto,
taxa, tarifa, líquido, parcelas, bandeira, maquininha e data prevista de pagamento. O posto de
cada venda vem do nº de série da maquininha (oct_parametros.pagbank_maquininhas de cada empresa);
maquininha não cadastrada fica com empresa_id nulo (a tela avisa).

Credenciais — POR POSTO (12/10/2026): cada posto cadastra o EDI dele em Parâmetros › Recebimentos
(oct_parametros: pagbank_edi_usuario + pagbank_edi_token). O servidor puxa de cada credencial e atribui a
venda ao posto (série da maquininha; ou, em conta própria, o dono da conta). Quando as contas se separarem,
cada posto passa a puxar do lugar certo sem mexer em nada aqui.
Reserva (Railway › Variables, nunca no código): PAGBANK_EDI_CONTAS = "usuario:token;usuario:token"
ou PAGBANK_EDI_USUARIO + PAGBANK_EDI_TOKEN — vale para credencial que não é de nenhum posto.
Desliga com CARTOES_EDI=0. Intervalo: CARTOES_EDI_INTERVALO_MIN (15). Hoje e ontem a cada ciclo;
os últimos 7 dias uma vez por dia (ajustes/cancelamentos chegam depois).

O EDI é intradiário: o dia corrente vem parcial e vai completando ao longo do dia.
"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

from .empresa_cert import _rest_get, _supabase_conf

HOST = os.environ.get("PAGBANK_EDI_HOST", "edi.api.pagbank.com.br").strip()
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_BAND = {"MASTERCARD": "Mastercard", "MASTER": "Mastercard", "VISA": "Visa",
         "ELO": "Elo", "AMEX": "Amex", "HIPERCARD": "Hipercard", "HIPER": "Hipercard"}
_MAPA = {"quando": 0.0, "dados": {}}
_LOCK = threading.Lock()


# ------------------------------------------------------------------ config
def _credenciais_dos_postos():
    """{empresa_id: (usuario, token)} do cadastro de cada posto (oct_parametros). Só entra quem tem os dois."""
    try:
        linhas = _rest_get("oct_parametros", "?chave=in.(pagbank_edi_usuario,pagbank_edi_token)&select=empresa_id,chave,valor")
    except Exception as e:
        print("[cartoes] credenciais dos postos:", str(e)[:120])
        return {}
    por = {}
    for p in linhas or []:
        v = str(p.get("valor") or "").strip().strip('"')
        if p.get("empresa_id") and v:
            por.setdefault(p["empresa_id"], {})[p.get("chave")] = v
    return {e: (d["pagbank_edi_usuario"], d["pagbank_edi_token"]) for e, d in por.items()
            if d.get("pagbank_edi_usuario") and d.get("pagbank_edi_token")}


def _postos_conta_propria():
    """Postos que declararam 'esta conta PagBank é só deste posto' (oct_parametros.pagbank_edi_propria)."""
    try:
        linhas = _rest_get("oct_parametros", "?chave=eq.pagbank_edi_propria&select=empresa_id,valor")
    except Exception:
        return set()
    return {p["empresa_id"] for p in linhas or []
            if p.get("empresa_id") and str(p.get("valor")).strip().strip('"').lower() in ("true", "1", "sim")}


def fontes(empresa_id=None, por_posto=None, propria=None):
    """Credenciais a consultar: [{"usuario", "token", "donos": [empresa_id, ...], "rotulo", "propria"}].
    propria = a credencial é de UM posto que declarou conta própria (as vendas sem série conhecida são dele)."""
    fs = _fontes_base(empresa_id, por_posto)
    propria = _postos_conta_propria() if propria is None else propria
    for f in fs:
        f["propria"] = len(f["donos"]) == 1 and f["donos"][0] in propria
    return fs


def _fontes_base(empresa_id=None, por_posto=None):
    """Credenciais a consultar, sem a marca 'propria' (ver fontes()).
    empresa_id=None -> todas (robô); com empresa_id -> só a do posto (botão da tela), ou a reserva do servidor
    quando o posto não cadastrou a dele. Credencial repetida em vários postos vira UMA fonte com vários donos."""
    por_posto = _credenciais_dos_postos() if por_posto is None else por_posto
    out, idx = [], {}
    def add(par, dono, rotulo):
        if par in idx:
            if dono and dono not in out[idx[par]]["donos"]:
                out[idx[par]]["donos"].append(dono)
            return
        idx[par] = len(out)
        out.append({"usuario": par[0], "token": par[1], "donos": [dono] if dono else [], "rotulo": rotulo})
    if empresa_id:
        if empresa_id in por_posto:
            add(por_posto[empresa_id], empresa_id, "posto " + str(empresa_id)[:8])
            return out
        for par in contas():
            add(par, None, "variável do servidor")
        return out
    for emp, par in sorted(por_posto.items()):
        add(par, emp, "posto " + str(emp)[:8])
    for par in contas():
        add(par, None, "variável do servidor")
    return out


def contas():
    """[(usuario, token)] das variáveis de ambiente (reserva). Vazio = nenhuma."""
    out = []
    txt = (os.environ.get("PAGBANK_EDI_CONTAS") or "").strip()
    for peca in txt.replace("\n", ";").split(";"):
        peca = peca.strip()
        if ":" in peca:
            u, t = peca.split(":", 1)
            if u.strip() and t.strip():
                out.append((u.strip(), t.strip()))
    u, t = (os.environ.get("PAGBANK_EDI_USUARIO") or "").strip(), (os.environ.get("PAGBANK_EDI_TOKEN") or "").strip()
    if u and t and (u, t) not in out:
        out.append((u, t))
    return out


def ligado():
    return os.environ.get("CARTOES_EDI", "1").strip().lower() not in ("0", "false", "nao", "não", "off")


def _intervalo_min():
    try:
        return max(3, int(os.environ.get("CARTOES_EDI_INTERVALO_MIN") or 15))
    except Exception:
        return 15


# ------------------------------------------------------------------ utilidades (puras)
def _num(v):
    try:
        return float(str(v).replace(",", ".")) if v not in (None, "") else 0.0
    except Exception:
        return 0.0


def parsear_maquininhas(txt):
    """'PB3S262372864=pista 1; PB3S265J77196: loja' -> {'PB3S262372864': 'pista 1', ...} (igual ao núcleo)."""
    out = {}
    for peca in str(txt or "").replace("\r", "\n").replace("\n", ";").replace(",", ";").split(";"):
        peca = peca.strip()
        if not peca:
            continue
        serie, apelido = peca, ""
        for sep in ("=", ":"):
            if sep in peca:
                serie, apelido = peca.split(sep, 1)
                break
        serie = serie.strip().upper()
        if serie:
            out[serie] = apelido.strip()
    return out


def forma_bandeira(arranjo):
    """'DEBIT_VISA' -> ('debito', 'Visa'); 'CREDIT_MASTERCARD' -> ('credito', 'Mastercard'); 'PIX' -> ('pix', 'Pix')."""
    a = str(arranjo or "").strip().upper()
    if not a:
        return "", ""
    if "PIX" in a:
        return "pix", "Pix"
    forma = "debito" if a.startswith("DEBIT") else "credito" if a.startswith("CREDIT") else ""
    resto = a.split("_", 1)[1] if "_" in a else ""
    band = _BAND.get(resto, resto.title().replace("_", " ")) if resto else ""
    return forma, band


def dono_unico(fonte, mapa_maquininhas=None):
    """Posto a quem pertencem as vendas SEM série conhecida de uma credencial: só quando o posto DECLAROU conta própria
    (pagbank_edi_propria) e é o único dono dela. Credencial só registrada num posto NÃO basta: com a conta única da rede,
    isso levaria para ele as máquinas de todos os outros."""
    donos = [d for d in ((fonte or {}).get("donos") or []) if d]
    return donos[0] if len(donos) == 1 and (fonte or {}).get("propria") else None


def mapear(reg, mapa_maquininhas=None, dono=None):
    """Registro do EDI -> linha de oct_cartao_transacoes (PURA). None = não é venda confirmada.
    dono = posto da conta quando a série não está cadastrada (ver dono_unico)."""
    bruto = _num(reg.get("valor_total_transacao")) or _num(reg.get("valor_original_transacao"))
    if bruto <= 0:
        return None
    status = str(reg.get("status_pagamento") or "").strip()
    if status and status != "1":
        return None
    forma, band = forma_bandeira(reg.get("arranjo_ur"))
    dia = str(reg.get("data_inicial_transacao") or reg.get("data_venda_ajuste") or "")[:10]
    hora = str(reg.get("hora_inicial_transacao") or "")[:8]
    cod = str(reg.get("codigo_venda") or reg.get("codigo_transacao") or reg.get("tx_id") or reg.get("movimento_api_codigo") or "").strip()
    if not dia or not cod:
        return None
    taxa = _num(reg.get("taxa_intermediacao"))
    tarifa = _num(reg.get("tarifa_intermediacao"))
    liq = _num(reg.get("valor_liquido_transacao"))
    if liq <= 0 and bruto > 0:
        liq = round(bruto - taxa - tarifa, 2)
    serie = str(reg.get("numero_serie_leitor") or "").strip().upper()
    emp = (mapa_maquininhas or {}).get(serie) or dono
    try:
        parcelas = int(str(reg.get("quantidade_parcelas") or "0").strip() or 0) or 1
    except Exception:
        parcelas = 1
    try:
        parcela = int(str(reg.get("parcela") or "1").strip() or 1)
    except Exception:
        parcela = 1
    prevista = str(reg.get("data_prevista_pagamento") or "")[:10] or None
    return {
        "id": "edi-" + cod,
        "adquirente": "pagbank",
        "estabelecimento": str(reg.get("estabelecimento") or "")[:40] or None,
        "empresa_id": emp,
        "maquininha": serie or None,
        "data_venda": dia,
        "hora": hora[:5] or None,
        "recebido_em": f"{dia}T{hora or '00:00:00'}-03:00",
        "forma": forma or None,
        "bandeira": band or None,
        "arranjo": str(reg.get("arranjo_ur") or "")[:40] or None,
        "tipo_cartao": str(reg.get("tipo_cartao") or "")[:20] or None,
        "parcelas": parcelas,
        "parcela": parcela,
        "plano": str(reg.get("plano") or "")[:20] or None,
        "valor_bruto": round(bruto, 2),
        "taxa_valor": round(taxa, 2),
        "tarifa_valor": round(tarifa, 2),
        "valor_liquido": round(liq, 2),
        "taxa_pct": round((taxa + tarifa) / bruto * 100, 4) if bruto > 0 else None,
        "data_prevista_pagamento": prevista,
        "pagamento_prazo": str(reg.get("pagamento_prazo") or "")[:10] or None,
        "nsu": str(reg.get("nsu") or "")[:40] or None,
        "codigo_autorizacao": str(reg.get("codigo_autorizacao") or "")[:40] or None,
        "codigo_transacao": str(reg.get("codigo_transacao") or "")[:64] or None,
        "tid": str(reg.get("tid") or "")[:64] or None,
        "codigo_venda": cod[:64],
        "status_pagamento": status or None,
        "tipo_evento": str(reg.get("tipo_evento") or "")[:20] or None,
        "tipo_transacao": str(reg.get("tipo_transacao") or "")[:20] or None,
        "atualizado_em": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# ------------------------------------------------------------------ EDI
def _get(url, usuario, token):
    basic = base64.b64encode(f"{usuario}:{token}".encode()).decode()
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Basic {basic}")
    req.add_header("User-Agent", _UA)        # sem isto o gateway responde 403
    req.add_header("Accept", "application/json, */*")
    req.add_header("Accept-Encoding", "identity")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            corpo = r.read().decode("utf-8", "ignore")
            try:
                return r.status, json.loads(corpo)
            except ValueError:
                return r.status, corpo
    except urllib.error.HTTPError as e:
        corpo = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(corpo)
        except ValueError:
            return e.code, corpo[:300]
    except Exception as e:
        return 0, {"erro": f"{type(e).__name__}: {e}"}


def buscar_dia(usuario, token, data_iso):
    """Todos os registros brutos do EDI de um dia (todas as páginas). Levanta RuntimeError com o motivo."""
    base = f"https://{HOST}/movement/v3.00/transactional/{data_iso}"
    regs, pagina, total = [], 1, 1
    while pagina <= total and pagina <= 60:
        status, corpo = _get(f"{base}?pageNumber={pagina}&pageSize=1000", usuario, token)
        if status == 401:
            raise RuntimeError("401 credencial recusada (usuario/token do EDI)")
        if status == 403:
            raise RuntimeError("403 acesso negado (User-Agent/limite/ip)")
        if status != 200 or not isinstance(corpo, dict):
            raise RuntimeError(f"HTTP {status}: {str(corpo)[:120]}")
        regs += corpo.get("detalhes") or []
        total = int((corpo.get("pagination") or {}).get("totalPages") or 1)
        pagina += 1
    return regs


# ------------------------------------------------------------------ banco
def _rest(method, path, body=None, prefer=None):
    url, key = _supabase_conf()
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    req = urllib.request.Request(f"{url}/rest/v1/{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            t = r.read().decode("utf-8", "ignore")
            return r.status, (json.loads(t) if t.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:300]


def mapa_maquininhas(forcar=False):
    """{serie: empresa_id} de todas as empresas (oct_parametros.pagbank_maquininhas). Cache 5 min."""
    with _LOCK:
        if not forcar and time.time() - _MAPA["quando"] < 300:
            return _MAPA["dados"]
    dados = {}
    try:
        # pagbank_maquininhas (núcleo: filtra o PDV e desliga o e-mail) + pagbank_maquininhas_cartoes (só esta aba)
        for p in _rest_get("oct_parametros", "?chave=in.(pagbank_maquininhas,pagbank_maquininhas_cartoes)&select=empresa_id,valor"):
            for serie in parsear_maquininhas(p.get("valor")):
                dados[serie] = p.get("empresa_id")
    except Exception as e:
        print("[cartoes] mapa de maquininhas:", str(e)[:120])
        with _LOCK:
            return _MAPA["dados"]
    with _LOCK:
        _MAPA.update(quando=time.time(), dados=dados)
    return dados


def gravar(linhas):
    """Upsert em lotes (só as colunas do EDI: conferido/obs/livro_em do usuário ficam como estão)."""
    n = 0
    for i in range(0, len(linhas), 500):
        lote = linhas[i:i + 500]
        st, corpo = _rest("POST", "oct_cartao_transacoes?on_conflict=id", lote, prefer="resolution=merge-duplicates,return=minimal")
        if st not in (200, 201, 204):
            raise RuntimeError(f"gravar oct_cartao_transacoes: HTTP {st} {str(corpo)[:160]}")
        n += len(lote)
    return n


def sincronizar_dias(dias, fontes_=None, mapa=None):
    """Baixa e grava os dias informados de cada credencial. Uma credencial com erro não derruba as outras.
    Devolve ({dia: {vendas, gravadas, sem_posto, bruto, taxas}}, {rotulo: erro})."""
    fontes_ = fontes_ if fontes_ is not None else fontes()
    mapa = mapa if mapa is not None else mapa_maquininhas()
    out, erros, ok_alguma = {}, {}, False
    for dia in dias:
        vistos, linhas, sem_posto = set(), [], 0
        for f in fontes_:
            if f["rotulo"] in erros:
                continue
            try:
                regs = buscar_dia(f["usuario"], f["token"], dia)
                ok_alguma = True
            except Exception as e:
                erros[f["rotulo"]] = str(e)[:160]
                continue
            dono = dono_unico(f, mapa)
            for reg in regs:
                try:
                    r = mapear(reg, mapa, dono)
                except Exception:
                    r = None
                if not r or r["id"] in vistos:
                    continue
                vistos.add(r["id"])
                linhas.append(r)
                if not r["empresa_id"]:
                    sem_posto += 1
        gravadas = gravar(linhas) if linhas else 0
        out[dia] = {"vendas": len(linhas), "gravadas": gravadas, "sem_posto": sem_posto,
                    "bruto": round(sum(l["valor_bruto"] for l in linhas), 2), "taxas": round(sum(l["taxa_valor"] + l["tarifa_valor"] for l in linhas), 2)}
    if erros and not ok_alguma:
        raise RuntimeError("; ".join(f"{k}: {v}" for k, v in erros.items()))
    return out, erros


# ------------------------------------------------------------------ robô
def _controle():
    try:
        r = _rest_get("oct_cartao_controle", "?adquirente=eq.pagbank&select=*")
        return r[0] if r else None
    except Exception:
        return None


def _controle_gravar(campos):
    campos = dict(campos, adquirente="pagbank", atualizado_em=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
    _rest("POST", "oct_cartao_controle?on_conflict=adquirente", [campos], prefer="resolution=merge-duplicates,return=minimal")


def _claim(intervalo_min):
    """Um só worker consulta por intervalo: PATCH condicionado ao valor anterior de ultima_consulta."""
    c = _controle()
    agora = datetime.utcnow()
    agora_s = agora.strftime("%Y-%m-%dT%H:%M:%SZ")
    if c is None:
        st, _ = _rest("POST", "oct_cartao_controle", [{"adquirente": "pagbank", "ultima_consulta": agora_s}], prefer="return=minimal")
        return st in (200, 201)
    if c.get("bloqueado_ate") and str(c["bloqueado_ate"])[:19] > agora_s[:19]:
        return False
    ultima = c.get("ultima_consulta")
    if ultima:
        try:
            u = datetime.strptime(str(ultima)[:19], "%Y-%m-%dT%H:%M:%S")
            if (agora - u).total_seconds() < intervalo_min * 60:
                return False
        except Exception:
            pass
    cond = f"ultima_consulta=eq.{urllib.request.quote(str(ultima), safe='')}" if ultima else "ultima_consulta=is.null"
    st, corpo = _rest("PATCH", f"oct_cartao_controle?adquirente=eq.pagbank&{cond}", {"ultima_consulta": agora_s}, prefer="return=representation")
    return st in (200, 204) and bool(corpo)


def ciclo():
    """Chamado pelo agendador (dfe_auto). Nunca levanta exceção."""
    try:
        if not ligado():
            return None
        fs = fontes()
        if not fs:
            return None
        if not _claim(_intervalo_min()):
            return None
        hoje = date.today()
        dias = [hoje.isoformat(), (hoje - timedelta(days=1)).isoformat()]
        c = _controle() or {}
        if (c.get("dias") or "")[:10] != hoje.isoformat():         # 1ª rodada do dia: 7 dias (ajustes tardios)
            dias = [(hoje - timedelta(days=i)).isoformat() for i in range(0, 8)]
        res, erros = sincronizar_dias(dias, fs)
        _controle_gravar({"dias": hoje.isoformat() + " " + ",".join(dias),
                          "erro": ("; ".join(f"{k}: {v}" for k, v in erros.items()))[:300] if erros else None, "bloqueado_ate": None})
        print("[cartoes] EDI:", {d: (v["vendas"], v["sem_posto"]) for d, v in res.items()}, "| erros:", erros or "nenhum")
        return res
    except Exception as e:
        msg = str(e)[:200]
        print("[cartoes] ERRO:", msg)
        try:
            bloq = (datetime.utcnow() + timedelta(minutes=60 if msg.startswith(("401", "403")) else 10)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _controle_gravar({"erro": msg, "bloqueado_ate": bloq})
        except Exception:
            pass
        return None


def baixar_periodo(de_iso, ate_iso, empresa_id=None):
    """Botão ⬇ da tela (até 62 dias). Com empresa_id usa o EDI cadastrado por aquele posto (ou a reserva do servidor).
    Devolve {ok, dias, vendas, sem_posto, erros} ou {ok: False, motivo}."""
    fs = fontes(empresa_id)
    if not fs:
        return {"ok": False, "motivo": "EDI deste posto não cadastrado (Parâmetros › Recebimentos › PagBank — EDI) e sem PAGBANK_EDI_CONTAS no servidor"}
    try:
        de, ate = date.fromisoformat(de_iso), date.fromisoformat(ate_iso)
    except Exception:
        return {"ok": False, "motivo": "datas inválidas"}
    if ate < de or (ate - de).days > 62 or ate > date.today():
        return {"ok": False, "motivo": "período inválido (até 62 dias, sem data futura)"}
    dias = [(de + timedelta(days=i)).isoformat() for i in range((ate - de).days + 1)]
    try:
        res, erros = sincronizar_dias(dias, fs, mapa=mapa_maquininhas(forcar=True))
    except Exception as e:
        return {"ok": False, "motivo": str(e)[:200]}
    return {"ok": True, "dias": res, "vendas": sum(v["vendas"] for v in res.values()), "sem_posto": sum(v["sem_posto"] for v in res.values()),
            "fontes": len(fs), "erros": erros}
