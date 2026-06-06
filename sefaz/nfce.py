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
    NS, UF_CODIGO, montar_chave, assinar_nfe,
)


def _imposto_item_nfce(it):
    """Imposto do item da NFC-e, espelhando EXATAMENTE os cupons autorizados do posto.
    Diferencas vs modelo 55: SEM <IPI>; PIS/COFINS no grupo Aliq (CST 01) com valor real.
    """
    cst = str(it.get("cst_icms") or "").strip()
    orig = it.get("origem", "0")
    vprod = float(it["vProd"])

    if cst == "61":
        # combustivel monofasico
        q = float(it["qCom"]); adrem = float(it.get("aliq_icms_ad_rem") or 0)
        v = round(q * adrem, 2)
        icms = (f"<ICMS><ICMS61><orig>{orig}</orig><CST>61</CST>"
                f"<qBCMonoRet>{q:.4f}</qBCMonoRet><adRemICMSRet>{adrem:.4f}</adRemICMSRet>"
                f"<vICMSMonoRet>{v:.2f}</vICMSMonoRet></ICMS61></ICMS>")
        # PIS/COFINS nao-tributado (combustivel) + IBSCBS monofasico (CST 620)
        pis = "<PIS><PISNT><CST>04</CST></PISNT></PIS>"
        cof = "<COFINS><COFINSNT><CST>04</CST></COFINSNT></COFINS>"
        ibscbs = ("<IBSCBS><CST>620</CST><cClassTrib>620006</cClassTrib>"
                  "<gIBSCBSMono><vTotIBSMonoItem>0.00</vTotIBSMonoItem>"
                  "<vTotCBSMonoItem>0.00</vTotCBSMonoItem></gIBSCBSMono></IBSCBS>")
    else:
        # CST 60 (ICMS-ST ja retido) - espelha o cupom de loja autorizado
        icms = f"<ICMS><ICMS60><orig>{orig}</orig><CST>60</CST></ICMS60></ICMS>"
        # PIS/COFINS no grupo Aliq (CST 01), como no cupom real do lubrificante
        ppis = float(it.get("aliq_pis") or 1.65)
        pcof = float(it.get("aliq_cofins") or 7.60)
        vpis = round(vprod * ppis / 100, 2)
        vcof = round(vprod * pcof / 100, 2)
        pis = (f"<PIS><PISAliq><CST>01</CST><vBC>{vprod:.2f}</vBC>"
               f"<pPIS>{ppis:.4f}</pPIS><vPIS>{vpis:.2f}</vPIS></PISAliq></PIS>")
        cof = (f"<COFINS><COFINSAliq><CST>01</CST><vBC>{vprod:.2f}</vBC>"
               f"<pCOFINS>{pcof:.4f}</pCOFINS><vCOFINS>{vcof:.2f}</vCOFINS></COFINSAliq></COFINS>")
        # IBSCBS regular (CST 000), aliquotas-teste 2026 (IBS-UF 0,10%, CBS 0,90%)
        v_ibs_uf = round(vprod * 0.10 / 100, 2)
        v_cbs = round(vprod * 0.90 / 100, 2)
        ibscbs = (f"<IBSCBS><CST>000</CST><cClassTrib>000001</cClassTrib>"
                  f"<gIBSCBS><vBC>{vprod:.2f}</vBC>"
                  f"<gIBSUF><pIBSUF>0.1000</pIBSUF><vIBSUF>{v_ibs_uf:.2f}</vIBSUF></gIBSUF>"
                  f"<gIBSMun><pIBSMun>0.0000</pIBSMun><vIBSMun>0.00</vIBSMun></gIBSMun>"
                  f"<vIBS>{v_ibs_uf:.2f}</vIBS>"
                  f"<gCBS><pCBS>0.9000</pCBS><vCBS>{v_cbs:.2f}</vCBS></gCBS></gIBSCBS></IBSCBS>")
    return f"<imposto>{icms}{pis}{cof}{ibscbs}</imposto>"


def _det_item_nfce(it, n, cnpj_emit):
    """Item (det) da NFC-e espelhando os cupons autorizados: com <comb> e <CNPJFab>,
    SEM IPI. O posto trata combustivel E lubrificante com grupo <comb> (cProdANP)."""
    cest = it.get("cest")
    tem_anp = bool(it.get("cod_anp"))
    comb = ""
    if tem_anp:
        comb = (f"<comb><cProdANP>{it['cod_anp']}</cProdANP>"
                f"<descANP>{it.get('desc_anp', it['xProd'])[:95]}</descANP>"
                f"<UFCons>{it.get('uf_cons','MG')}</UFCons></comb>")
    prod = (
        f'<det nItem="{n}"><prod>'
        f"<cProd>{it['cProd']}</cProd><cEAN>{it.get('cEAN','SEM GTIN')}</cEAN>"
        f"<xProd>{it['xProd']}</xProd><NCM>{it['ncm']}</NCM>"
        + (f"<CEST>{cest}</CEST><indEscala>N</indEscala>" if cest else "")
        + f"<CNPJFab>{cnpj_emit}</CNPJFab>"
        f"<CFOP>{it['cfop']}</CFOP><uCom>{it['uCom']}</uCom>"
        f"<qCom>{float(it['qCom']):.4f}</qCom><vUnCom>{float(it['vUnCom']):.10f}</vUnCom>"
        f"<vProd>{float(it['vProd']):.2f}</vProd>"
        f"<cEANTrib>{it.get('cEANTrib','SEM GTIN')}</cEANTrib>"
        f"<uTrib>{it.get('uTrib', it['uCom'])}</uTrib>"
        f"<qTrib>{float(it['qCom']):.4f}</qTrib><vUnTrib>{float(it['vUnCom']):.10f}</vUnTrib>"
        f"<indTot>1</indTot>{comb}</prod>"
        + _imposto_item_nfce(it)
        + "</det>"
    )
    return prod

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

    # itens (montagem propria da NFC-e, espelhando os cupons autorizados)
    dets = "".join(_det_item_nfce(it, i + 1, cnpj_emit) for i, it in enumerate(nota["itens"]))
    v_prod = sum(float(it["vProd"]) for it in nota["itens"])
    v_icms_mono = sum(
        round(float(it["qCom"]) * float(it.get("aliq_icms_ad_rem") or 0), 2)
        for it in nota["itens"] if str(it.get("cst_icms")) == "61"
    )
    nao_mono = [it for it in nota["itens"] if str(it.get("cst_icms")) != "61"]
    mono = [it for it in nota["itens"] if str(it.get("cst_icms")) == "61"]
    tem_mono = len(mono) > 0
    v_pis_tot = sum(round(float(it["vProd"]) * float(it.get("aliq_pis") or 1.65) / 100, 2) for it in nao_mono)
    v_cofins_tot = sum(round(float(it["vProd"]) * float(it.get("aliq_cofins") or 7.60) / 100, 2) for it in nao_mono)
    q_bc_mono = sum(float(it["qCom"]) for it in mono)
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
        f"<xFant>{emit.get('nome_fantasia', emit['nome'])}</xFant>"
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

    tag_qbcmono = f"<qBCMonoRet>{q_bc_mono:.2f}</qBCMonoRet>" if q_bc_mono > 0 else ""
    icmstot = (
        f"<ICMSTot><vBC>0.00</vBC><vICMS>0.00</vICMS>"
        f"<vICMSDeson>0.00</vICMSDeson><vFCP>0.00</vFCP><vBCST>0.00</vBCST>"
        f"<vST>0.00</vST><vFCPST>0.00</vFCPST><vFCPSTRet>0.00</vFCPSTRet>"
        f"{tag_qbcmono}"
        f"<vProd>{v_prod:.2f}</vProd><vFrete>0.00</vFrete><vSeg>0.00</vSeg>"
        f"<vDesc>0.00</vDesc><vII>0.00</vII><vIPI>0.00</vIPI><vIPIDevol>0.00</vIPIDevol>"
        f"<vPIS>{v_pis_tot:.2f}</vPIS><vCOFINS>{v_cofins_tot:.2f}</vCOFINS><vOutro>0.00</vOutro>"
        f"<vNF>{v_prod:.2f}</vNF></ICMSTot>"
    )
    # IBSCBSTot espelhando o cupom de loja autorizado
    ibscbstot = (
        f"<IBSCBSTot><vBCIBSCBS>{v_bc_rt_tot:.2f}</vBCIBSCBS>"
        f"<gIBS>"
        f"<gIBSUF><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vIBSUF>{v_ibs_uf_tot:.2f}</vIBSUF></gIBSUF>"
        f"<gIBSMun><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vIBSMun>{v_ibs_mun_tot:.2f}</vIBSMun></gIBSMun>"
        f"<vIBS>{v_ibs_tot:.2f}</vIBS><vCredPres>0.00</vCredPres><vCredPresCondSus>0.00</vCredPresCondSus>"
        f"</gIBS>"
        f"<gCBS><vDif>0.00</vDif><vDevTrib>0.00</vDevTrib><vCBS>{v_cbs_tot:.2f}</vCBS>"
        f"<vCredPres>0.00</vCredPres><vCredPresCondSus>0.00</vCredPresCondSus></gCBS></IBSCBSTot>"
    )
    total = f"<total>{icmstot}{ibscbstot}</total>"
    transp = f"<transp><modFrete>9</modFrete></transp>"

    # pagamento: cartao (03/04) exige grupo <card> com tpIntegra
    tpag = nota.get("forma_pagamento", "01")
    card = "<card><tpIntegra>2</tpIntegra></card>" if tpag in ("03", "04") else ""
    pag = f"<pag><detPag><tPag>{tpag}</tPag><vPag>{v_prod:.2f}</vPag>{card}</detPag></pag>"

    # responsavel tecnico (obrigatorio na NFC-e)
    rt = nota.get("resp_tec") or {
        "cnpj": "60943666000105",
        "contato": "FC CONTABIL",
        "email": "fcccontabil01@gmail.com",
        "fone": "3133867015",
    }
    inf_resptec = (
        f"<infRespTec><CNPJ>{rt.get('cnpj','')}</CNPJ>"
        f"<xContato>{rt.get('contato','')}</xContato>"
        f"<email>{rt.get('email','')}</email>"
        f"<fone>{rt.get('fone','')}</fone></infRespTec>"
    )

    # informacao adicional (opcional) - texto livre
    inf_adic = "<infAdic><infCpl>Documento emitido por ME ou EPP optante. NFC-e</infCpl></infAdic>"

    inf = (
        f'<infNFe versao="4.00" Id="NFe{chave}">'
        f"{ide}{emit_xml}{dest_xml}{dets}{total}{transp}{pag}{inf_adic}{inf_resptec}</infNFe>"
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
            "sefaz_raw": resp.text[:2000] if not autorizado else None,
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
