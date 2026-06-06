"""
sefaz/nfce.py  -  Autorizacao de NFC-e modelo 65 (rota /emitir-nfce)

Reaproveita o maximo de sefaz/emissao.py (modelo 55):
- montar_chave, _det_item, assinar_nfe, _c14n_bytes, UF_CODIGO, NS
A NFC-e (mod 65) difere da NF-e (mod 55) em:
- mod=65; consumidor pode ser nao-identificado (sem <dest> ou so CPF)
- grupo <pag> obrigatorio com a forma de pagamento
- grupo <infNFeSupl> com QR Code (hash SHA-1 calculado com o CSC) + urlChave
- webservice proprio NFCeAutorizacao4 de MG
"""

import re
import hashlib
from datetime import datetime, timezone, timedelta
from lxml import etree
import requests

from .cert import extrair_cert_pem, limpar_arquivos
from .emissao import (
    NS, UF_CODIGO, montar_chave, _det_item, assinar_nfe,
)

# Webservice de Autorizacao da NFC-e SEFAZ-MG (modelo 65) - autorizador proprio.
URLS_AUTORIZACAO_NFCE = {
    "producao":    "https://nfce.fazenda.mg.gov.br/nfce/services/NFeAutorizacao4",
    "homologacao": "https://hnfce.fazenda.mg.gov.br/nfce/services/NFeAutorizacao4",
}
# URL de consulta do QR Code (portal NFC-e MG)
URL_QRCODE = {
    "producao":    "https://portalsped.fazenda.mg.gov.br/portalnfce/sistema/qrcode.xhtml",
    "homologacao": "https://portalsped.fazenda.mg.gov.br/portalnfce/sistema/qrcode.xhtml",
}
# URL que vai no campo urlChave (consulta por chave) - exibida no DANFE
URL_CONSULTA = {
    "producao":    "https://portalsped.fazenda.mg.gov.br/portalnfce",
    "homologacao": "https://portalsped.fazenda.mg.gov.br/portalnfce",
}


def _gerar_qrcode(chave, tp_amb, csc_id, csc, ambiente, dh_emi_hex=None):
    """Monta a URL do QR Code da NFC-e (modelo ONLINE, versao 2 do QR Code).
    Para emissao normal (online), os parametros sao apenas chave/versao/ambiente/idCSC
    + o hash SHA-1 (cHashQRCode) calculado sobre a string desses parametros + CSC.
    """
    base = URL_QRCODE[ambiente]
    versao = "2"
    # parametros (modelo online): chave|versao|ambiente|idCSC
    params = f"chNFe={chave}&nVersao={versao}&tpAmb={tp_amb}&cIdToken={csc_id}"
    # string para hash = params (sem o &cHashQRCode) + CSC
    str_hash = params + csc
    chash = hashlib.sha1(str_hash.encode("utf-8")).hexdigest().upper()
    qr = f"{base}?{params}&cHashQRCode={chash}"
    return qr


def montar_infnfce(nota, empresa, ambiente):
    tp_amb = "1" if ambiente == "producao" else "2"
    emit = empresa
    cuf = UF_CODIGO.get(emit.get("uf", "MG"), "31")

    dh = datetime.now(timezone(timedelta(hours=-3)))
    dh_emi = dh.strftime("%Y-%m-%dT%H:%M:%S-03:00")

    cnpj_emit = re.sub(r"\D", "", emit["cnpj"])
    cnf = nota.get("cnf") or "12345678"
    numero = nota["numero"]
    serie = nota.get("serie", 1)
    modelo = "65"
    tp_emis = "1"

    chave = montar_chave(cuf, dh, cnpj_emit, modelo, serie, numero, tp_emis, cnf)
    cnf_fmt = str(cnf).zfill(8)
    cdv = chave[-1]

    # itens (reusa o _det_item do modelo 55: impostos, combustivel, IBS/CBS)
    crt_emit = str(emit.get("crt", "3"))
    dets = "".join(_det_item(it, i + 1, crt_emit) for i, it in enumerate(nota["itens"]))
    v_prod = sum(float(it["vProd"]) for it in nota["itens"])
    v_icms_mono = sum(
        round(float(it["qCom"]) * float(it.get("aliq_icms_ad_rem") or 0), 2)
        for it in nota["itens"] if str(it.get("cst_icms")) == "61"
    )
    nao_mono = [it for it in nota["itens"] if str(it.get("cst_icms")) != "61"]
    tem_mono = any(str(it.get("cst_icms")) == "61" for it in nota["itens"])
    v_ibs_uf_tot = sum(round(float(it["vProd"]) * 0.10 / 100, 2) for it in nao_mono)
    v_ibs_mun_tot = 0.00
    v_ibs_tot = round(v_ibs_uf_tot + v_ibs_mun_tot, 2)
    v_cbs_tot = sum(round(float(it["vProd"]) * 0.90 / 100, 2) for it in nao_mono)
    v_bc_rt_tot = sum(float(it["vProd"]) for it in nao_mono)

    # ide: NFC-e -> mod 65, tpImp 4 (DANFE NFC-e), indPres 1 (presencial)
    ide = (
        f"<ide><cUF>{cuf}</cUF><cNF>{cnf_fmt}</cNF>"
        f"<natOp>{nota.get('natureza_op','VENDA AO CONSUMIDOR')}</natOp>"
        f"<mod>{modelo}</mod><serie>{serie}</serie><nNF>{numero}</nNF>"
        f"<dhEmi>{dh_emi}</dhEmi><tpNF>1</tpNF><idDest>1</idDest>"
        f"<cMunFG>{emit.get('c_mun','3123205')}</cMunFG>"
        f"<tpImp>4</tpImp><tpEmis>{tp_emis}</tpEmis><cDV>{cdv}</cDV>"
        f"<tpAmb>{tp_amb}</tpAmb><finNFe>1</finNFe><indFinal>1</indFinal>"
        f"<indPres>1</indPres><procEmi>0</procEmi><verProc>Octano1.0</verProc></ide>"
    )
    cep_emit = re.sub(r"\D", "", emit.get("cep", "") or "")
    ie_emit = re.sub(r"\D", "", emit.get("ie", "") or "")
    emit_xml = (
        f"<emit><CNPJ>{cnpj_emit}</CNPJ><xNome>{emit['nome']}</xNome>"
        f"<enderEmit><xLgr>{emit.get('logradouro','')}</xLgr>"
        f"<nro>{emit.get('numero','S/N')}</nro><xBairro>{emit.get('bairro','')}</xBairro>"
        f"<cMun>{emit.get('c_mun','3123205')}</cMun><xMun>{emit.get('municipio','')}</xMun>"
        f"<UF>{emit.get('uf','MG')}</UF><CEP>{cep_emit}</CEP>"
        f"<cPais>1058</cPais><xPais>BRASIL</xPais></enderEmit>"
        f"<IE>{ie_emit}</IE><CRT>{emit.get('crt','3')}</CRT></emit>"
    )

    # destinatario: opcional na NFC-e. So inclui <dest> se houver CPF/CNPJ informado.
    doc_dest = re.sub(r"\D", "", str(nota.get("cpf_consumidor", "") or ""))
    dest_xml = ""
    if doc_dest:
        tag_doc = "CNPJ" if len(doc_dest) == 14 else "CPF"
        dest_xml = f"<dest><{tag_doc}>{doc_dest}</{tag_doc}></dest>"

    tag_mono = f"<vICMSMono>{v_icms_mono:.2f}</vICMSMono>" if v_icms_mono > 0 else ""
    icmstot = (
        f"<ICMSTot><vBC>0.00</vBC><vICMS>0.00</vICMS>"
        f"<vICMSDeson>0.00</vICMSDeson><vFCP>0.00</vFCP><vBCST>0.00</vBCST>"
        f"<vST>0.00</vST><vFCPST>0.00</vFCPST><vFCPSTRet>0.00</vFCPSTRet>"
        f"<vProd>{v_prod:.2f}</vProd><vFrete>0.00</vFrete><vSeg>0.00</vSeg>"
        f"<vDesc>0.00</vDesc><vII>0.00</vII><vIPI>0.00</vIPI><vIPIDevol>0.00</vIPIDevol>"
        f"<vPIS>0.00</vPIS><vCOFINS>0.00</vCOFINS><vOutro>0.00</vOutro>"
        f"<vNF>{v_prod:.2f}</vNF>{tag_mono}<vTotTrib>0.00</vTotTrib></ICMSTot>"
    )
    tag_gmono = (
        "<gMono><vIBSMono>0.00</vIBSMono><vCBSMono>0.00</vCBSMono>"
        "<vIBSMonoReten>0.00</vIBSMonoReten><vCBSMonoReten>0.00</vCBSMonoReten>"
        "<vIBSMonoRet>0.00</vIBSMonoRet><vCBSMonoRet>0.00</vCBSMonoRet></gMono>"
    ) if tem_mono else ""
    ibscbstot = (
        f"<IBSCBSTot><vBCIBSCBS>{v_bc_rt_tot:.2f}</vBCIBSCBS>"
        f"<gIBS>"
        f"<gIBSUF><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vIBSUF>{v_ibs_uf_tot:.2f}</vIBSUF></gIBSUF>"
        f"<gIBSMun><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vIBSMun>{v_ibs_mun_tot:.2f}</vIBSMun></gIBSMun>"
        f"<vIBS>{v_ibs_tot:.2f}</vIBS><vCredPres>0.00</vCredPres><vCredPresCondSus>0.00</vCredPresCondSus>"
        f"</gIBS>"
        f"<gCBS><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vCBS>{v_cbs_tot:.2f}</vCBS>"
        f"<vCredPres>0.00</vCredPres><vCredPresCondSus>0.00</vCredPresCondSus></gCBS>"
        f"{tag_gmono}</IBSCBSTot>"
    )
    total = f"<total>{icmstot}{ibscbstot}</total>"
    transp = f"<transp><modFrete>9</modFrete></transp>"

    # pagamento: tPag (01=dinheiro,02=cheque,03=cartao credito,04=debito,17=PIX...)
    tpag = nota.get("forma_pagamento", "01")
    pag = f"<pag><detPag><tPag>{tpag}</tPag><vPag>{v_prod:.2f}</vPag></detPag></pag>"

    inf = (
        f'<infNFe versao="4.00" Id="NFe{chave}">'
        f"{ide}{emit_xml}{dest_xml}{dets}{total}{transp}{pag}</infNFe>"
    )
    nfe = f'<NFe xmlns="{NS}">{inf}</NFe>'
    return nfe, chave, tp_amb


def emitir_nfce(nota, empresa, cert_base64, cert_senha, csc, csc_id, ambiente="homologacao"):
    """Autoriza uma NFC-e modelo 65 na SEFAZ-MG.
    nota: { numero, serie, itens:[...], cpf_consumidor?, forma_pagamento?, cnf? }
    empresa: dados do emitente (cnpj, ie, nome, c_mun, uf, etc.)
    csc / csc_id: Codigo de Seguranca do Contribuinte e seu id (idToken).
    """
    if not csc or not csc_id:
        return {"ok": False, "etapa": "validacao",
                "erro": "CSC e ID do CSC sao obrigatorios para NFC-e. Configure na empresa."}
    if not nota.get("itens"):
        return {"ok": False, "etapa": "validacao", "erro": "nota.itens vazio"}

    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        nfe_xml, chave, tp_amb = montar_infnfce(nota, empresa, ambiente)

        # assina (mesma tecnica SHA-1 do modelo 55)
        xml_assinada = assinar_nfe(nfe_xml, cert_file, key_file)

        # QR Code + urlChave -> infNFeSupl (inserido DEPOIS da assinatura, ANTES de enviar;
        # infNFeSupl nao e assinado, fica fora do infNFe)
        qr = _gerar_qrcode(chave, tp_amb, csc_id, csc, ambiente)
        url_chave = URL_CONSULTA[ambiente]
        supl = f"<infNFeSupl><qrCode><![CDATA[{qr}]]></qrCode><urlChave>{url_chave}</urlChave></infNFeSupl>"
        # insere o infNFeSupl logo apos </infNFe> dentro de <NFe>...</NFe>
        # (a Signature vem depois do infNFeSupl no layout, mas a ordem aceita e:
        #  infNFe, infNFeSupl, Signature). Como assinar_nfe ja inseriu a Signature,
        #  inserimos o supl ANTES da <Signature>.
        xml_final = xml_assinada.replace("<Signature", supl + "<Signature", 1)

        url = URLS_AUTORIZACAO_NFCE[ambiente]
        soap = _soap_autorizacao_nfce(xml_final)
        action = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4/nfeAutorizacaoLote"
        ctype = f'application/soap+xml; charset=utf-8; action="{action}"'
        resp = requests.post(
            url, data=soap.encode("utf-8"),
            headers={"Content-Type": ctype},
            cert=(cert_file, key_file), timeout=60, verify=True,
        )
        if resp.status_code != 200:
            print("ERRO HTTP SEFAZ (nfce):", resp.status_code, "| corpo:", resp.text[:1500])
            return {"ok": False, "etapa": "http", "chave": chave,
                    "status": resp.status_code, "detalhes": resp.text[:800]}

        root = etree.fromstring(resp.content)

        def t(tag):
            el = root.find(f".//{{{NS}}}{tag}")
            return el.text if el is not None else None

        cstat_lote = t("cStat")
        xmotivo = t("xMotivo")
        prot = root.find(f".//{{{NS}}}protNFe")
        cstat_nfe = None
        nprot = None
        nfe_proc = None
        if prot is not None:
            inf_prot = prot.find(f"{{{NS}}}infProt")
            if inf_prot is not None:
                cstat_nfe = inf_prot.findtext(f"{{{NS}}}cStat")
                xmotivo = inf_prot.findtext(f"{{{NS}}}xMotivo") or xmotivo
                nprot = inf_prot.findtext(f"{{{NS}}}nProt")

        autorizado = (cstat_nfe == "100")
        if autorizado and prot is not None:
            prot_xml = etree.tostring(prot, encoding="unicode")
            nfe_proc = (
                f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<nfeProc versao="4.00" xmlns="{NS}">'
                f'{xml_final}{prot_xml}</nfeProc>'
            )

        return {
            "ok": autorizado,
            "etapa": "sefaz",
            "chave": chave,
            "cstat_lote": cstat_lote,
            "cstat_nfe": cstat_nfe,
            "xmotivo": xmotivo,
            "protocolo": nprot,
            "qrcode": qr,
            "xml_assinado": xml_final,
            "nfe_proc": nfe_proc,
        }
    finally:
        limpar_arquivos(cert_file, key_file)


def _soap_autorizacao_nfce(xml_nfe_assinada):
    wsdl_ns = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4"
    enviNFe = (
        f'<enviNFe versao="4.00" xmlns="{NS}">'
        f'<idLote>1</idLote><indSinc>1</indSinc>'
        f'{xml_nfe_assinada}</enviNFe>'
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope '
        'xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"><soap12:Body>'
        f'<nfeDadosMsg xmlns="{wsdl_ns}">{enviNFe}</nfeDadosMsg>'
        '</soap12:Body></soap12:Envelope>'
    )
