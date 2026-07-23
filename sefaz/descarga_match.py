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
    i2 = _iso(ini - timedelta(minutes=10))
    f2 = _iso(fim + timedelta(minutes=10))
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
def _gravar(emp_id, d, fuel, recon, vend, match):
    chave_desc = _iso(d["ini"])
    try:
        ex = _rest_get("oct_nfe_descarga",
                       f"?empresa_id=eq.{emp_id}&tanque_numero=eq.{d['tanque']}"
                       f"&descarga_ini=eq.{chave_desc}&select=id,status&limit=1")
    except Exception:
        ex = []
    if ex and ex[0].get("status") not in (None, "candidato", "sem_nf"):
        return  # ja confirmado/entrada feita -> nao mexe
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
            registrar_evento(cnpj, r["chave_nfe"], dados["cert_base64"], dados["cert_senha"], amb, tipo="210210")
            _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{r['id']}", body={"status": "ciencia"}, prefer="return=minimal")
            print(f"[descarga] {emp_id}: ciencia p/ puxar XML da nota {r['chave_nfe'][:12]} (descarga sem NF)")
        except Exception as e:
            print(f"[descarga] {emp_id}: ciencia falhou {str(r.get('chave_nfe'))[:12]}: {e}")


# ------------------------------------------------------------------
# ENTRADA: casa as descargas de uma empresa
# ------------------------------------------------------------------
def casar_empresa(emp_id):
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
        n_casadas = 0
        n_descargas = 0
        datas_sem_nf = set()
        for t, serie in por_tanque.items():
            fuel = tq.get(t)
            if not fuel:
                continue
            for d in _detectar(sorted(serie, key=lambda x: x[0])):
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
        if n_casadas:
            print(f"[descarga] {emp_id}: {n_casadas} descarga(s) casada(s) com NF")
        return {"ok": True, "casadas": n_casadas, "descargas": n_descargas,
                "nfs_combustivel": len(nfs), "medicoes": len(med)}
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[descarga] {emp_id}: erro no casamento: {e}\n{tb}")
        return {"ok": False, "erro": str(e), "traceback": tb[-1500:]}
