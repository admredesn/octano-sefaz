"""
sefaz/cancelamento.py  -  Cancelamento de NF-e modelo 55 (evento 110111)

Segue EXATAMENTE o mesmo padrao de emissao.py:
- SOAP 1.2 manual com requests + cert=(cert_file, key_file)
- Assinatura SHA-1 manual com cryptography (a NF-e/evento usa SHA-1; o signxml 4.x
  bloqueia SHA-1). A Signature e montada a mao, sem prefixo ds:, referenciando o
  Id do <infEvento>. Reaproveita a tecnica de _c14n_bytes que resolveu o 297.

O cancelamento e um EVENTO: monta-se <envEvento> contendo <evento> -> <infEvento>,
assina-se o <infEvento> (Reference ao Id), e envia-se ao RecepcaoEvento de MG.

Regras SEFAZ:
- tpEvento = 110111 (cancelamento)
- xJust (justificativa) entre 15 e 255 caracteres
- nProt = protocolo de AUTORIZACAO da NF-e original (obrigatorio)
- prazo legal (MG: em geral 24h apos autorizacao para cancelamento normal)
- so cancela NF-e autorizada/transmitida (com chave de 44 digitos e protocolo)
"""

import re
from datetime import datetime, timezone
from lxml import etree
import requests

from .cert import extrair_cert_pem, limpar_arquivos

NS = "http://www.portalfiscal.inf.br/nfe"
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
_C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"  # C14N 1.0 inclusiva

# MG usa autorizador proprio tambem para eventos (confirmado no portal SPED-MG).
URLS_EVENTO = {
    "producao":    "https://nfe.fazenda.mg.gov.br/nfe2/services/NFeRecepcaoEvento4",
    "homologacao": "https://hnfe.fazenda.mg.gov.br/nfe2/services/NFeRecepcaoEvento4",
}

TP_EVENTO_CANCELAMENTO = "110111"


def _c14n_bytes(elem) -> bytes:
    # Mesma tecnica do emissao.py: serializa primeiro (mantem o xmlns do proprio
    # elemento, sem xmlns="" nos filhos) e so entao canonicaliza. Calcular o c14n
    # direto sobre sub-elemento com namespace herdado insere xmlns="" e quebra o
    # digest (rejeicao na validacao da assinatura).
    return etree.canonicalize(etree.tostring(elem).decode("utf-8")).encode("utf-8")


def _assinar_evento(xml_evento: str, cert_file: str, key_file: str) -> str:
    """Assina o <infEvento> (Reference ao Id), enveloped + C14N, RSA-SHA1.
    Mesma implementacao manual da assinar_nfe do emissao.py, trocando infNFe->infEvento."""
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509 import load_pem_x509_certificate
    from cryptography.hazmat.primitives.asymmetric import padding

    root = etree.fromstring(xml_evento.encode("utf-8"))
    # o elemento assinado e o <infEvento> dentro de <evento>
    evento = root.find(f"{{{NS}}}evento")
    inf = evento.find(f"{{{NS}}}infEvento")
    ref_id = inf.get("Id")

    with open(cert_file, "rb") as cf, open(key_file, "rb") as kf:
        cert_pem, key_pem = cf.read(), kf.read()

    private_key = serialization.load_pem_private_key(key_pem, password=None)
    cert = load_pem_x509_certificate(cert_pem)
    cert_der_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()

    def ds(tag):
        return f"{{{DS_NS}}}{tag}"

    signed_info = etree.Element(ds("SignedInfo"), nsmap={None: DS_NS})
    c14n_m = etree.SubElement(signed_info, ds("CanonicalizationMethod"))
    c14n_m.set("Algorithm", _C14N)
    sig_m = etree.SubElement(signed_info, ds("SignatureMethod"))
    sig_m.set("Algorithm", f"{DS_NS}rsa-sha1")
    ref = etree.SubElement(signed_info, ds("Reference"))
    ref.set("URI", f"#{ref_id}")
    transforms = etree.SubElement(ref, ds("Transforms"))
    t1 = etree.SubElement(transforms, ds("Transform"))
    t1.set("Algorithm", f"{DS_NS}enveloped-signature")
    t2 = etree.SubElement(transforms, ds("Transform"))
    t2.set("Algorithm", _C14N)
    dig_m = etree.SubElement(ref, ds("DigestMethod"))
    dig_m.set("Algorithm", f"{DS_NS}sha1")
    dig_v = etree.SubElement(ref, ds("DigestValue"))

    signature = etree.Element(ds("Signature"), nsmap={None: DS_NS})
    signature.append(signed_info)
    sv = etree.SubElement(signature, ds("SignatureValue"))
    key_info = etree.SubElement(signature, ds("KeyInfo"))
    x509_data = etree.SubElement(key_info, ds("X509Data"))
    x509_cert = etree.SubElement(x509_data, ds("X509Certificate"))
    x509_cert.text = cert_der_b64
    # a Signature do evento e irma de <infEvento> (dentro de <evento>)
    evento.append(signature)

    # DigestValue: aplica enveloped (remove a Signature da copia) e canoniza o
    # infEvento no contexto final, depois SHA1.
    evento_tmp = etree.fromstring(etree.tostring(evento))
    sig_tmp = evento_tmp.find(ds("Signature"))
    evento_tmp.remove(sig_tmp)
    inf_tmp = evento_tmp.find(f"{{{NS}}}infEvento")
    inf_c14n = _c14n_bytes(inf_tmp)
    digest = hashes.Hash(hashes.SHA1())
    digest.update(inf_c14n)
    dig_v.text = base64.b64encode(digest.finalize()).decode()

    signed_info_c14n = _c14n_bytes(signed_info)
    assinatura = private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())
    sv.text = base64.b64encode(assinatura).decode()

    return etree.tostring(root, encoding="unicode")


def _soap_evento(xml_envevento_assinado):
    wsdl_ns = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope '
        'xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"><soap12:Body>'
        f'<nfeDadosMsg xmlns="{wsdl_ns}">{xml_envevento_assinado}</nfeDadosMsg>'
        '</soap12:Body></soap12:Envelope>'
    )


def cancelar_nfe(chave, protocolo, justificativa, cnpj, cert_base64, cert_senha, ambiente="homologacao"):
    """Cancela uma NF-e autorizada via evento 110111.

    Parametros:
      chave        - chave de acesso (44 digitos) da NF-e a cancelar
      protocolo    - nProt da AUTORIZACAO original (obrigatorio)
      justificativa- texto entre 15 e 255 caracteres
      cnpj         - CNPJ do emitente (so digitos)
      cert_base64  - certificado A1 em base64
      cert_senha   - senha do certificado
      ambiente     - 'homologacao' ou 'producao'
    """
    # --- validacoes locais (antes de gastar chamada na SEFAZ) ---
    chave = re.sub(r"\D", "", chave or "")
    if len(chave) != 44:
        return {"ok": False, "etapa": "validacao", "erro": "Chave de acesso deve ter 44 digitos."}
    if not protocolo:
        return {"ok": False, "etapa": "validacao", "erro": "Protocolo de autorizacao e obrigatorio para cancelar."}
    just = (justificativa or "").strip()
    if len(just) < 15:
        return {"ok": False, "etapa": "validacao", "erro": "Justificativa deve ter no minimo 15 caracteres."}
    if len(just) > 255:
        return {"ok": False, "etapa": "validacao", "erro": "Justificativa deve ter no maximo 255 caracteres."}
    cnpj = re.sub(r"\D", "", cnpj or "")

    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        tp_amb = "1" if ambiente == "producao" else "2"
        c_orgao = chave[0:2]  # cUF = 2 primeiros digitos da chave (MG=31)
        dh_evento = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        # formata o offset de -0300 para -03:00
        dh_evento = dh_evento[:-2] + ":" + dh_evento[-2:]
        n_seq = "1"
        id_evento = f"ID{TP_EVENTO_CANCELAMENTO}{chave}{n_seq.zfill(2)}"

        # escapa caracteres XML na justificativa
        just_xml = (just.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

        xml_evento = (
            f'<envEvento versao="1.00" xmlns="{NS}">'
            f'<idLote>1</idLote>'
            f'<evento versao="1.00">'
            f'<infEvento Id="{id_evento}">'
            f'<cOrgao>{c_orgao}</cOrgao>'
            f'<tpAmb>{tp_amb}</tpAmb>'
            f'<CNPJ>{cnpj}</CNPJ>'
            f'<chNFe>{chave}</chNFe>'
            f'<dhEvento>{dh_evento}</dhEvento>'
            f'<tpEvento>{TP_EVENTO_CANCELAMENTO}</tpEvento>'
            f'<nSeqEvento>{n_seq}</nSeqEvento>'
            f'<verEvento>1.00</verEvento>'
            f'<detEvento versao="1.00">'
            f'<descEvento>Cancelamento</descEvento>'
            f'<nProt>{protocolo}</nProt>'
            f'<xJust>{just_xml}</xJust>'
            f'</detEvento>'
            f'</infEvento>'
            f'</evento>'
            f'</envEvento>'
        )

        xml_assinado = _assinar_evento(xml_evento, cert_file, key_file)

        url = URLS_EVENTO[ambiente]
        soap = _soap_evento(xml_assinado)
        action = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4/nfeRecepcaoEvento"
        ctype = f'application/soap+xml; charset=utf-8; action="{action}"'
        resp = requests.post(
            url, data=soap.encode("utf-8"),
            headers={"Content-Type": ctype},
            cert=(cert_file, key_file), timeout=60, verify=True,
        )
        if resp.status_code != 200:
            print("ERRO HTTP SEFAZ (evento):", resp.status_code, "| corpo:", resp.text[:1500])
            return {"ok": False, "etapa": "http", "chave": chave,
                    "status": resp.status_code, "detalhes": resp.text[:800]}

        root = etree.fromstring(resp.content)

        def t(tag):
            el = root.find(f".//{{{NS}}}{tag}")
            return el.text if el is not None else None

        # cStat do lote de retorno; o resultado do evento vem em retEvento/infEvento
        cstat_lote = t("cStat")
        xmotivo = t("xMotivo")
        ret = root.find(f".//{{{NS}}}retEvento")
        cstat_evt = None
        n_prot_evt = None
        if ret is not None:
            inf_ret = ret.find(f"{{{NS}}}infEvento")
            if inf_ret is not None:
                cstat_evt = inf_ret.findtext(f"{{{NS}}}cStat")
                xmotivo = inf_ret.findtext(f"{{{NS}}}xMotivo") or xmotivo
                n_prot_evt = inf_ret.findtext(f"{{{NS}}}nProt")

        # 135 = evento registrado e vinculado; 155 = registrado fora de prazo (tb cancela)
        cancelado = cstat_evt in ("135", "155")
        return {
            "ok": cancelado,
            "etapa": "sefaz",
            "chave": chave,
            "cstat_lote": cstat_lote,
            "cstat_evento": cstat_evt,
            "xmotivo": xmotivo,
            "protocolo_cancelamento": n_prot_evt,
            "xml_evento": xml_assinado,
        }
    finally:
        limpar_arquivos(cert_file, key_file)
