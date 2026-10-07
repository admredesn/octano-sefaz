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

CUSTO do combustivel (oct_produtos.preco_custo): quem grava e' so' atualizar_custos(),
no fim de cada ciclo -- custo por litro da ULTIMA nota de compra que chegou na
manifestacao, com o ICMS-ST. Ver a docstring da funcao.
"""

import os
import re
import json
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from .empresa_cert import carregar_empresa, _rest_get, _supabase_conf

NS = {"n": "http://www.portalfiscal.inf.br/nfe"}
ANP_TANQUE = ("320102", "810101", "820101")     # gasolina, etanol, diesel
CUSTO_DIAS = int(os.environ.get("CUSTO_DIAS", "90"))


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
    # data LOCAL do posto. Era UTC: depois das 21h (00h UTC) a entrada caia no
    # dia seguinte e o LMC do dia acusava sobra/falta de milhares de litros.
    return datetime.now().date().isoformat()


def _data_descarga(cab):
    """Dia em que o combustivel ENTROU no tanque, para o LMC.

    Nao e' a data em que o robo processou a nota: a nota costuma ser
    manifestada/baixada no dia seguinte a' descarga, e o LMC e' diario. Media
    de 21/08 no Tijuco: a sonda viu +5.000 L de gasolina no dia 20, a entrada
    foi lancada no dia 21 -> o livro acusou +5.015 L de sobra num dia e
    -5.007 L de falta no outro, com o mes fechando certo. O mesmo padrao
    aparece nos tres postos, em todo tanque que recebeu descarga.

    Ordem: saida da mercadoria (dhSaiEnt) > emissao (dhEmi) > hoje.
    """
    for campo in ("dhSaiEnt", "dhEmi"):
        v = (cab or {}).get(campo)
        if v and len(str(v)) >= 10:
            d = str(v)[:10]
            if d[4] == "-" and d[7] == "-":
                return d
    return _hoje()


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
        "dhSaiEnt": _txt(root, ".//n:ide/n:dhSaiEnt"),
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
        "finNFe": _txt(root, ".//n:ide/n:finNFe"), "tpNF": _txt(root, ".//n:ide/n:tpNF"),
        "destCnpj": _txt(root, ".//n:dest/n:CNPJ"),
        "cfopCapa": None,
    }
    itens = []
    for det in root.findall(".//n:det", NS):
        prod = det.find("n:prod", NS)
        if prod is None:
            continue
        anp = _txt(prod, "n:comb/n:cProdANP")
        if not anp.startswith(ANP_TANQUE):
            continue  # so combustivel de tanque
        imp = det.find("n:imposto", NS)
        icms = imp.find(".//n:ICMS/*", NS) if imp is not None else None
        ipi = imp.find(".//n:IPI/*", NS) if imp is not None else None
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
            # o que entra no CUSTO alem do vProd (ver _custo_item)
            "vDesc": _f(_txt(prod, "n:vDesc")), "vFrete": _f(_txt(prod, "n:vFrete")),
            "vSeg": _f(_txt(prod, "n:vSeg")), "vOutro": _f(_txt(prod, "n:vOutro")),
            "vICMSST": _f(_txt(icms, "n:vICMSST")), "vFCPST": _f(_txt(icms, "n:vFCPST")),
            "vIPI": _f(_txt(ipi, "n:vIPI")),
        })
    if itens:
        cab["cfopCapa"] = itens[0]["cfop"]
    return cab, itens


# ------------------------------------------------------------------
# CUSTO do combustivel
# ------------------------------------------------------------------
def _total_item(it):
    """Quanto o posto PAGA pelo item: e' a parcela dele no valor da nota (vNF).
    Conferido em 07/10/2026 nas 66 notas de combustivel dos 4 postos desde
    setembro: a soma dos itens por esta conta fecha com o vNF em todas."""
    return (it["vProd"] - it.get("vDesc", 0.0) + it.get("vFrete", 0.0) + it.get("vSeg", 0.0)
            + it.get("vOutro", 0.0) + it.get("vICMSST", 0.0) + it.get("vFCPST", 0.0) + it.get("vIPI", 0.0))


def _custo_item(it):
    """Custo por litro do item. Era o vUnCom puro, que deixa de fora o ICMS-ST:
    gasolina e diesel sao monofasicos (CST 61, o imposto ja' esta' no preco),
    mas o ETANOL vem com ST destacada (CST 10) e cobrada por fora -- a NF 373512
    do Florestal traz 3,2766/L de unitario e custa 3,3918/L (R$ 576,03 de ST em
    5.000 L). O custo sem ST inflava a margem do etanol em ~R$ 0,12/L."""
    q = it.get("qCom") or 0.0
    return round(_total_item(it) / q, 4) if q > 0 else 0.0


def _cfop_de_compra(cfop):
    """So' VENDA do fornecedor vira custo. Fica fora remessa, bonificacao,
    transferencia, armazenagem e devolucao, que vem com preco que nao e' de compra."""
    c = str(cfop or "")
    return len(c) == 4 and c[0] in "56" and c[1:] in (
        "651", "652", "653", "654", "655", "656", "101", "102", "401", "403", "405")


def chaves_canceladas(emp_id):
    """Chaves de NF-e com evento de CANCELAMENTO guardado (110111/110112).

    O evento fica em oct_nfe_manifestadas com status='evento' e SEM a chave na
    coluna chave_nfe: ela so' existe dentro do XML do evento (<chNFe>). E a nota
    cancelada continua na lista com o status que tinha -- a NF 14694 da SETTA
    (etanol, 04/09) esta' cancelada e aparece como 'sem_manifestacao'.
    Levanta erro se a consulta falhar: melhor nao atualizar custo nenhum do que
    tirar custo de nota cancelada."""
    out = set()
    for tp in ("110111", "110112"):
        pad = urllib.parse.quote(f"<tpEvento>{tp}</tpEvento>")
        evs = _rest_get("oct_nfe_manifestadas",
                        f"?empresa_id=eq.{emp_id}&status=eq.evento&xml=like.*{pad}*&select=xml&limit=1000")
        for e in evs or []:
            m = re.search(r"<chNFe>(\d{44})</chNFe>", e.get("xml") or "")
            if m:
                out.add(m.group(1))
    return out


_CUSTO_NOTA = {}      # id da linha -> {"dhEmi", "custos": {anp: custo}} | None (nota nao serve)
_CUSTO_AVISADO = set()
_CNPJ_EMP = {}


def _cnpj_empresa(emp_id):
    if emp_id not in _CNPJ_EMP:
        e = _rest_get("oct_empresas", f"?id=eq.{emp_id}&select=cnpj&limit=1")
        _CNPJ_EMP[emp_id] = "".join(c for c in str((e or [{}])[0].get("cnpj") or "") if c.isdigit())
    return _CNPJ_EMP[emp_id]


def _custos_da_nota(xml, cnpj_emp):
    """{anp: custo por litro} de UMA nota, ou None se ela nao serve de custo:
    nao e' nota normal de saida do fornecedor para ESTE posto, ou nao tem item
    de combustivel com CFOP de venda. Dois itens do mesmo combustivel (dois
    compartimentos do caminhao) viram a media ponderada."""
    try:
        cab, itens = _parse_nfe(xml or "")
    except Exception:
        return None
    if cab.get("finNFe") != "1" or cab.get("tpNF") != "1":
        return None                      # complementar, ajuste, devolucao ou nota de entrada
    if cnpj_emp and cab.get("destCnpj") and cab["destCnpj"] != cnpj_emp:
        return None
    tot, lit = {}, {}
    for it in itens:
        if not _cfop_de_compra(it["cfop"]) or it["qCom"] <= 0:
            continue
        tot[it["codAnp"]] = tot.get(it["codAnp"], 0.0) + _total_item(it)
        lit[it["codAnp"]] = lit.get(it["codAnp"], 0.0) + it["qCom"]
    custos = {a: round(tot[a] / lit[a], 4) for a in tot if lit[a] > 0 and tot[a] > 0}
    return {"dhEmi": cab.get("dhEmi") or "", "custos": custos} if custos else None


def atualizar_custos(emp_id, simular=False):
    """oct_produtos.preco_custo do combustivel = custo por litro (com ST) da
    ULTIMA nota de compra que chegou na manifestacao. Devolve o que mudou.

    Antes o custo so' era gravado quando a nota virava ENTRADA, e nota so' vira
    entrada quando o detector casa a descarga na sonda. Em 07/10/2026, dos 17
    itens de combustivel comprados pelo Florestal desde setembro, 10 nunca
    viraram entrada: a gasolina estava com o custo de 30/09 (duas notas depois),
    o diesel S500 com o de 17/09 (5,538, contra 5,7609 de 24/09). No AC eram 13
    de 27. Custo e' preco de reposicao -- nao depende de o livro ter dado a entrada.

    Vale a nota mais nova (por emissao) de cada combustivel que: tem o XML
    completo, nao foi cancelada, e' venda normal para este posto. So' mexe no
    produto ligado a tanque. Custo fora de 0,5x a 2x do atual nao e' gravado
    (erro de digitacao na nota), so' avisado no log.
    """
    prods = _rest_get("oct_produtos",
                      f"?empresa_id=eq.{emp_id}&tanque_id=not.is.null&select=id,nome,cod_anp,preco_custo")
    alvo = {}
    for p in prods or []:
        anp = str(p.get("cod_anp") or "")
        if anp.startswith(ANP_TANQUE):
            alvo.setdefault(anp, []).append(p)
    if not alvo:
        return []
    desde = (datetime.now(timezone.utc) - timedelta(days=CUSTO_DIAS)).date().isoformat()
    notas = _rest_get("oct_nfe_manifestadas",
                      f"?empresa_id=eq.{emp_id}&tipo=eq.nfe_completa&status=neq.evento&emissao=gte.{desde}"
                      f"&select=id,chave_nfe,numero,emissao&order=emissao.desc&limit=500")
    if not notas:
        return []
    canceladas = chaves_canceladas(emp_id)
    notas = [n for n in notas if n.get("chave_nfe") not in canceladas]
    cnpj = _cnpj_empresa(emp_id)

    achado, i = {}, 0
    while i < len(notas) and len(achado) < len(alvo):
        lote = notas[i:i + 12]
        i += 12
        novas = [n["id"] for n in lote if n["id"] not in _CUSTO_NOTA]
        if novas:      # o XML de nota completa nao muda: le uma vez por processo
            for x in _rest_get("oct_nfe_manifestadas", f"?id=in.({','.join(novas)})&select=id,xml") or []:
                _CUSTO_NOTA[x["id"]] = _custos_da_nota(x.get("xml"), cnpj)
        for n in lote:                       # ja' vem da mais nova para a mais velha
            info = _CUSTO_NOTA.get(n["id"])
            for anp, custo in ((info or {}).get("custos") or {}).items():
                if anp in alvo and anp not in achado:
                    achado[anp] = (custo, n.get("numero"), (info.get("dhEmi") or str(n.get("emissao") or ""))[:10])

    mudou = []
    for anp, (custo, numero, em) in sorted(achado.items()):
        for p in alvo[anp]:
            atual = _f(p.get("preco_custo"))
            if abs(atual - custo) < 0.00005:
                continue
            if atual > 0 and not (0.5 * atual <= custo <= 2.0 * atual):
                if (p["id"], numero) not in _CUSTO_AVISADO:
                    _CUSTO_AVISADO.add((p["id"], numero))
                    print(f"[custo] {emp_id}: {p.get('nome')} NF {numero}: custo {custo} fora da faixa "
                          f"(atual {atual}) - NAO gravado, conferir a nota")
                continue
            if not simular:
                st, r = _rest("PATCH", f"oct_produtos?id=eq.{p['id']}", body={"preco_custo": custo},
                              prefer="return=minimal")
                if st not in (200, 204):
                    print(f"[custo] {emp_id}: falha ao gravar {p.get('nome')}: {st} {r}")
                    continue
            print(f"[custo] {emp_id}: {p.get('nome')} {atual} -> {custo} (NF {numero} de {em})"
                  + (" [simulacao]" if simular else ""))
            mudou.append({"produto_id": p["id"], "nome": p.get("nome"), "cod_anp": anp, "de": atual,
                          "para": custo, "nf": numero, "emissao": em})
    return mudou


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


def _produto_do_item(emp_id, tanque_desc, it):
    """Acha (produto_id, tanque_id) do ITEM pelo codigo ANP; se nao existe, CADASTRA.

    O tanque da descarga e' so' o palpite inicial: uma mesma NF pode trazer dois
    combustiveis (gasolina + diesel na mesma carreta) e cada item tem de cair no
    SEU tanque. Casar pelo tanque da nota jogava o 2o item no produto do 1o.

    NAO grava mais o custo do produto que ja' existe: a entrada pode ser de uma
    nota ANTIGA (descarga casada com atraso) e voltava o custo para tras. Quem
    grava o custo e' atualizar_custos(), sempre pela ultima nota.
    """
    anp = str(it.get("codAnp") or "")
    try:
        if anp:
            pr = _rest_get("oct_produtos",
                           f"?empresa_id=eq.{emp_id}&cod_anp=eq.{anp}&select=id,tanque_id")
            # prefere o produto que ja tem tanque; entre esses, o tanque da descarga
            comtq = [x for x in (pr or []) if x.get("tanque_id")]
            if comtq:
                esc = next((x for x in comtq if x["tanque_id"] == tanque_desc), comtq[0])
                return esc["id"], esc["tanque_id"]
            if pr:
                _rest("PATCH", f"oct_produtos?id=eq.{pr[0]['id']}",
                      body={"tanque_id": tanque_desc}, prefer="return=minimal")
                return pr[0]["id"], tanque_desc
        # ANP desconhecido: so' reaproveita o produto do tanque se o ANP bater ou
        # estiver vazio -- reaproveitar produto de outro combustivel foi o bug.
        pt = _rest_get("oct_produtos",
                       f"?empresa_id=eq.{emp_id}&tanque_id=eq.{tanque_desc}&select=id,cod_anp&limit=1")
        if pt and str(pt[0].get("cod_anp") or "") in ("", anp):
            return pt[0]["id"], tanque_desc
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
        "cfop": it["cfop"] or None, "preco_custo": _custo_item(it), "tanque_id": tanque_desc,
        "estoque": 0, "ativo": True, **perfil})
    return (r[0]["id"] if isinstance(r, list) and r else None), tanque_desc


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
        "emissao": cab["dhEmi"] or None, "entrada": _data_descarga(cab), "fornecedor_id": forn, "natureza": cab["natOp"],
        "cfop": cab["cfopCapa"], "valor_total": cab["vNF"], "valor_icms": cab["vICMS"], "valor_pis": cab["vPIS"],
        "valor_cofins": cab["vCOFINS"], "valor_frete": cab["vFrete"], "valor_desconto": cab["vDesc"],
        "status": "importada", "xml_completo": nota["xml"], "n_prot": cab["nProt"] or None})
    if not (isinstance(nfe, list) and nfe):
        return False, f"falha oct_nfe_entrada: {nfe}"
    nfe_id = nfe[0]["id"]

    # 4) itens + estoque
    tanque_desc = cand.get("_tanque_id")
    for it in itens:
        produto_id, tanque_id = _produto_do_item(emp_id, tanque_desc, it)
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
                cap = _f(tq[0].get("capacidade")) or 0.0
                novo = ant + it["qCom"]
                # NAO capar mais o estoque na capacidade. O `min(ant+q, cap)`
                # descartava os litros excedentes EM SILENCIO: em 21/08 o Tijuco
                # perdeu 4.939 L de gasolina assim. E o excesso nunca significa
                # que o caminhao trouxe demais — significa que o estoque estava
                # inflado (so' somava entrada, nunca baixava venda). Grava o
                # valor real e deixa registrado quando passa do teto.
                obs = f"NF-e {cab['numero']}/{cab['serie']} - {cab['emitNome']} (auto)"
                if cap and novo > cap + 0.5:
                    obs += f" [ATENCAO: estoque calculado {novo:.0f} L acima da capacidade {cap:.0f} L - conferir a sonda]"
                _rest("PATCH", f"oct_tanques?id=eq.{tanque_id}", body={"estoque_atual": novo}, prefer="return=minimal")
                _rest("POST", "oct_lmc", body={
                    "empresa_id": emp_id, "tanque_id": tanque_id, "data": _data_descarga(cab),
                    "saldo_anterior": ant, "entrada": it["qCom"], "saldo_final": novo,
                    "observacoes": obs}, prefer="return=minimal")

    _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{nota['id']}", body={"status": "importada"}, prefer="return=minimal")
    return True, f"entrada OK NF {cab['numero']}"


# ------------------------------------------------------------------
# processa uma empresa
# ------------------------------------------------------------------
def processar_empresa(emp_id):
    if not _habilitado(emp_id):
        return 0
    feitas = _entradas(emp_id)
    # o custo roda SEMPRE, com ou sem entrada no ciclo (ver atualizar_custos)
    try:
        atualizar_custos(emp_id)
    except Exception as e:
        print(f"[custo] {emp_id}: {e}")
    return feitas


def _entradas(emp_id):
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
