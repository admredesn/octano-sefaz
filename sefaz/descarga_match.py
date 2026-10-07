"""
sefaz/descarga_match.py  -  Fase 2: casa a DESCARGA (sonda) com a NF de combustivel.

Roda no servidor SEFAZ logo apos a consulta DistDFe (mesmo claim, 1x/h por empresa).
Para cada empresa:
  1) detecta descargas no historico da sonda (oct_medicoes) - salto grande e rapido;
  2) reconstroi o volume recebido = salto + vendas do periodo (oct_pdv_abastecimentos);
  3) casa com a(s) NF(s) de combustivel (oct_nfe_manifestadas): mesmo combustivel (por ANP),
     volume +-tolerancia, emitida ate N dias antes. Tenta 1 nota; se nao, combo de 2 notas
     (entrega dupla = 2 compartimentos);
  4) grava o par (candidato) em oct_nfe_descarga - modo preview, SEM dar entrada (Fase 3).

Config (env): DESCARGA_DIAS (7), TOL_PCT (1.0), TOL_ABS (150), DIAS_NF (3).
Idempotente: upsert por (empresa_id, tanque_numero, descarga_ini). Nao rebaixa status.
"""

import os
import json
import unicodedata
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from .empresa_cert import carregar_empresa, _rest_get, _supabase_conf

DESCARGA_DIAS = int(os.environ.get("DESCARGA_DIAS", "7"))
TOL_PCT = float(os.environ.get("DESCARGA_TOL_PCT", "1.0"))
TOL_ABS = float(os.environ.get("DESCARGA_TOL_ABS", "150"))
DIAS_NF = int(os.environ.get("DESCARGA_DIAS_NF", "3"))

MIN_DESCARGA = 500.0
JANELA_MIN = 75
QUEDA_FIM = 250.0
NS = {"n": "http://www.portalfiscal.inf.br/nfe"}


# ------------------------------------------------------------------
def _rest(method, path, body=None, prefer=None):
    url, key = _supabase_conf()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, method=method)
    req.add_header("apikey", key); req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            t = r.read().decode("utf-8", "ignore")
            return r.status, (json.loads(t) if t.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:200]
    except Exception as e:
        return 0, str(e)


def _norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return s.lower().strip()


def _fuel_tanque(nome):
    s = _norm(nome).replace("-", "")
    if "gasolina" in s and any(w in s for w in ("aditiv", "adt", "podium", "premium", "grid")):
        return "gasolina_aditivada"
    if "gasolina" in s or "gasol" in s:
        return "gasolina_comum"
    if "etanol" in s or "alcool" in s:
        return "etanol"
    if "diesel" in s and "s10" in s:
        return "diesel_s10"
    if "diesel" in s:
        return "diesel_s500"
    return None


def _fuel_anp(anp, xprod):
    a = str(anp or "")
    if a.startswith("320102"):
        return "gasolina_aditivada" if a.endswith("002") else "gasolina_comum"
    if a.startswith("810101"):
        return "etanol"
    if a.startswith("820101"):
        return "diesel_s10" if (a == "820101034" or "s10" in _norm(xprod).replace("-", "")) else "diesel_s500"
    return None


def _t(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ------------------------------------------------------------------
# 1. detector de descarga
# ------------------------------------------------------------------
def _puxar_medicoes(emp_id):
    desde = (datetime.now(timezone.utc) - timedelta(days=DESCARGA_DIAS)).strftime("%Y-%m-%dT%H:%M:%S")
    linhas, off = [], 0
    while True:
        p = (f"oct_medicoes?empresa_id=eq.{emp_id}&select=tanque_numero,volume,medido_em"
             f"&medido_em=gte.{desde}&order=tanque_numero.asc,medido_em.asc&limit=1000&offset={off}")
        try:
            parte = _rest_get(p.split("?", 1)[0], "?" + p.split("?", 1)[1])
        except Exception:
            break
        linhas += parte
        if len(parte) < 1000 or off > 40000:
            break
        off += 1000
    return linhas


def _detectar(serie):
    limpo, visto = [], set()
    for dt, v in serie:
        if v is None or v <= 0:
            continue
        k = dt.replace(microsecond=0).isoformat()
        if k in visto:
            continue
        visto.add(k); limpo.append((dt, float(v)))
    n = len(limpo); ds = []; i = 0
    while i < n - 1:
        base = limpo[i][1]; pico = base; pico_idx = i; k = i + 1; subiu = False
        while k < n and (limpo[k][0] - limpo[i][0]).total_seconds() < JANELA_MIN * 60:
            v = limpo[k][1]
            if v > pico:
                pico = v; pico_idx = k
            if v > base + 100:
                subiu = True
            if subiu and v < pico - QUEDA_FIM and (pico - base) > MIN_DESCARGA:
                break
            k += 1
        if pico - base > MIN_DESCARGA:
            ds.append({"ini": limpo[i][0], "fim": limpo[pico_idx][0], "v_ini": base, "v_pico": pico})
            i = pico_idx + 1
        else:
            i += 1
    fund = []
    for d in ds:
        if fund and (d["ini"] - fund[-1]["fim"]).total_seconds() < 20 * 60:
            fund[-1]["fim"] = d["fim"]; fund[-1]["v_pico"] = max(fund[-1]["v_pico"], d["v_pico"])
        else:
            fund.append(dict(d))
    for d in fund:
        d["salto"] = round(d["v_pico"] - d["v_ini"], 1)
    return fund


# ------------------------------------------------------------------
# 2. vendas na janela (reconstrucao)
# ------------------------------------------------------------------
def _vendas(emp_id, fuel, ini, fim):
    i2 = urllib.parse.quote(_iso(ini - timedelta(minutes=10)))
    f2 = urllib.parse.quote(_iso(fim + timedelta(minutes=10)))
    try:
        ab = _rest_get("oct_pdv_abastecimentos",
                       f"?empresa_id=eq.{emp_id}&data_abast=gte.{i2}&data_abast=lte.{f2}"
                       f"&select=combustivel,litros&limit=800")
    except Exception:
        return 0.0
    return round(sum(float(a.get("litros") or 0) for a in ab if _fuel_tanque(a.get("combustivel")) == fuel), 1)


# ------------------------------------------------------------------
# 3. NFs de combustivel (parse XML)
# ------------------------------------------------------------------
def _nfs_combustivel(emp_id):
    try:
        rows = _rest_get("oct_nfe_manifestadas",
                         f"?empresa_id=eq.{emp_id}&tipo=eq.nfe_completa"
                         f"&select=numero,chave_nfe,emitente,emissao,xml,status&order=emissao.desc&limit=80")
    except Exception:
        return []
    itens = []
    for n in rows:
        xml = n.get("xml") or ""
        if "cProdANP" not in xml:
            continue
        try:
            root = ET.fromstring(xml)
        except Exception:
            continue
        for det in root.findall(".//n:det", NS):
            prod = det.find("n:prod", NS)
            if prod is None:
                continue
            anp = prod.findtext("n:comb/n:cProdANP", default="", namespaces=NS)
            xprod = prod.findtext("n:xProd", default="", namespaces=NS)
            fuel = _fuel_anp(anp, xprod)
            if not fuel:
                continue
            try:
                qcom = round(float(prod.findtext("n:qCom", default="0", namespaces=NS)), 2)
            except (TypeError, ValueError):
                qcom = 0.0
            if qcom <= 0:
                continue
            itens.append({"numero": n.get("numero"), "chave": n.get("chave_nfe"),
                          "emitente": n.get("emitente"), "emissao": (n.get("emissao") or "")[:10],
                          "fuel": fuel, "qcom": qcom})
    return itens


# ------------------------------------------------------------------
# 4. casamento (1 nota; senao combo de 2)
# ------------------------------------------------------------------
def _casar(recon, fuel, dia, nfs):
    cands = []
    for nf in nfs:
        if nf["fuel"] != fuel:
            continue
        try:
            emi = datetime.fromisoformat(nf["emissao"]).date()
        except Exception:
            continue
        dd = (dia.date() - emi).days
        if 0 <= dd <= DIAS_NF:
            cands.append(nf)
    tol = max(TOL_ABS, recon * TOL_PCT / 100.0)
    # 1 nota
    sing = sorted(cands, key=lambda nf: abs(recon - nf["qcom"]))
    dentro = [nf for nf in sing if abs(recon - nf["qcom"]) <= tol]
    if dentro:
        nf = dentro[0]
        conf = "alta" if len(dentro) == 1 else "media"
        return {"nfs": [nf], "qtot": nf["qcom"], "dif": round(abs(recon - nf["qcom"]), 1), "conf": conf}
    # combo de 2 notas (entrega dupla)
    m = len(cands); melhor = None
    for a in range(m):
        for b in range(a + 1, m):
            soma = cands[a]["qcom"] + cands[b]["qcom"]
            dif = abs(recon - soma)
            if dif <= tol and (melhor is None or dif < melhor["dif"]):
                melhor = {"nfs": [cands[a], cands[b]], "qtot": round(soma, 2), "dif": round(dif, 1), "conf": "media"}
    if melhor:
        return melhor
    return None


# ------------------------------------------------------------------
# 5. grava candidato (idempotente; nao rebaixa status)
# ------------------------------------------------------------------
VIZINHANCA_H = 2   # mesma descarga re-detectada com `ini` deslocado (borda da janela)


def _gravar(emp_id, d, fuel, recon, vend, match):
    chave_desc = _iso(d["ini"])
    # A MESMA descarga fisica volta a ser detectada com `ini` deslocado alguns
    # minutos quando a borda da janela de DESCARGA_DIAS passa por cima dela (o
    # primeiro ponto da serie muda a cada ciclo, entao a "base" do salto muda).
    # Chavear por descarga_ini EXATO criava uma linha nova a cada ciclo por ~1h,
    # 7 dias depois da descarga (803 linhas excedentes em 23/09/2026). Por isso a
    # busca e' por VIZINHANCA: mesma empresa+tanque, ini dentro de +-VIZINHANCA_H.
    # OBS: o '+00:00' do timestamp PRECISA de URL-encode ('+' cru vira espaco no
    # PostgREST -> busca falha -> POST duplicado silencioso e a linha nunca atualiza)
    ini_de = urllib.parse.quote(_iso(d["ini"] - timedelta(hours=VIZINHANCA_H)))
    ini_ate = urllib.parse.quote(_iso(d["ini"] + timedelta(hours=VIZINHANCA_H)))
    try:
        ex = _rest_get("oct_nfe_descarga",
                       f"?empresa_id=eq.{emp_id}&tanque_numero=eq.{d['tanque']}"
                       f"&descarga_ini=gte.{ini_de}&descarga_ini=lte.{ini_ate}"
                       f"&select=id,status,diferenca,confianca,volume_salto,nf_chaves&order=criado_em.asc")
    except Exception:
        ex = []
    # "mesma descarga" = vizinha no tempo E salto compativel (ou mesma NF). Dois
    # compartimentos do mesmo produto no mesmo tanque, 1h um do outro, tem saltos
    # diferentes (ex.: AC 10/09: 4.816 L e 8.029 L) e continuam sendo 2 linhas.
    nf_new = ",".join(x.get("chave") or "" for x in match["nfs"]) if match else ""

    def _mesma(e):
        try:
            vs = float(e.get("volume_salto") or 0)
        except (TypeError, ValueError):
            vs = 0.0
        if abs(vs - d["salto"]) <= max(300.0, 0.10 * d["salto"]):
            return True
        return bool(nf_new) and (e.get("nf_chaves") or "") == nf_new
    ex = [e for e in ex if _mesma(e)]
    if any(e.get("status") not in (None, "candidato", "sem_nf") for e in ex):
        return  # ja confirmado/entrada feita -> nao mexe
    if ex:
        # ja existe candidato p/ essa descarga: so' atualiza se o casamento novo
        # for igual ou melhor (a deteccao na borda da janela sai truncada e pode
        # piorar a diferenca); e mantem o descarga_ini original da linha.
        old = ex[0]
        old_dif = old.get("diferenca")
        new_dif = match["dif"] if match else None
        if old.get("confianca") not in (None, "sem_nf"):
            if new_dif is None or (old_dif is not None and new_dif > float(old_dif)):
                return
    reg = {
        "empresa_id": emp_id, "tanque_numero": d["tanque"], "combustivel": fuel,
        "descarga_ini": chave_desc, "descarga_fim": _iso(d["fim"]),
        "volume_salto": d["salto"], "volume_vendas": vend, "volume_reconstruido": recon,
        "nf_chaves": (",".join(x.get("chave") or "" for x in match["nfs"]) if match else None),
        "nf_numeros": (",".join(str(x.get("numero") or "") for x in match["nfs"]) if match else None),
        "qcom_total": (match["qtot"] if match else None),
        "diferenca": (match["dif"] if match else None),
        "confianca": (match["conf"] if match else "sem_nf"),
        "status": "candidato", "atualizado_em": _iso(datetime.now(timezone.utc)),
    }
    if ex:
        reg.pop("descarga_ini", None)   # chave original fica; nunca cria 2a linha
        _rest("PATCH", f"oct_nfe_descarga?id=eq.{ex[0]['id']}", body=reg, prefer="return=minimal")
    else:
        _rest("POST", "oct_nfe_descarga", body=reg, prefer="return=minimal")


# ------------------------------------------------------------------
# CIENCIA nas notas resumo proximas de uma descarga (puxa o XML completo)
# ------------------------------------------------------------------
def _ciencia_resumos(emp_id, datas_descarga):
    if not datas_descarga:
        return
    try:
        resumos = _rest_get("oct_nfe_manifestadas",
                            f"?empresa_id=eq.{emp_id}&status=eq.sem_manifestacao&tipo=eq.resumo"
                            f"&chave_nfe=neq.null&select=id,chave_nfe,emissao&limit=100")
    except Exception:
        return
    alvo = []
    for r in resumos:
        try:
            ed = datetime.fromisoformat((r.get("emissao") or "")[:10]).date()
        except Exception:
            continue
        for dd in datas_descarga:
            if 0 <= (dd - ed).days <= DIAS_NF + 1:   # nota emitida ate ~DIAS_NF antes da descarga
                alvo.append(r)
                break
    if not alvo:
        return
    try:
        dados = carregar_empresa(emp_id)
        cnpj = str((dados.get("empresa") or {}).get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
        from .evento import registrar_evento
    except Exception as e:
        print(f"[descarga] {emp_id}: cert p/ ciencia falhou: {e}")
        return
    amb = os.environ.get("DFE_AMBIENTE", "producao")
    for r in alvo:
        try:
            # registrar_evento NAO levanta erro quando a SEFAZ rejeita: devolve
            # ok=False. Antes marcava "ciencia" de qualquer jeito e a nota ficava
            # presa como resumo para sempre (KR 1655 e 3 ALE de 29/09 no
            # Florestal, 01/10/2026). So' marca se a SEFAZ registrou.
            res = registrar_evento(cnpj, r["chave_nfe"], dados["cert_base64"], dados["cert_senha"], amb, tipo="210210") or {}
            if not (res.get("ok") is True or str(res.get("cstat")) in ("135", "136", "573")):
                print(f"[descarga] {emp_id}: ciencia RECUSADA {r['chave_nfe'][:12]}: "
                      f"{res.get('cstat')} {res.get('xmotivo') or res.get('erro') or ''}")
                continue
            _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{r['id']}", body={"status": "ciencia"}, prefer="return=minimal")
            print(f"[descarga] {emp_id}: ciencia p/ puxar XML da nota {r['chave_nfe'][:12]} (descarga sem NF)")
        except Exception as e:
            print(f"[descarga] {emp_id}: ciencia falhou {str(r.get('chave_nfe'))[:12]}: {e}")


_RECIENCIA_ULT = {}     # chave -> quando este processo tentou pela ultima vez
CIENCIA_DIAS = int(os.environ.get("CIENCIA_DIAS", "10"))
_DISTRIB = {"quando": None, "raizes": set()}


def _raizes_distribuidoras():
    """Raiz do CNPJ (8 digitos) de quem ja' mandou nota com combustivel de tanque
    para algum posto do grupo -- ALE, Setta, Rio Branco, Royal FIC, Raizen...
    O resumo que a SEFAZ entrega (resNFe) so' traz emitente e valor, sem os
    itens: a unica forma de saber que e' combustivel e' pelo emitente.
    Relido de 6 em 6 h; se a consulta falhar, vale a lista anterior."""
    agora = datetime.utcnow()
    if _DISTRIB["quando"] and agora - _DISTRIB["quando"] < timedelta(hours=6):
        return _DISTRIB["raizes"]
    try:
        desde = (agora - timedelta(days=365)).date().isoformat()
        ou = ",".join("xml.like.*" + urllib.parse.quote(f"<cProdANP>{p}") + "*" for p in ("320102", "810101", "820101"))
        linhas = _rest_get("oct_nfe_manifestadas",
                           f"?tipo=eq.nfe_completa&emissao=gte.{desde}&or=({ou})&select=emit_cnpj&limit=20000")
        raizes = {"".join(c for c in str(l.get("emit_cnpj") or "") if c.isdigit())[:8] for l in linhas or []}
        _DISTRIB["raizes"] = {r for r in raizes if len(r) == 8}
        _DISTRIB["quando"] = agora
    except Exception as e:
        print(f"[descarga] nao li as distribuidoras: {e}")
    return _DISTRIB["raizes"]


def _ciencia_distribuidoras(emp_id):
    """Ciencia (210210) em TODA nota de distribuidora de combustivel assim que
    ela chega como resumo. Decisao do Ronan em 07/10/2026, para o custo do
    combustivel acompanhar a compra no mesmo dia: sem a ciencia o XML completo
    nao vem, e sem o XML o custo (entrada_auto.atualizar_custos) nao enxerga a
    nota. Antes a ciencia so' saia quando havia descarga sem NF na janela
    (_ciencia_resumos, que continua valendo para emitente desconhecido).

    So' nota dos ultimos CIENCIA_DIAS dias, de emitente que ja' vendeu
    combustivel para o grupo, e nao cancelada. Nao depende da sonda.
    Desliga com CIENCIA_DISTRIBUIDORA=0 no Railway."""
    if os.environ.get("CIENCIA_DISTRIBUIDORA", "1").strip().lower() in ("0", "false", "nao", "off"):
        return 0
    agora = datetime.utcnow()
    desde = (agora - timedelta(days=CIENCIA_DIAS)).date().isoformat()
    try:
        resumos = _rest_get("oct_nfe_manifestadas",
                            f"?empresa_id=eq.{emp_id}&status=eq.sem_manifestacao&tipo=eq.resumo"
                            f"&chave_nfe=not.is.null&emissao=gte.{desde}"
                            f"&select=id,chave_nfe,emit_cnpj&limit=100")
    except Exception:
        return 0
    if not resumos:
        return 0
    raizes = _raizes_distribuidoras()
    alvo = []
    for r in resumos:
        cnpj = "".join(c for c in str(r.get("emit_cnpj") or "") if c.isdigit()) or str(r["chave_nfe"])[6:20]
        if cnpj[:8] not in raizes:
            continue
        ult = _RECIENCIA_ULT.get(r["chave_nfe"])
        if ult and agora - ult < timedelta(hours=6):
            continue                      # recusada ha' pouco: nao insiste a cada ciclo
        alvo.append(r)
    if not alvo:
        return 0
    try:
        from .entrada_auto import chaves_canceladas
        canceladas = chaves_canceladas(emp_id)
        dados = carregar_empresa(emp_id)
        cnpj_emp = str((dados.get("empresa") or {}).get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
        from .evento import registrar_evento
    except Exception as e:
        print(f"[descarga] {emp_id}: ciencia de distribuidora adiada: {e}")
        return 0
    amb = os.environ.get("DFE_AMBIENTE", "producao")
    feitas = 0
    for r in alvo:
        if r["chave_nfe"] in canceladas:
            continue
        _RECIENCIA_ULT[r["chave_nfe"]] = agora
        # RESERVA a linha antes de falar com a SEFAZ: o servidor roda 2 processos
        # e os dois passam por aqui no mesmo ciclo. So' quem conseguir trocar
        # sem_manifestacao -> ciencia manda o evento; o outro nao recebe a linha.
        st, pego = _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{r['id']}&status=eq.sem_manifestacao",
                         body={"status": "ciencia"}, prefer="return=representation")
        if not (isinstance(pego, list) and pego):
            continue
        try:
            res = registrar_evento(cnpj_emp, r["chave_nfe"], dados["cert_base64"], dados["cert_senha"], amb, tipo="210210") or {}
        except Exception as e:
            res = {"erro": str(e)}
        if res.get("ok") is True or str(res.get("cstat")) in ("135", "136", "573"):
            feitas += 1
            print(f"[descarga] {emp_id}: ciencia na nota de distribuidora {r['chave_nfe'][:12]} (chegou como resumo)")
        else:
            _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{r['id']}", body={"status": "sem_manifestacao"}, prefer="return=minimal")
            print(f"[descarga] {emp_id}: ciencia de distribuidora RECUSADA {r['chave_nfe'][:12]}: "
                  f"{res.get('cstat')} {res.get('xmotivo') or res.get('erro') or ''}")
    return feitas


def _reciencia_presas(emp_id):
    """Resumo marcado 'ciencia' ha' mais de 3 h e SEM o XML completo = a ciencia
    nao foi registrada na SEFAZ (ou o XML nao veio). Reenvia: 573 (duplicidade)
    tambem vale -- quer dizer que ja' estava registrada. Se a SEFAZ recusar,
    devolve a nota para 'sem_manifestacao' para aparecer de novo em Pendentes.

    Roda a cada ciclo de 10 min, entao tem freio: a mesma nota so' e' reenviada
    de 6 em 6 h (se a SEFAZ responde 573 e o XML nao vem, sem o freio seriam 6
    eventos por hora, para sempre). E nota CANCELADA pelo fornecedor fica fora:
    o XML completo dela nunca vem. Nota emitida ha' mais de CIENCIA_DIAS + 5
    dias tambem: se o XML nao veio ate' ai', reenviar nao resolve (as duas ALE
    de 30/06 do Tijuco seriam reenviadas de 6 em 6 h para sempre)."""
    desde = (datetime.utcnow() - timedelta(days=CIENCIA_DIAS + 5)).date().isoformat()
    try:
        presas = _rest_get("oct_nfe_manifestadas",
                           f"?empresa_id=eq.{emp_id}&status=eq.ciencia&tipo=eq.resumo&emissao=gte.{desde}"
                           f"&chave_nfe=neq.null&select=id,chave_nfe,criado_em&limit=50")
    except Exception:
        return
    if not presas:
        return
    try:
        from .entrada_auto import chaves_canceladas
        canceladas = chaves_canceladas(emp_id)
    except Exception as e:
        print(f"[descarga] {emp_id}: nao li os cancelamentos, re-ciencia adiada: {e}")
        return
    agora = datetime.utcnow()
    limite = agora - timedelta(hours=3)
    alvo = []
    for r in presas:
        try:
            if r["chave_nfe"] in canceladas:
                continue
            ult = _RECIENCIA_ULT.get(r["chave_nfe"])
            if ult and agora - ult < timedelta(hours=6):
                continue
            if datetime.fromisoformat((r.get("criado_em") or "")[:19]) > limite:
                continue
            comp = _rest_get("oct_nfe_manifestadas",
                             f"?empresa_id=eq.{emp_id}&chave_nfe=eq.{r['chave_nfe']}&tipo=eq.nfe_completa&select=id&limit=1")
            if not comp:
                alvo.append(r)
        except Exception:
            continue
    if not alvo:
        return
    try:
        dados = carregar_empresa(emp_id)
        cnpj = str((dados.get("empresa") or {}).get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
        from .evento import registrar_evento
    except Exception as e:
        print(f"[descarga] {emp_id}: cert p/ re-ciencia falhou: {e}")
        return
    amb = os.environ.get("DFE_AMBIENTE", "producao")
    for r in alvo:
        _RECIENCIA_ULT[r["chave_nfe"]] = agora
        try:
            res = registrar_evento(cnpj, r["chave_nfe"], dados["cert_base64"], dados["cert_senha"], amb, tipo="210210") or {}
            ok = res.get("ok") is True or str(res.get("cstat")) in ("135", "136", "573")
            print(f"[descarga] {emp_id}: re-ciencia {r['chave_nfe'][:12]} -> {res.get('cstat')} "
                  f"{res.get('xmotivo') or res.get('erro') or ''}")
            if not ok:
                _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{r['id']}", body={"status": "sem_manifestacao"}, prefer="return=minimal")
        except Exception as e:
            print(f"[descarga] {emp_id}: re-ciencia falhou {str(r.get('chave_nfe'))[:12]}: {e}")


# ------------------------------------------------------------------
# ENTRADA: casa as descargas de uma empresa
# ------------------------------------------------------------------
def casar_empresa(emp_id):
    # a ciencia das notas de distribuidora nao depende da sonda: vem antes de tudo
    try:
        _ciencia_distribuidoras(emp_id)
    except Exception as e:
        print(f"[descarga] {emp_id}: ciencia de distribuidora: {e}")
    try:
        med = _puxar_medicoes(emp_id)
        if not med:
            return {"ok": True, "casadas": 0, "obs": "sem medicoes"}
        tanques = _rest_get("oct_tanques", f"?empresa_id=eq.{emp_id}&select=numero,combustivel")
        tq = {t["numero"]: _fuel_tanque(t["combustivel"]) for t in tanques}
        por_tanque = {}
        for l in med:
            por_tanque.setdefault(l["tanque_numero"], []).append((_t(l["medido_em"]), l.get("volume")))
        nfs = _nfs_combustivel(emp_id)
        # NF ja' CONSUMIDA por uma descarga com entrada feita nao concorre de novo.
        # Senao a 2a carga do mesmo produto (NF nova ainda chegando pelo DistDFe)
        # casa com a NF velha em conf 'alta', o entrada_auto responde "ja importada"
        # e a NF certa nunca da' entrada (13 NFs em 2 descargas ate 23/09/2026).
        try:
            feitas = _rest_get("oct_nfe_descarga",
                               f"?empresa_id=eq.{emp_id}&status=eq.entrada_feita&select=nf_chaves&limit=1000")
            usadas = {c for r in feitas for c in (r.get("nf_chaves") or "").split(",") if c}
        except Exception:
            usadas = set()
        nfs = [nf for nf in nfs if nf.get("chave") not in usadas]
        n_casadas = 0
        n_descargas = 0
        datas_sem_nf = set()
        for t, serie in por_tanque.items():
            fuel = tq.get(t)
            if not fuel:
                continue
            serie_ord = sorted(serie, key=lambda x: x[0])
            borda = serie_ord[0][0] + timedelta(minutes=JANELA_MIN)
            for d in _detectar(serie_ord):
                if d["ini"] < borda:
                    # Descarga CORTADA pela borda da janela de DESCARGA_DIAS: a
                    # "base" do salto e' o 1o ponto da serie (parcial) e o `ini`
                    # anda a cada ciclo. Ela ja' foi gravada inteira quando era
                    # recente -- ignora em vez de gerar linha nova/truncada.
                    continue
                n_descargas += 1
                d["tanque"] = t
                vend = _vendas(emp_id, fuel, d["ini"], d["fim"])
                recon = round(d["salto"] + vend, 1)
                match = _casar(recon, fuel, d["ini"], nfs)
                _gravar(emp_id, d, fuel, recon, vend, match)
                if match:
                    n_casadas += 1
                else:
                    datas_sem_nf.add(d["ini"].date())
        # descarga sem nota casada -> da ciencia nas resumo da janela p/ puxar o XML completo
        _ciencia_resumos(emp_id, datas_sem_nf)
        _reciencia_presas(emp_id)
        if n_casadas:
            print(f"[descarga] {emp_id}: {n_casadas} descarga(s) casada(s) com NF")
        return {"ok": True, "casadas": n_casadas, "descargas": n_descargas,
                "nfs_combustivel": len(nfs), "medicoes": len(med)}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[descarga] {emp_id}: erro no casamento: {e}\n{tb}")
        return {"ok": False, "erro": str(e), "traceback": tb[-1500:]}
