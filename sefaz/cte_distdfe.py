"""
sefaz/cte_distdfe.py  -  Distribuicao de DF-e do CT-e (Ambiente Nacional).

Pedido do Ronan (11/10/2026): "construir um buscador de CT-e" para saber o custo
do frete, conferir as transportadoras e ter o CT-e guardado para compor o SPED.

E' o irmao do distdfe.py (NF-e): mesmo certificado A1, mesmo desenho (consulta
por ultNSU, lote com docZip em gzip+base64). Diferencas do CT-e:
  - o servico e' o CTeDistribuicaoDFe (www1.cte.fazenda.gov.br), operacao
    cteDistDFeInteresse, leiaute distDFeInt versao 1.00;
  - NAO existe resumo nem ciencia: o XML ja' vem completo (procCTe) para todo
    ator do CT-e (tomador, remetente, destinatario, expedidor, recebedor);
  - eventos vem como procEventoCTe (cancelamento 110111, prestacao em desacordo
    610110, comprovante de entrega 110180...).

extrair_cte() tira do XML tudo o que o SPED (registros D100/D190) e a
conferencia de frete precisam; nao fala com ninguem (testavel sozinha).
"""
import base64
import gzip
import re
import xml.etree.ElementTree as ET

URLS_CTE_DISTDFE = {
    "producao":    "https://www1.cte.fazenda.gov.br/CTeDistribuicaoDFe/CTeDistribuicaoDFe.asmx",
    "homologacao": "https://hom1.cte.fazenda.gov.br/CTeDistribuicaoDFe/CTeDistribuicaoDFe.asmx",
}
NS = "http://www.portalfiscal.inf.br/cte"
_N = {"c": NS}
SOAP_ACTION = "http://www.portalfiscal.inf.br/cte/wsdl/CTeDistribuicaoDFe/cteDistDFeInteresse"

# tpEvento -> descricao curta (os que interessam a quem recebe o CT-e)
EVENTOS = {
    "110111": "Cancelamento",
    "110110": "Carta de correção",
    "110113": "EPEC",
    "110160": "Registros do multimodal",
    "110170": "Informações da GTV",
    "110180": "Comprovante de entrega",
    "110181": "Cancelamento do comprovante de entrega",
    "110190": "Insucesso na entrega",
    "110191": "Cancelamento do insucesso na entrega",
    "610110": "Prestação de serviço em desacordo",
    "610111": "Cancelamento da prestação em desacordo",
    "310610": "MDF-e autorizado",
    "310611": "MDF-e cancelado",
    "310620": "Registro de passagem",
    "510620": "Registro de passagem automático",
}


def montar_soap(cnpj, cuf, tp_amb, ultimo_nsu="0", nsu_unico=None):
    """Envelope SOAP 1.2. Com nsu_unico consulta UM documento (consNSU); senao pede o lote a partir de ultNSU."""
    if nsu_unico is not None:
        miolo = f"<consNSU><NSU>{str(nsu_unico).zfill(15)}</NSU></consNSU>"
    else:
        miolo = f"<distNSU><ultNSU>{str(ultimo_nsu).zfill(15)}</ultNSU></distNSU>"
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <cteDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/cte/wsdl/CTeDistribuicaoDFe">
      <cteDadosMsg>
        <distDFeInt versao="1.00" xmlns="http://www.portalfiscal.inf.br/cte">
          <tpAmb>{tp_amb}</tpAmb>
          <cUFAutor>{cuf}</cUFAutor>
          <CNPJ>{cnpj}</CNPJ>
          {miolo}
        </distDFeInt>
      </cteDadosMsg>
    </cteDistDFeInteresse>
  </soap12:Body>
</soap12:Envelope>"""


def consultar_cte(cnpj, cert_base64, cert_senha, ambiente, ultimo_nsu="0", cuf="31", nsu_unico=None):
    """Uma chamada ao CTeDistribuicaoDFe. Devolve o dict de processar_resposta() ou {"erro": ...}."""
    import requests
    from .cert import extrair_cert_pem, limpar_arquivos
    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        tp_amb = 1 if ambiente == "producao" else 2
        resp = requests.post(
            URLS_CTE_DISTDFE[ambiente],
            data=montar_soap(cnpj, cuf, tp_amb, ultimo_nsu, nsu_unico).encode("utf-8"),
            headers={"Content-Type": "application/soap+xml; charset=utf-8", "SOAPAction": SOAP_ACTION},
            cert=(cert_file, key_file), timeout=40, verify=True,
        )
        if resp.status_code != 200:
            return {"erro": f"SEFAZ (CT-e) retornou HTTP {resp.status_code}", "detalhes": resp.text[:500]}
        return processar_resposta(resp.text)
    finally:
        limpar_arquivos(cert_file, key_file)


def processar_resposta(xml_resp):
    """retDistDFeInt -> {"cstat","xmotivo","ultimo_nsu","max_nsu","total","docs":[...]} (docs na ordem do NSU)."""
    try:
        root = ET.fromstring(xml_resp)
    except Exception as e:
        return {"erro": f"Resposta da SEFAZ (CT-e) nao e' XML: {e}", "xml": str(xml_resp)[:500]}
    ret = root.find(".//c:retDistDFeInt", _N)
    if ret is None:
        return {"erro": "Resposta inesperada da SEFAZ (CT-e)", "xml": str(xml_resp)[:500]}
    docs = []
    for doc in ret.findall(".//c:docZip", _N):
        schema = doc.get("schema", "") or ""
        try:
            conteudo = gzip.decompress(base64.b64decode(doc.text or "")).decode("utf-8")
        except Exception:
            conteudo = ""
        d = {"nsu": doc.get("NSU"), "schema": schema, "xml": conteudo}
        try:
            if conteudo and ("Evento" in schema or "<eventoCTe" in conteudo or "<procEventoCTe" in conteudo):
                d.update(extrair_evento(conteudo))
            elif conteudo:
                d.update(extrair_cte(conteudo))
        except Exception as e:      # documento estranho nao pode derrubar o lote: guarda o XML cru
            d.update({"tipo": "desconhecido", "erro_leitura": str(e)[:200]})
        docs.append(d)
    docs.sort(key=lambda x: str(x.get("nsu") or "0").zfill(15))
    return {
        "cstat": ret.findtext("c:cStat", namespaces=_N),
        "xmotivo": ret.findtext("c:xMotivo", namespaces=_N),
        "ultimo_nsu": ret.findtext("c:ultNSU", namespaces=_N),
        "max_nsu": ret.findtext("c:maxNSU", namespaces=_N),
        "total": len(docs),
        "docs": docs,
    }


# ---------------------------------------------------------------------------
# leitura do CT-e
# ---------------------------------------------------------------------------
def _t(el, caminho):
    """texto de um caminho com o namespace do CT-e ('ide/nCT'); '' se nao houver."""
    if el is None:
        return ""
    alvo = el.find("/".join("c:" + p for p in caminho.split("/")), _N)
    return (alvo.text or "").strip() if alvo is not None and alvo.text else ""


def _num(txt):
    try:
        return float(txt) if txt not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _doc(el):
    """CNPJ ou CPF de uma parte do CT-e."""
    return _t(el, "CNPJ") or _t(el, "CPF")


_PLACA = re.compile(r"\b([A-Z]{3})[ -]?(\d[A-Z0-9]\d{2})\b")


def _placa_e_motorista(texto):
    """Placa e motorista quando a transportadora escreve nas observacoes (o CT-e 3.00/4.00 nao tem campo proprio)."""
    t = (texto or "").upper()
    m = re.search(r"PLACA\w*\s*[:\-]?\s*([A-Z]{3})[ -]?(\d[A-Z0-9]\d{2})", t) or _PLACA.search(t)
    placa = (m.group(1) + m.group(2)) if m else None
    mm = re.search(r"MOTORISTA\s*[:\-]?\s*(?:\d{11}\s*[-:]?\s*)?([A-ZÀ-Ú][A-ZÀ-Ú .']{4,60})", t)
    motorista = re.split(r"\s{2,}|//|\bCPF\b|\bPLACA\b|\bRG\b|\bCNH\b", mm.group(1))[0].strip(" .-") if mm else None
    return placa, (motorista or None)


def extrair_cte(xml):
    """XML de um CT-e (cteProc, CTe, cteOSProc...) -> dict plano. Pura."""
    root = ET.fromstring(xml)
    inf = root if root.tag.endswith("}infCte") else root.find(".//c:infCte", _N)
    if inf is None:
        raise ValueError("XML sem infCte")
    ide = inf.find("c:ide", _N)
    emit = inf.find("c:emit", _N)
    partes = {p: inf.find("c:" + p, _N) for p in ("rem", "exped", "receb", "dest")}
    chave = (inf.get("Id") or "").replace("CTe", "") or (root.findtext(".//c:protCTe/c:infProt/c:chCTe", namespaces=_N) or "")

    # ---- tomador: toma3 (0 rem, 1 exped, 2 receb, 3 dest) ou toma4/toma (outros, com os dados dele) ----
    cod_toma = _t(ide, "toma3/toma") or _t(ide, "toma4/toma")
    toma_el = None
    if cod_toma in ("0", "1", "2", "3"):
        toma_el = partes[("rem", "exped", "receb", "dest")[int(cod_toma)]]
    elif ide is not None and ide.find("c:toma4", _N) is not None:
        toma_el = ide.find("c:toma4", _N)
    if toma_el is None:                       # CT-e OS (modelo 67): <toma> direto no infCte
        toma_el = inf.find("c:toma", _N)
        cod_toma = cod_toma or ("4" if toma_el is not None else "")

    # ---- ICMS: um unico grupo filho de <ICMS> (ICMS00, ICMS20, ICMS45, ICMS60, ICMS90, ICMSOutraUF, ICMSSN) ----
    icms_pai = inf.find("c:imp/c:ICMS", _N)
    g = icms_pai[0] if icms_pai is not None and len(icms_pai) else None
    grupo = g.tag.split("}")[-1] if g is not None else ""
    icms = {
        "grupo": grupo,
        "cst": _t(g, "CST"),
        "bc": _num(_t(g, "vBC") or _t(g, "vBCOutraUF") or _t(g, "vBCSTRet")),
        "aliq": _num(_t(g, "pICMS") or _t(g, "pICMSOutraUF") or _t(g, "pICMSSTRet")),
        "valor": _num(_t(g, "vICMS") or _t(g, "vICMSOutraUF") or _t(g, "vICMSSTRet")),
        "red_bc": _num(_t(g, "pRedBC") or _t(g, "pRedBCOutraUF")),
    }
    if grupo == "ICMSSN":
        icms["cst"] = icms["cst"] or "90"     # Simples Nacional: sem destaque

    # ---- carga e documentos transportados ----
    norm = inf.find("c:infCTeNorm", _N)
    carga = norm.find("c:infCarga", _N) if norm is not None else None
    qtds = []
    if carga is not None:
        for q in carga.findall("c:infQ", _N):
            qtds.append({"unid": _t(q, "cUnid"), "medida": _t(q, "tpMed"), "qtd": _num(_t(q, "qCarga"))})
    chaves_nfe = []
    for ch in inf.findall(".//c:infDoc/c:infNFe/c:chave", _N):
        if ch.text and ch.text.strip() not in chaves_nfe:
            chaves_nfe.append(ch.text.strip())
    ref = (inf.findtext(".//c:infCteComp/c:chCTe", namespaces=_N) or inf.findtext(".//c:infCteComp/c:chave", namespaces=_N)
           or inf.findtext(".//c:infCteSub/c:chCte", namespaces=_N) or "")

    comps = [{"nome": _t(c, "xNome"), "valor": _num(_t(c, "vComp"))} for c in inf.findall("c:vPrest/c:Comp", _N)]
    obs = " // ".join(x for x in [_t(inf, "compl/xObs")] +
                      [f"{o.get('xCampo')}: {_t(o, 'xTexto')}" for o in inf.findall("c:compl/c:ObsCont", _N)] if x)
    placa, motorista = _placa_e_motorista(obs)
    prot = root.find(".//c:protCTe/c:infProt", _N)

    return {
        "tipo": "cte",
        "chave": chave,
        "modelo": _t(ide, "mod"), "serie": _t(ide, "serie"), "numero": _t(ide, "nCT"),
        "emissao": _t(ide, "dhEmi") or None,
        "tp_cte": _t(ide, "tpCTe"), "tp_serv": _t(ide, "tpServ"), "modal": _t(ide, "modal"),
        "cfop": _t(ide, "CFOP"), "nat_op": _t(ide, "natOp"),
        "emit_cnpj": _doc(emit), "emit_nome": _t(emit, "xNome"), "emit_ie": _t(emit, "IE"),
        "emit_uf": _t(emit, "enderEmit/UF"), "emit_mun": _t(emit, "enderEmit/cMun"),
        "tomador_tipo": cod_toma, "tomador_cnpj": _doc(toma_el), "tomador_nome": _t(toma_el, "xNome"),
        "rem_cnpj": _doc(partes["rem"]), "rem_nome": _t(partes["rem"], "xNome"),
        "dest_cnpj": _doc(partes["dest"]), "dest_nome": _t(partes["dest"], "xNome"),
        "exped_cnpj": _doc(partes["exped"]), "receb_cnpj": _doc(partes["receb"]),
        "mun_ini": _t(ide, "cMunIni"), "mun_ini_nome": _t(ide, "xMunIni"), "uf_ini": _t(ide, "UFIni"),
        "mun_fim": _t(ide, "cMunFim"), "mun_fim_nome": _t(ide, "xMunFim"), "uf_fim": _t(ide, "UFFim"),
        "valor_prestacao": _num(_t(inf, "vPrest/vTPrest")), "valor_receber": _num(_t(inf, "vPrest/vRec")),
        "componentes": comps,
        "icms_grupo": icms["grupo"], "icms_cst": icms["cst"], "icms_bc": icms["bc"], "icms_aliq": icms["aliq"],
        "icms_valor": icms["valor"], "icms_red_bc": icms["red_bc"],
        "valor_carga": _num(_t(carga, "vCarga")), "produto_pred": _t(carga, "proPred"), "qtd_carga": qtds,
        "chaves_nfe": chaves_nfe, "chave_cte_ref": ref or None,
        "rntrc": inf.findtext(".//c:rodo/c:RNTRC", namespaces=_N) or None,
        "obs": obs or None, "placa": placa, "motorista": motorista,
        "protocolo": _t(prot, "nProt") or None, "autorizado_em": _t(prot, "dhRecbto") or None,
        "prot_cstat": _t(prot, "cStat") or None,
    }


def extrair_evento(xml):
    """XML de um evento de CT-e (procEventoCTe / eventoCTe) -> dict plano. Pura."""
    root = ET.fromstring(xml)
    inf = root.find(".//c:eventoCTe/c:infEvento", _N)
    if inf is None:
        inf = root.find(".//c:infEvento", _N)
    if inf is None:
        raise ValueError("XML sem infEvento")
    tp = _t(inf, "tpEvento")
    ret = root.find(".//c:retEventoCTe/c:infEvento", _N)
    cstat = _t(ret, "cStat")
    det = inf.find("c:detEvento", _N)
    desc = ""
    if det is not None:
        for el in det.iter():
            if el.tag.endswith("}descEvento") and el.text:
                desc = el.text.strip(); break
    just = ""
    if det is not None:
        for el in det.iter():
            if el.tag.split("}")[-1] in ("xJust", "xObs") and el.text:
                just = el.text.strip(); break
    return {
        "tipo": "evento",
        "chave": _t(inf, "chCTe"),
        "evento_tipo": tp,
        "evento_descr": desc or EVENTOS.get(tp) or tp,
        "evento_seq": int(_t(inf, "nSeqEvento") or 0) or None,
        "evento_em": _t(inf, "dhEvento") or None,
        "evento_cstat": cstat or None,
        # sem retEvento (evento solto) considera registrado; 135/136 = registrado; 134 = registrado com alerta
        "evento_registrado": (cstat in ("134", "135", "136")) if cstat else True,
        "evento_just": just or None,
        "emit_cnpj": _doc(inf),
    }
