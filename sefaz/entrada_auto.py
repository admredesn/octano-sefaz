"""
sefaz/entrada_auto.py  -  Fase 3: entrada automatica da NF de combustivel.

Para cada candidato 'alta' em oct_nfe_descarga (status='candidato'), replica a entrada
que o retaguarda faz manualmente:
  1) manifesta CIENCIA (210210) na(s) nota(s);
  2) resolve o FORNECEDOR (oct_pessoas por CNPJ; cria se falta);
  3) cria a ENTRADA FISCAL (oct_nfe_entrada + oct_nfe_entrada_itens + oct_produto_nfe);
  4) da ENTRADA no ESTOQUE: combustivel -> oct_tanques.estoque_atual += qCom + oct_lmc;
     produto do tanque: vincula (existe) ou CADASTRA (falta, usando o titulo da NF);
  5) marca oct_nfe_manifestadas.status='importada' e oct_nfe_descarga.status='entrada_feita'.

TRAVA: env ENTRADA_AUTO = lista de empresa_id habilitados (ou 'all', ou vazio=DESLIGADO).
Assim liga primeiro num posto de teste (ex.: so Tijuco) antes de soltar em todos.
Idempotente: nao reprocessa 'entrada_feita'; nao duplica oct_nfe_entrada (checa chave).
"""

import os
import json
import unicodedata
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from .empresa_cert import carregar_empresa, _rest_get, _supabase_conf

NS = {"n": "http://www.portalfiscal.inf.br/nfe"}


def _habilitado(emp_id):
    v = os.environ.get("ENTRADA_AUTO", "").strip().lower()
    if not v:
        return False
    if v in ("all", "todos", "1", "true"):
        return True
    return str(emp_id).lower() in [x.strip() for x in v.split(",")]


def _rest(method, path, body=None, prefer="return=representation"):
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
        return e.code, e.read().decode("utf-8", "ignore")[:300]
    except Exception as e:
        return 0, str(e)


def _norm(s):
    return unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _hoje():
    return datetime.now(timezone.utc).date().isoformat()


# ------------------------------------------------------------------
# parse do XML da NF-e -> cabecalho + itens de combustivel
# ------------------------------------------------------------------
def _txt(el, path):
    return (el.findtext(path, default="", namespaces=NS) or "").strip() if el is not None else ""


def _parse_nfe(xml):
    root = ET.fromstring(xml)
    inf = root.find(".//n:infNFe", NS)
    chave = ""
    if inf is not None and inf.get("Id"):
        chave = inf.get("Id").replace("NFe", "")
    emit = root.find(".//n:emit", NS)
    ender = root.find(".//n:emit/n:enderEmit", NS)
    tot = root.find(".//n:total/n:ICMSTot", NS)
    cab = {
        "chave": chave,
        "numero": _txt(root, ".//n:ide/n:nNF"),
        "serie": _txt(root, ".//n:ide/n:serie"),
        "natOp": _txt(root, ".//n:ide/n:natOp"),
        "dhEmi": _txt(root, ".//n:ide/n:dhEmi"),
        "emitCnpj": _txt(emit, "n:CNPJ"),
        "emitNome": _txt(emit, "n:xNome"),
        "emitIE": _txt(emit, "n:IE"),
        "emitLgr": _txt(ender, "n:xLgr"), "emitNro": _txt(ender, "n:nro"),
        "emitBairro": _txt(ender, "n:xBairro"), "emitMun": _txt(ender, "n:xMun"),
        "emitUF": _txt(ender, "n:UF"), "emitCEP": _txt(ender, "n:CEP"), "emitFone": _txt(ender, "n:fone"),
        "vNF": _f(_txt(tot, "n:vNF")), "vICMS": _f(_txt(tot, "n:vICMS")),
        "vPIS": _f(_txt(tot, "n:vPIS")), "vCOFINS": _f(_txt(tot, "n:vCOFINS")),
        "vFrete": _f(_txt(tot, "n:vFrete")), "vDesc": _f(_txt(tot, "n:vDesc")),
        "nProt": _txt(root, ".//n:protNFe/n:infProt/n:nProt"),
        "cfopCapa": None,
    }
    itens = []
    for det in root.findall(".//n:det", NS):
        prod = det.find("n:prod", NS)
        if prod is None:
            continue
        anp = _txt(prod, "n:comb/n:cProdANP")
        if not anp.startswith(("320102", "810101", "820101")):
            continue  # so combustivel de tanque
        imp = det.find("n:imposto", NS)
        icms = imp.find(".//n:ICMS/*", NS) if imp is not None else None
        pis = imp.find(".//n:PIS/*", NS) if imp is not None else None
        cof = imp.find(".//n:COFINS/*", NS) if imp is not None else None
        itens.append({
            "codigo": _txt(prod, "n:cProd"), "descricao": _txt(prod, "n:xProd"),
            "ncm": _txt(prod, "n:NCM"), "cest": _txt(prod, "n:CEST"), "cfop": _txt(prod, "n:CFOP"),
            "unidade": _txt(prod, "n:uCom") or "LTS", "qCom": _f(_txt(prod, "n:qCom")),
            "vUnCom": _f(_txt(prod, "n:vUnCom")), "vProd": _f(_txt(prod, "n:vProd")),
            "codAnp": anp, "descAnp": _txt(prod, "n:comb/n:descANP"), "pBio": _f(_txt(prod, "n:comb/n:pBio")),
            "cstIcms": _txt(icms, "n:CST") or _txt(icms, "n:CSOSN"), "aliqIcms": _f(_txt(icms, "n:pICMS")),
            "cstPis": _txt(pis, "n:CST"), "aliqPis": _f(_txt(pis, "n:pPIS")),
            "cstCofins": _txt(cof, "n:CST"), "aliqCofins": _f(_txt(cof, "n:pCOFINS")),
            "adRem": _f(_txt(icms, "n:adRemICMS")),
            "vICMSMonoRet": _f(_txt(icms, "n:vICMSMonoRet")), "qBCMonoRet": _f(_txt(icms, "n:qBCMonoRet")),
        })
    if itens:
        cab["cfopCapa"] = itens[0]["cfop"]
    return cab, itens


# ------------------------------------------------------------------
# fornecedor / produto / estoque
# ------------------------------------------------------------------
def _fornecedor(emp_id, cab):
    cnpj = cab["emitCnpj"]
    if not cnpj:
        return None
    try:
        f = _rest_get("oct_pessoas", f"?empresa_id=eq.{emp_id}&documento=eq.{cnpj}&select=id&limit=1")
        if f:
            return f[0]["id"]
    except Exception:
        pass
    end = " - ".join(x for x in ([cab["emitLgr"], cab["emitNro"], cab["emitBairro"]]) if x) or None
    st, r = _rest("POST", "oct_pessoas", body={
        "empresa_id": emp_id, "nome": cab["emitNome"], "tipo": "fornecedor", "documento": cnpj,
        "ie": cab["emitIE"] or None, "endereco": end, "cidade": cab["emitMun"] or None,
        "uf": cab["emitUF"] or None, "telefone": cab["emitFone"] or None, "ativo": True})
    return r[0]["id"] if isinstance(r, list) and r else None


def _produto_do_tanque(emp_id, tanque_id, it):
    """Acha o produto ligado ao tanque; se nao existe, CADASTRA (titulo da NF)."""
    try:
        pr = _rest_get("oct_produtos", f"?empresa_id=eq.{emp_id}&tanque_id=eq.{tanque_id}&select=id&limit=1")
        if pr:
            _rest("PATCH", f"oct_produtos?id=eq.{pr[0]['id']}",
                  body={"preco_custo": it["vUnCom"]}, prefer="return=minimal")
            return pr[0]["id"]
    except Exception:
        pass
    perfil = {
        "ind_combustivel": "S", "ind_monofasico": "S", "cod_anp": it["codAnp"], "desc_anp": it["descAnp"] or None,
        "cest": it["cest"] or None, "origem": "0", "cst_icms": it["cstIcms"] or None, "aliq_icms": it["aliqIcms"],
        "aliq_icms_ad_rem": it["adRem"], "cst_pis": it["cstPis"] or None, "aliq_pis": it["aliqPis"],
        "cst_cofins": it["cstCofins"] or None, "aliq_cofins": it["aliqCofins"], "perc_bio": it["pBio"],
    }
    st, r = _rest("POST", "oct_produtos", body={
        "empresa_id": emp_id, "nome": it["descricao"], "codigo": it["codigo"] or None,
        "unidade": it["unidade"], "categoria": "combustivel", "ncm": it["ncm"] or None,
        "cfop": it["cfop"] or None, "preco_custo": it["vUnCom"], "tanque_id": tanque_id,
        "estoque": 0, "ativo": True, **perfil})
    return r[0]["id"] if isinstance(r, list) and r else None


# ------------------------------------------------------------------
# entrada de UMA nota (combustivel)
# ------------------------------------------------------------------
def _entrar_nota(emp_id, dados_emp, chave, cand):
    nfr = _rest_get("oct_nfe_manifestadas",
                    f"?empresa_id=eq.{emp_id}&chave_nfe=eq.{chave}&select=id,numero,status,xml&limit=1")
    if not nfr or not nfr[0].get("xml"):
        return False, f"nota {chave[:10]} sem XML"
    nota = nfr[0]
    if nota.get("status") == "importada":
        return True, "ja importada"
    # ja existe entrada p/ essa chave? (idempotencia)
    try:
        ex = _rest_get("oct_nfe_entrada", f"?empresa_id=eq.{emp_id}&chave_nfe=eq.{chave}&select=id&limit=1")
        if ex:
            _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{nota['id']}", body={"status": "importada"}, prefer="return=minimal")
            return True, "entrada ja existia"
    except Exception:
        pass
    cab, itens = _parse_nfe(nota["xml"])
    if not itens:
        return False, "sem item de combustivel"

    # 1) CIENCIA (210210)
    try:
        from .evento import registrar_evento
        cnpj = str((dados_emp.get("empresa") or {}).get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
        registrar_evento(cnpj, chave, dados_emp["cert_base64"], dados_emp["cert_senha"],
                          os.environ.get("DFE_AMBIENTE", "producao"), tipo="210210")
    except Exception as e:
        print(f"[entrada] {emp_id}: ciencia falhou {chave[:10]}: {e}")

    # 2) fornecedor
    forn = _fornecedor(emp_id, cab)

    # 3) cabecalho da entrada
    st, nfe = _rest("POST", "oct_nfe_entrada", body={
        "empresa_id": emp_id, "numero": cab["numero"], "serie": cab["serie"], "chave_nfe": cab["chave"],
        "emissao": cab["dhEmi"] or None, "entrada": _hoje(), "fornecedor_id": forn, "natureza": cab["natOp"],
        "cfop": cab["cfopCapa"], "valor_total": cab["vNF"], "valor_icms": cab["vICMS"], "valor_pis": cab["vPIS"],
        "valor_cofins": cab["vCOFINS"], "valor_frete": cab["vFrete"], "valor_desconto": cab["vDesc"],
        "status": "importada", "xml_completo": nota["xml"], "n_prot": cab["nProt"] or None})
    if not (isinstance(nfe, list) and nfe):
        return False, f"falha oct_nfe_entrada: {nfe}"
    nfe_id = nfe[0]["id"]

    # 4) itens + estoque
    tanque_id = cand.get("_tanque_id")
    for it in itens:
        produto_id = _produto_do_tanque(emp_id, tanque_id, it)
        st, item = _rest("POST", "oct_nfe_entrada_itens", body={
            "nfe_id": nfe_id, "codigo": it["codigo"], "descricao": it["descricao"], "ncm": it["ncm"],
            "cest": it["cest"] or None, "cfop": it["cfop"], "unidade": it["unidade"], "quantidade": it["qCom"],
            "valor_unitario": it["vUnCom"], "valor_total": it["vProd"], "cod_anp": it["codAnp"],
            "desc_anp": it["descAnp"] or None, "perc_bio": it["pBio"], "cst_icms": it["cstIcms"] or None,
            "aliq_icms": it["aliqIcms"], "cst_pis": it["cstPis"] or None, "aliq_pis": it["aliqPis"],
            "cst_cofins": it["cstCofins"] or None, "aliq_cofins": it["aliqCofins"], "produto_id": produto_id})
        item_id = item[0]["id"] if isinstance(item, list) and item else None
        if produto_id and item_id:
            _rest("POST", "oct_produto_nfe",
                  body={"produto_id": produto_id, "nfe_id": nfe_id, "nfe_item_id": item_id, "empresa_id": emp_id},
                  prefer="return=minimal")
        # estoque do TANQUE + LMC
        if tanque_id:
            tq = _rest_get("oct_tanques", f"?id=eq.{tanque_id}&select=estoque_atual,capacidade&limit=1")
            if tq:
                ant = _f(tq[0].get("estoque_atual"))
                cap = _f(tq[0].get("capacidade")) or (ant + it["qCom"])
                novo = min(ant + it["qCom"], cap)
                _rest("PATCH", f"oct_tanques?id=eq.{tanque_id}", body={"estoque_atual": novo}, prefer="return=minimal")
                _rest("POST", "oct_lmc", body={
                    "empresa_id": emp_id, "tanque_id": tanque_id, "data": _hoje(), "saldo_anterior": ant,
                    "entrada": it["qCom"], "saldo_final": novo,
                    "observacoes": f"NF-e {cab['numero']}/{cab['serie']} - {cab['emitNome']} (auto)"}, prefer="return=minimal")

    _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{nota['id']}", body={"status": "importada"}, prefer="return=minimal")
    return True, f"entrada OK NF {cab['numero']}"


# ------------------------------------------------------------------
# processa uma empresa
# ------------------------------------------------------------------
def processar_empresa(emp_id):
    if not _habilitado(emp_id):
        return 0
    try:
        cands = _rest_get("oct_nfe_descarga",
                          f"?empresa_id=eq.{emp_id}&confianca=eq.alta&status=eq.candidato&select=*")
    except Exception:
        return 0
    if not cands:
        return 0
    try:
        dados_emp = carregar_empresa(emp_id)
    except Exception as e:
        print(f"[entrada] {emp_id}: cert nao carregou: {e}")
        return 0
    tanques = {t["numero"]: t["id"] for t in _rest_get("oct_tanques", f"?empresa_id=eq.{emp_id}&select=id,numero")}
    feitas = 0
    for c in cands:
        c["_tanque_id"] = tanques.get(c["tanque_numero"])
        chaves = [x for x in (c.get("nf_chaves") or "").split(",") if x]
        ok_all = True
        for ch in chaves:
            try:
                ok, msg = _entrar_nota(emp_id, dados_emp, ch, c)
            except Exception as e:
                ok, msg = False, str(e)
            print(f"[entrada] {emp_id} desc {c['id'][:8]} NF {ch[:10]}: {msg}")
            ok_all = ok_all and ok
        if chaves and ok_all:
            _rest("PATCH", f"oct_nfe_descarga?id=eq.{c['id']}",
                  body={"status": "entrada_feita", "atualizado_em": datetime.now(timezone.utc).isoformat()},
                  prefer="return=minimal")
            feitas += 1
    if feitas:
        print(f"[entrada] {emp_id}: {feitas} entrada(s) automatica(s) feita(s)")
    return feitas
