import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from .cert import extrair_cert_pem, limpar_arquivos
from .assinatura import assinar_xml_evento

URLS_EVENTO = {
    "producao":    "https://www.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
    "homologacao": "https://homologacao.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
}

TIPOS_EVENTO = {
    "210200": "Confirmação da Operação",
    "210210": "Ciência da Operação",
    "210220": "Desconhecimento da Operação",
    "210240": "Operação não Realizada",
}

def registrar_evento(cnpj: str, chave: str, cert_base64: str, cert_senha: str, ambiente: str, tipo: str = "210210"):
    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        tp_amb = 1 if ambiente == "producao" else 2
        dh_evento = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S-00:00")
        desc = TIPOS_EVENTO.get(tipo, "Ciência da Operação")
        c_orgao = "91"  # SEFAZ Nacional

        xml_evento = f"""<eventoCTe versao="1.00" xmlns="http://www.portalfiscal.inf.br/nfe">
  <infEvento Id="ID{tipo}{chave}01">
    <cOrgao>{c_orgao}</cOrgao>
    <tpAmb>{tp_amb}</tpAmb>
    <CNPJ>{cnpj}</CNPJ>
    <chNFe>{chave}</chNFe>
    <dhEvento>{dh_evento}</dhEvento>
    <tpEvento>{tipo}</tpEvento>
    <nSeqEvento>1</nSeqEvento>
    <verEvento>1.00</verEvento>
    <detEvento versao="1.00">
      <descEvento>{desc}</descEvento>
    </detEvento>
  </infEvento>
</eventoCTe>"""

        xml_assinado = assinar_xml_evento(xml_evento, cert_file, key_file)

        soap = f"""<?xml version="1.0" encoding="utf-8"?>
<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <nfeRecepcaoEvento xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4">
      <nfeDadosMsg>{xml_assinado}</nfeDadosMsg>
    </nfeRecepcaoEvento>
  </soap12:Body>
</soap12:Envelope>"""

        url = URLS_EVENTO[ambiente]
        resp = requests.post(url, data=soap.encode("utf-8"),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            cert=(cert_file, key_file), timeout=30)

        root = ET.fromstring(resp.text)
        ns = {"n": "http://www.portalfiscal.inf.br/nfe"}
        cstat = root.findtext(".//n:cStat", namespaces=ns)
        xmotivo = root.findtext(".//n:xMotivo", namespaces=ns)

        return {"cstat": cstat, "xmotivo": xmotivo, "tipo": tipo, "descricao": desc}

    finally:
        limpar_arquivos(cert_file, key_file)
