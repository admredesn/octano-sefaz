"""
sefaz/descarga_match.py  -  Fase 2: casa a DESCARGA (sonda) com a NF de combustivel.

Roda no ciclo do dfe_auto (10 em 10 min, todas as empresas). Para cada empresa:
  1) le as descargas que a SONDA do posto registrou (oct_sonda_entregas: console do medidor
     e detector minuto a minuto do nucleo, conferidos contra o PDF do console, <= 20 L);
  2) junta os compartimentos do mesmo caminhao no mesmo tanque (bloco);
  3) casa cada bloco com a(s) NF(s) de combustivel (oct_nfe_manifestadas): mesmo combustivel
     (por ANP), emitida ate DIAS_NF dias antes / 12 h depois, volume parecido. 1 nota; senao
     2 notas somadas (dois compartimentos/notas no mesmo caminhao);
  4) grava o par em oct_nfe_descarga. confianca 'alta' (1 nota, diferenca <= max(TOL_ABS,
     TOL_PCT%), sem outra nota que sirva) -> entrada_auto da' a entrada; 'media' fica para
     conferir; 'sem_nf' -> ciencia nas notas resumo da janela (puxa o XML completo).

ATE' 17/10/2026 a descarga era detectada AQUI, pelas leituras cruas de oct_medicoes (salto >
500 L numa janela de 75 min), e isso cortava a descarga ao meio e gerava "sem nota" falso:
  - o `ini` era o 1o ponto cuja janela de 75 min alcancava a subida -> ~72 min ANTES da
    descarga real (AC T3 13/10: sonda 18:04, tabela 16:52);
  - as medicoes chegam ao vivo e o ciclo e' de 10 min: o 1o ciclo que via a subida pegava o
    caminhao AINDA descarregando e gravava o volume parcial (2.571 L de 4.974);
  - nos ciclos seguintes a deteccao completa tinha o MESMO `ini`, mas o _gravar (vizinhanca de
    23/09) so' aceitava "mesma descarga" com salto +-10% -> tentava POST -> a UNIQUE
    (empresa, tanque, descarga_ini) recusava em silencio (409) -> a linha ficava congelada em
    'sem_nf' (22-23 POSTs recusados por descarga no replay de 24/09 a 15/10);
  - _vendas comparava data_abast (hora LOCAL rotulada +00:00) com UTC: somava as vendas de 3 h
    depois (quase sempre 0 na hora do ciclo).
A sonda do posto ja' fecha a descarga certa (inicio = ultima leitura antes de subir, fim = pico,
compartimentos separados). Por isso a fonte passou a ser oct_sonda_entregas.

Config (env): DESCARGA_DIAS (7), DESCARGA_TOL_PCT (1.0), DESCARGA_TOL_ABS (150),
DESCARGA_DIAS_NF (4), DESCARGA_ESPERA_MIN (60).
Idempotente: a linha e' achada por SOBREPOSICAO (mesmo tanque, intervalo encostando no bloco);
nunca mexe em linha que ja' saiu de 'candidato' (entrada feita).
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
TOL_PCT = float(os.environ.get("DESCARGA_TOL_PCT", "1.0"))     # confianca 'alta'
TOL_ABS = float(os.environ.get("DESCARGA_TOL_ABS", "150"))
DIAS_NF = int(os.environ.get("DESCARGA_DIAS_NF", "4"))         # nota emitida ate N dias antes...
HORAS_NF_DEPOIS = 12                                           # ...ou ate 12 h depois da descarga
ESPERA_MIN = int(os.environ.get("DESCARGA_ESPERA_MIN", "60"))  # bloco fechado ha' pelo menos isso

TOL_LARGA_PCT = 6.0     # casa como 'media' (conferir) -- mesma regua do relatorio do retaguarda
TOL_LARGA_ABS = 300.0
MIN_ENTREGA = 150.0     # registro da sonda menor que isso e' ruido
MIN_ITEM_NF = 400.0     # item de combustivel menor que isso nao e' carga de caminhao
MESMA_INI_MIN = 15      # console x nucleo: mesma descarga = inicio a ate 15 min...
MESMA_VOL_INI = 150.0   # ...E volume inicial a ate 150 L (regra do nucleo)
BLOCO_GAP_MIN = 20      # compartimentos do mesmo caminhao: proximo comeca ate 20 min depois
FOLGA_MIN = 10          # linha gravada x bloco: encostam com ate 10 min de folga
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
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _todas(path, params, pagina=1000, maximo=20):
    out = []
    for i in range(maximo):
        parte = _rest_get(path, f"{params}&limit={pagina}&offset={i * pagina}") or []
        out += parte
        if len(parte) < pagina:
            break
    return out


# ------------------------------------------------------------------
# 1. descargas da sonda (puras)
# ------------------------------------------------------------------
def entregas_unicas(linhas, agora=None):
    """oct_sonda_entregas -> [{tanque, ini, fim, litros, vol_ini, vol_fim, origem}] sem repeticao.

    A MESMA descarga vem duas vezes (console do medidor + detector do nucleo). Mesma = mesmo
    tanque e (inicio a ate 15 min E volume inicial a ate 150 L, ou intervalos sobrepostos).
    So' o horario NAO serve: juntava compartimentos (AC T3 24/09: 702 + 1.208 + 5.118 L com
    4 min entre um e outro). Fica a de MAIOR volume: o console as vezes comeca atrasado
    (FLO T4 29/09: console 16:44 2.857 L, nucleo 16:40 3.118 L)."""
    cand = []
    for x in linhas:
        try:
            ini = _t(x["inicio"])
            fim = _t(x.get("fim") or x["inicio"])
        except Exception:
            continue
        if ini.year < 2020 or (agora and ini > agora + timedelta(hours=1)):
            continue                                  # console com relogio errado (AC voltou a 2017)
        vi, vf = _num(x.get("vol_ini")), _num(x.get("vol_fim"))
        vol = _num(x.get("entregue")) or (vf - vi)
        if vol < MIN_ENTREGA:
            continue
        cand.append({"tanque": int(_num(x.get("tanque_numero"))), "ini": ini, "fim": max(fim, ini),
                     "litros": round(vol, 1), "vol_ini": vi, "vol_fim": vf, "origem": x.get("origem") or ""})
    cand.sort(key=lambda e: (-e["litros"], 0 if e["origem"] == "console" else 1, e["ini"]))
    out = []
    for e in cand:
        if any(o["tanque"] == e["tanque"] and (
                (abs((o["ini"] - e["ini"]).total_seconds()) <= MESMA_INI_MIN * 60
                 and abs(o["vol_ini"] - e["vol_ini"]) <= MESMA_VOL_INI)
                or (min(o["fim"], e["fim"]) - max(o["ini"], e["ini"])).total_seconds() > 60)
               for o in out):
            continue
        out.append(e)
    return sorted(out, key=lambda e: (e["tanque"], e["ini"]))


def blocos(entregas):
    """Compartimentos do mesmo caminhao no mesmo tanque viram UM bloco: a sonda separa quando a
    vazao para e volta (AC T3 01/10: 4.914 + 3.027 L). Proximo registro comecando ate
    BLOCO_GAP_MIN depois do fim do anterior = mesmo bloco."""
    out = []
    for e in sorted(entregas, key=lambda e: (e["tanque"], e["ini"])):
        b = out[-1] if out else None
        if b and b["tanque"] == e["tanque"] and (e["ini"] - b["fim"]).total_seconds() <= BLOCO_GAP_MIN * 60:
            b["fim"] = max(b["fim"], e["fim"]); b["litros"] = round(b["litros"] + e["litros"], 1)
            b["vol_fim"] = e["vol_fim"]; b["partes"].append(e)
        else:
            out.append({"tanque": e["tanque"], "ini": e["ini"], "fim": e["fim"], "litros": e["litros"],
                        "vol_ini": e["vol_ini"], "vol_fim": e["vol_fim"], "partes": [e]})
    return out


# ------------------------------------------------------------------
# 2. NFs de combustivel (pura: linhas de oct_nfe_manifestadas com xml)
# ------------------------------------------------------------------
def notas_combustivel(linhas, canceladas=()):
    """-> itens de combustivel de notas de ENTRADA validas: [{chave, numero, emitente, cnpj,
    emissao (UTC), fuel, qcom}]. Fora: cancelada, devolvida (refNFe de nota de entrada do
    fornecedor -- Tijuco 369375/370218), nota que nao e' de saida do emitente, item < 400 L."""
    notas, devolvidas = [], set()
    for n in linhas:
        xml = n.get("xml") or ""
        if "cProdANP" not in xml and "<refNFe>" not in xml:
            continue
        try:
            root = ET.fromstring(xml)
        except Exception:
            continue
        tp = root.findtext(".//n:ide/n:tpNF", default="", namespaces=NS)
        if tp == "0":
            devolvidas.update(r.text.strip() for r in root.findall(".//n:ide/n:NFref/n:refNFe", NS) if r.text)
            continue
        notas.append((n, root, tp))
    itens = []
    for n, root, tp in notas:
        ch = n.get("chave_nfe")
        if tp != "1" or not ch or ch in canceladas or ch in devolvidas:
            continue
        dh = root.findtext(".//n:ide/n:dhEmi", default="", namespaces=NS)
        try:
            emissao = _t(dh) if dh else _t((n.get("emissao") or "")[:10] + "T12:00:00-03:00")
        except Exception:
            continue
        cnpj = root.findtext(".//n:emit/n:CNPJ", default="", namespaces=NS)
        for det in root.findall(".//n:det", NS):
            prod = det.find("n:prod", NS)
            if prod is None:
                continue
            fuel = _fuel_anp(prod.findtext("n:comb/n:cProdANP", default="", namespaces=NS),
                             prod.findtext("n:xProd", default="", namespaces=NS))
            q = round(_num(prod.findtext("n:qCom", default="0", namespaces=NS)), 2)
            if not fuel or q < MIN_ITEM_NF:
                continue
            itens.append({"numero": n.get("numero"), "chave": ch, "emitente": n.get("emitente"),
                          "cnpj": cnpj, "emissao": emissao, "fuel": fuel, "qcom": q})
    return itens


# ------------------------------------------------------------------
# 3. casamento (puro)
# ------------------------------------------------------------------
def tol_alta(vol):
    return max(TOL_ABS, vol * TOL_PCT / 100.0)


def tol_larga(vol):
    return max(TOL_LARGA_ABS, vol * TOL_LARGA_PCT / 100.0)


def casar(blocos_, itens, fuel_do_tanque):
    """Liga cada bloco (em ordem de hora) a 1 nota -- ou 2 somadas -- do mesmo combustivel,
    emitida ate DIAS_NF dias antes ou HORAS_NF_DEPOIS depois, volume dentro de tol_larga.
    Cada item de nota serve uma vez so'. Empate: ganha a nota do MESMO CAMINHAO (outro item
    dela casa com outra descarga do posto em ate 3 h) e depois a emitida mais perto (0,5 L de
    penalidade por hora) -- mesma regua do relatorio Descargas x Notas do retaguarda.
    Devolve {id(bloco): {"nfs", "qtot", "dif", "conf"} | None}."""
    usados, res = set(), {}
    ordem = sorted(blocos_, key=lambda b: b["ini"])

    def janela(i, b):
        it = itens[i]
        return (it["fuel"] == fuel_do_tanque.get(b["tanque"])
                and b["ini"] - timedelta(days=DIAS_NF) <= it["emissao"] <= b["ini"] + timedelta(hours=HORAS_NF_DEPOIS))

    def mesmo_caminhao(i, b):
        for o in ordem:
            if o is b or abs((o["ini"] - b["ini"]).total_seconds()) > 3 * 3600:
                continue
            fo = fuel_do_tanque.get(o["tanque"])
            if any(j != i and itens[j]["chave"] == itens[i]["chave"] and itens[j]["fuel"] == fo for j in range(len(itens))):
                return True
        return False

    def pontos(idx, b, d):
        horas = sum(abs((b["ini"] - itens[i]["emissao"]).total_seconds()) / 3600 for i in idx) / len(idx)
        return d + 0.5 * horas - (100 if any(mesmo_caminhao(i, b) for i in idx) else 0)

    for b in ordem:
        vol = b["litros"]
        cand = [i for i in range(len(itens)) if i not in usados and janela(i, b)]
        melhor, servem = None, []
        for i in cand:                                       # uma nota
            d = abs(itens[i]["qcom"] - vol)
            if d <= tol_larga(vol):
                p = pontos([i], b, d)
                if d <= tol_alta(vol):
                    servem.append(i)
                if melhor is None or p < melhor[0]:
                    melhor = (p, [i], d)
        if melhor is None:                                   # duas notas no mesmo caminhao
            for x in range(len(cand)):
                for y in range(x + 1, len(cand)):
                    a, c = cand[x], cand[y]
                    if itens[a]["chave"] == itens[c]["chave"]:
                        continue
                    d = abs(itens[a]["qcom"] + itens[c]["qcom"] - vol)
                    if d <= tol_larga(vol):
                        p = pontos([a, c], b, d)
                        if melhor is None or p < melhor[0]:
                            melhor = (p, [a, c], d)
        if not melhor:
            res[id(b)] = None
            continue
        idx, d = melhor[1], melhor[2]
        # 'alta' = 1 nota, dentro da tolerancia estreita e SEM outra nota que tambem sirva
        # (duas notas iguais na janela = quem decide e' a pessoa, nao o robo)
        conf = "alta" if (len(idx) == 1 and d <= tol_alta(vol) and servem == idx) else "media"
        usados.update(idx)
        nfs = [itens[i] for i in idx]
        res[id(b)] = {"nfs": nfs, "qtot": round(sum(n["qcom"] for n in nfs), 2), "dif": round(d, 1), "conf": conf}
    return res


# ------------------------------------------------------------------
# 4. linha gravada x bloco (puras)
# ------------------------------------------------------------------
def encosta(linha, b):
    """A linha de oct_nfe_descarga e' desta descarga? Mesmo tanque e intervalos encostando
    (folga de FOLGA_MIN). Cobre as linhas antigas, cujo `ini` ficou ~72 min antes da descarga
    e o `fim` no meio dela, e a troca nucleo -> console (inicio 1-4 min diferente)."""
    try:
        li, lf = _t(linha["descarga_ini"]), _t(linha.get("descarga_fim") or linha["descarga_ini"])
    except Exception:
        return False
    folga = timedelta(minutes=FOLGA_MIN)
    return (int(_num(linha.get("tanque_numero"))) == b["tanque"]
            and li <= b["fim"] + folga and lf >= b["ini"] - folga)


def registro(emp_id, b, fuel, match, agora):
    # sem somar as vendas da janela: a descarga dura 5-15 min e, no ensaio de set/out, somar o
    # que o PDV vendeu nesse tempo nao aproximou da nota (AC T3 09/10: -54 L -> -121 L)
    return {
        "empresa_id": emp_id, "tanque_numero": b["tanque"], "combustivel": fuel,
        "descarga_ini": _iso(b["ini"]), "descarga_fim": _iso(b["fim"]),
        "volume_salto": round(b["litros"], 1), "volume_vendas": 0,
        "volume_reconstruido": round(b["litros"], 1),
        "nf_chaves": (",".join(x.get("chave") or "" for x in match["nfs"]) if match else None),
        "nf_numeros": (",".join(str(x.get("numero") or "") for x in match["nfs"]) if match else None),
        "qcom_total": (match["qtot"] if match else None),
        "diferenca": (match["dif"] if match else None),
        "confianca": (match["conf"] if match else "sem_nf"),
        "status": "candidato", "atualizado_em": _iso(agora),
    }


_CAMPOS_CMP = ("descarga_ini", "descarga_fim", "volume_salto", "volume_vendas", "nf_chaves", "confianca", "diferenca")


def _igual(a, b):
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 0.05
    except (TypeError, ValueError):
        pass
    try:
        return _t(a) == _t(b)
    except Exception:
        return str(a) == str(b)


def planejar(emp_id, blocos_, existentes, fuel_do_tanque, match_de, agora):
    """Decide o que gravar, sem gravar: [(acao, bloco, reg, linha)], acao em
    'ja_feita' (linha encostada ja' saiu de 'candidato': nao mexe e nao cria outra),
    'igual', 'patch' (corrige a linha existente, inclusive o descarga_ini antigo) e 'post'.
    Cada linha existente serve a um bloco so'."""
    plano, tomadas = [], set()
    for b in sorted(blocos_, key=lambda b: b["ini"]):
        viz = [e for e in existentes if encosta(e, b)]
        feita = [e for e in viz if e.get("status") not in (None, "candidato")]
        if feita:
            plano.append(("ja_feita", b, None, feita[0]))
            continue
        reg = registro(emp_id, b, fuel_do_tanque.get(b["tanque"]), match_de.get(id(b)), agora)
        # a que ja' tem o mesmo descarga_ini primeiro (corrigir outra para esse ini daria 409
        # na UNIQUE); depois a mais antiga
        livres = sorted((e for e in viz if e["id"] not in tomadas),
                        key=lambda e: (not _igual(e.get("descarga_ini"), reg["descarga_ini"]), str(e.get("descarga_ini"))))
        if livres:
            alvo = livres[0]; tomadas.add(alvo["id"])
            igual = all(_igual(alvo.get(k), reg.get(k)) for k in _CAMPOS_CMP)
            plano.append(("igual" if igual else "patch", b, reg, alvo))
        else:
            plano.append(("post", b, reg, None))
    return plano


# ------------------------------------------------------------------
# 5. leitura do banco
# ------------------------------------------------------------------
def _entregas(emp_id, desde):
    d = urllib.parse.quote(_iso(desde))
    return _todas("oct_sonda_entregas",
                  f"?empresa_id=eq.{emp_id}&inicio=gte.{d}"
                  f"&select=tanque_numero,inicio,fim,vol_ini,vol_fim,entregue,origem&order=inicio.asc")


def _nfs_combustivel(emp_id, desde):
    linhas = _todas("oct_nfe_manifestadas",
                    f"?empresa_id=eq.{emp_id}&tipo=eq.nfe_completa&emissao=gte.{desde.date().isoformat()}"
                    f"&select=numero,chave_nfe,emitente,emissao,xml,status&order=id.asc", pagina=200)
    # sem a lista de canceladas, nota cancelada poderia ganhar entrada: sem ela, nao casa agora
    from .entrada_auto import chaves_canceladas
    return notas_combustivel(linhas, chaves_canceladas(emp_id))


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
def casar_empresa(emp_id, dias=None, simular=False, agora=None):
    """Casa as descargas da sonda dos ultimos `dias` (DESCARGA_DIAS) com as notas e grava.
    simular=True: nao grava nem da' ciencia -- devolve o plano (ensaio/correcao do historico)."""
    agora = agora or datetime.now(timezone.utc)
    # a ciencia das notas de distribuidora nao depende da sonda: vem antes de tudo
    if not simular:
        try:
            _ciencia_distribuidoras(emp_id)
        except Exception as e:
            print(f"[descarga] {emp_id}: ciencia de distribuidora: {e}")
    try:
        desde = agora - timedelta(days=dias or DESCARGA_DIAS)
        # 1 dia a mais na leitura: bloco que comecou antes de `desde` sai inteiro (e fica de fora)
        ent = _entregas(emp_id, desde - timedelta(days=1))
        if not ent:
            return {"ok": True, "casadas": 0, "obs": "sem registro da sonda"}
        tanques = _rest_get("oct_tanques", f"?empresa_id=eq.{emp_id}&select=numero,combustivel")
        fq = {int(_num(t["numero"])): _fuel_tanque(t["combustivel"]) for t in tanques}
        espera = timedelta(minutes=ESPERA_MIN)
        # so' bloco FECHADO: o compartimento seguinte do nucleo chega ~26 min depois do fim
        # (ate 35 min depois do fim do anterior em set/out) e mudaria o volume do bloco
        bl = [b for b in blocos(entregas_unicas(ent, agora))
              if b["ini"] >= desde and agora - b["fim"] >= espera and fq.get(b["tanque"])]
        de = urllib.parse.quote(_iso(desde - timedelta(days=1)))
        existentes = _todas("oct_nfe_descarga",
                            f"?empresa_id=eq.{emp_id}&descarga_fim=gte.{de}"
                            f"&select=id,tanque_numero,combustivel,descarga_ini,descarga_fim,status,confianca,"
                            f"nf_chaves,diferenca,volume_salto,volume_vendas&order=descarga_ini.asc")
        # nota ja' CONSUMIDA (linha que saiu de 'candidato') nao concorre de novo -- por item:
        # (chave, combustivel). A entrada e' da nota inteira, entao o outro item dela (etanol da
        # mesma nota da gasolina) continua podendo casar com a descarga do outro tanque; o
        # entrada_auto responde "ja importada" e so' fecha a linha, sem entrada em dobro.
        feitas = _rest_get("oct_nfe_descarga",
                           f"?empresa_id=eq.{emp_id}&status=neq.candidato&select=nf_chaves,combustivel&limit=5000")
        usadas = {(c, r.get("combustivel")) for r in feitas for c in (r.get("nf_chaves") or "").split(",") if c}
        nfs = [nf for nf in _nfs_combustivel(emp_id, desde - timedelta(days=DIAS_NF + 1))
               if (nf["chave"], nf["fuel"]) not in usadas]
        fechadas = [e for e in existentes if e.get("status") not in (None, "candidato")]
        abertos = [b for b in bl if not any(encosta(e, b) for e in fechadas)]
        match_de = casar(abertos, nfs, fq)
        plano = planejar(emp_id, bl, existentes, fq, match_de, agora)
        if simular:
            return {"ok": True, "plano": plano, "blocos": bl, "nfs": nfs, "existentes": existentes, "match_de": match_de}
        n_casadas, datas_sem_nf = 0, set()
        for acao, b, reg, linha in plano:
            if acao == "ja_feita":
                continue
            if reg.get("nf_chaves"):
                n_casadas += 1
            else:
                datas_sem_nf.add(b["ini"].date())
            if acao == "patch":
                st, r = _rest("PATCH", f"oct_nfe_descarga?id=eq.{linha['id']}", body=reg, prefer="return=minimal")
            elif acao == "post":
                st, r = _rest("POST", "oct_nfe_descarga", body=reg, prefer="return=minimal")
            else:
                continue
            if st not in (200, 201, 204):
                # nunca mais em silencio: foi um 409 calado que congelou as descargas ate' 17/10
                print(f"[descarga] {emp_id}: {acao} RECUSADO T{b['tanque']} {_iso(b['ini'])}: {st} {str(r)[:160]}")
        # descarga sem nota casada -> da ciencia nas resumo da janela p/ puxar o XML completo
        _ciencia_resumos(emp_id, datas_sem_nf)
        _reciencia_presas(emp_id)
        if n_casadas:
            print(f"[descarga] {emp_id}: {n_casadas} descarga(s) casada(s) com NF")
        return {"ok": True, "casadas": n_casadas, "descargas": len(bl),
                "nfs_combustivel": len(nfs), "entregas": len(ent)}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[descarga] {emp_id}: erro no casamento: {e}\n{tb}")
        return {"ok": False, "erro": str(e), "traceback": tb[-1500:]}
