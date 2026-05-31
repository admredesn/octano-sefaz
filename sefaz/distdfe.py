import requests
import base64
import gzip
import xml.etree.ElementTree as ET
from datetime import datetime
from .cert import extrair_cert_pem, limpar_arquivos

URLS_DISTDFE = {
    "producao":    "https://www1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
    "homologacao": "https://hom1.nfe.fazenda.gov.br/NFeDistribuicaoDFe/NFeDistribuicaoDFe.asmx",
}

def montar_soap_distdfe(cnpj, cuf, ultimo_nsu, ambiente):
    nsu_fmt = str(ultimo_nsu).zfill(15)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <nfeDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe">
      <nfeDadosMsg>
        <distDFeInt versao="1.01" xmlns="http://www.portalfiscal.inf.br/nfe">
          <tpAmb>{ambiente}</tpAmb>
          <cUFAutor>{cuf}</cUFAutor>
          <CNPJ>{cnpj}</CNPJ>
          <distNSU>
            <ultNSU>{nsu_fmt}</ultNSU>
          </distNSU>
        </distDFeInt>
      </nfeDadosMsg>
    </nfeDistDFeInteresse>
  </soap12:Body>
</soap12:Envelope>"""

def consultar_distdfe(cnpj, cert_base64, cert_senha, ambiente, ultimo_nsu="0"):
    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        tp_amb = 1 if ambiente == "producao" else 2
        cuf = "31"  # MG

        url = URLS_DISTDFE[ambiente]
        soap_body = montar_soap_distdfe(cnpj, cuf, ultimo_nsu, tp_amb)

        resp = requests.post(
            url,
            data=soap_body.encode("utf-8"),
            headers={
                "Content-Type": "application/soap+xml; charset=utf-8",
                "SOAPAction": "http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe/nfeDistDFeInteresse",
            },
            cert=(cert_file, key_file),
            timeout=30,
            verify=True,
        )

        if resp.status_code != 200:
            return {"erro": f"SEFAZ retornou HTTP {resp.status_code}", "detalhes": resp.text[:500]}

        return processar_resposta_distdfe(resp.text)

    finally:
        limpar_arquivos(cert_file, key_file)

def processar_resposta_distdfe(xml_resp):
    try:
        ns = {
            "soap": "http://www.w3.org/2003/05/soap-envelope",
            "nfe":  "http://www.portalfiscal.inf.br/nfe",
        }
        root = ET.fromstring(xml_resp)
        ret = root.find(".//nfe:retDistDFeInt", ns)
        if ret is None:
            return {"erro": "Resposta inesperada da SEFAZ", "xml": xml_resp[:500]}

        cstat = ret.findtext("nfe:cStat", namespaces=ns)
        xmotivo = ret.findtext("nfe:xMotivo", namespaces=ns)
        ultimo_nsu = ret.findtext("nfe:ultNSU", namespaces=ns)
        max_nsu = ret.findtext("nfe:maxNSU", namespaces=ns)

        nfes = []
        for doc in ret.findall(".//nfe:docZip", ns):
            nsu = doc.get("NSU")
            schema = doc.get("schema", "")
            try:
                conteudo_gz = base64.b64decode(doc.text)
                conteudo_xml = gzip.decompress(conteudo_gz).decode("utf-8")
            except:
                conteudo_xml = ""

            nfe_dados = {"nsu": nsu, "schema": schema, "xml": conteudo_xml}

            # Tenta extrair dados do XML completo (procNFe)
            if conteudo_xml and ("procNFe" in schema or "nfeProc" in schema):
                try:
                    nfe_root = ET.fromstring(conteudo_xml)
                    ns_nfe = {"n": "http://www.portalfiscal.inf.br/nfe"}
                    chave = nfe_root.findtext(".//n:chNFe", namespaces=ns_nfe) or ""
                    if not chave:
                        id_val = nfe_root.findtext(".//n:infNFe", namespaces=ns_nfe)
                        if id_val:
                            chave = id_val.replace("NFe", "")
                    nfe_dados.update({
                        "chave":    chave,
                        "numero":   nfe_root.findtext(".//n:nNF", namespaces=ns_nfe),
                        "serie":    nfe_root.findtext(".//n:serie", namespaces=ns_nfe),
                        "emissao":  (nfe_root.findtext(".//n:dhEmi", namespaces=ns_nfe) or "")[:10],
                        "emitente": nfe_root.findtext(".//n:emit/n:xNome", namespaces=ns_nfe),
                        "emitCnpj": nfe_root.findtext(".//n:emit/n:CNPJ", namespaces=ns_nfe),
                        "valor":    nfe_root.findtext(".//n:vNF", namespaces=ns_nfe),
                        "natOp":    nfe_root.findtext(".//n:natOp", namespaces=ns_nfe),
                        "tipo":     "nfe_completa",
                    })
                except:
                    pass

            # Tenta extrair dados do resumo (resNFe)
            elif conteudo_xml and "resNFe" in schema:
                try:
                    res_root = ET.fromstring(conteudo_xml)
                    ns_nfe = {"n": "http://www.portalfiscal.inf.br/nfe"}
                    nfe_dados.update({
                        "chave":    res_root.findtext("n:chNFe", namespaces=ns_nfe),
                        "emissao":  (res_root.findtext("n:dhEmi", namespaces=ns_nfe) or "")[:10],
                        "emitente": res_root.findtext("n:xNome", namespaces=ns_nfe),
                        "emitCnpj": res_root.findtext("n:CNPJ", namespaces=ns_nfe),
                        "valor":    res_root.findtext("n:vNF", namespaces=ns_nfe),
                        "situacao": res_root.findtext("n:cSitNFe", namespaces=ns_nfe),
                        "tipo":     "resumo",
                    })
                except:
                    pass

            nfes.append(nfe_dados)

        # Ordena do mais recente para o mais antigo (maior NSU primeiro)
        nfes.sort(key=lambda x: x.get("nsu", "0"), reverse=True)

        return {
            "cstat": cstat,
            "xmotivo": xmotivo,
            "ultimo_nsu": ultimo_nsu,
            "max_nsu": max_nsu,
            "total": len(nfes),
            "nfes": nfes,
        }
    except Exception as e:
        return {"erro": f"Erro ao processar resposta: {str(e)}", "xml": xml_resp[:500]}

def baixar_xml_nfe(cnpj, cert_base64, cert_senha, ambiente, nsu):
    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        tp_amb = 1 if ambiente == "producao" else 2
        url = URLS_DISTDFE[ambiente]
        soap_body = f"""<?xml version="1.0" encoding="utf-8"?>
<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">
  <soap12:Body>
    <nfeDistDFeInteresse xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeDistribuicaoDFe">
      <nfeDadosMsg>
        <distDFeInt versao="1.01" xmlns="http://www.portalfiscal.inf.br/nfe">
          <tpAmb>{tp_amb}</tpAmb>
          <cUFAutor>31</cUFAutor>
          <CNPJ>{cnpj}</CNPJ>
          <consNSU>
            <NSU>{str(nsu).zfill(15)}</NSU>
          </consNSU>
        </distDFeInt>
      </nfeDadosMsg>
    </nfeDistDFeInteresse>
  </soap12:Body>
</soap12:Envelope>"""

        resp = requests.post(url, data=soap_body.encode("utf-8"),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            cert=(cert_file, key_file), timeout=30)

        return processar_resposta_distdfe(resp.text)
    finally:
        limpar_arquivos(cert_file, key_file)
