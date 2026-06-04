"""
sefaz/emissao.py  -  Autorizacao de NF-e modelo 55 (rota /emitir)

Segue o padrao do projeto: SOAP manual com requests + cert=(cert_file,key_file),
reaproveitando sefaz/cert.py (extrair_cert_pem). NAO usa o assinar_xml generico
de assinatura.py porque a NF-e exige assinatura especifica (Reference ao Id da
infNFe, enveloped + c14n exclusiva, SHA-256). A funcao de assinatura correta esta
aqui embaixo (assinar_nfe).

PRE-REQUISITOS:
  - Colocar os XSD ATUAIS (PL_010+ / NT 2023.001) em sefaz/schemas/ para validar
    o grupo ICMS61 (combustivel monofasico). Sem eles, validar_xsd e pulado.
  - signxml>=4.0.1, lxml, cryptography (ja no requirements).

TESTAR SEMPRE EM HOMOLOGACAO (ambiente="homologacao") ANTES DE PRODUCAO.
"""
import os
import re
from datetime import datetime, timezone, timedelta
from lxml import etree
import requests

from .cert import extrair_cert_pem, limpar_arquivos

NS = "http://www.portalfiscal.inf.br/nfe"
SCHEMAS_DIR = os.path.join(os.path.dirname(__file__), "schemas")

# Webservice de Autorizacao da SEFAZ-MG (modelo 55).
# MG usa autorizador proprio. CONFIRMAR a URL vigente no Portal NF-e antes de produzir.
URLS_AUTORIZACAO = {
    "producao":    "https://nfe.fazenda.mg.gov.br/nfe2/services/NFeAutorizacao4",
    "homologacao": "https://hnfe.fazenda.mg.gov.br/nfe2/services/NFeAutorizacao4",
}

UF_CODIGO = {"MG": "31"}  # adicionar outras UFs se necessario


# ---------------------------------------------------------------------------
# 1) Chave de acesso (44 digitos) + digito verificador (modulo 11)
# ---------------------------------------------------------------------------
def _dv_chave(chave43: str) -> str:
    pesos = [2, 3, 4, 5, 6, 7, 8, 9]
    soma = 0
    for i, d in enumerate(reversed(chave43)):
        soma += int(d) * pesos[i % len(pesos)]
    resto = soma % 11
    dv = 0 if resto in (0, 1) else 11 - resto
    return str(dv)


def montar_chave(cuf, dh_emi, cnpj, modelo, serie, numero, tp_emis, cnf):
    """Monta a chave de 44 digitos. cnf = codigo numerico aleatorio (8 dig)."""
    aamm = dh_emi.strftime("%y%m")
    chave43 = (
        f"{cuf}{aamm}{cnpj.zfill(14)}{modelo.zfill(2)}"
        f"{str(serie).zfill(3)}{str(numero).zfill(9)}"
        f"{str(tp_emis)}{str(cnf).zfill(8)}"
    )
    return chave43 + _dv_chave(chave43)


# ---------------------------------------------------------------------------
# 2) Grupos de imposto por item  (o coracao da regra de combustivel)
# ---------------------------------------------------------------------------
def _imposto_item(it):
    cst = str(it.get("cst_icms") or "").strip()

    if cst == "61":
        # COMBUSTIVEL MONOFASICO (gasolina/diesel) - posto SUBSTITUIDO.
        # Imposto ja cobrado anteriormente. qBCMono = litros, adRem = R$/L.
        # ATENCAO: validar nomes das tags no XSD ATUAL. Em alguns leiautes da NT2023.001
        # o grupo do substituido usa qBCMonoRet/adRemICMSRet/vICMSMonoRet (com "Ret").
        q = float(it["qCom"])
        adrem = float(it.get("aliq_icms_ad_rem") or 0)
        v = round(q * adrem, 2)
        icms = (
            f"<ICMS61><orig>{it.get('origem','0')}</orig><CST>61</CST>"
            f"<qBCMonoRet>{q:.4f}</qBCMonoRet>"
            f"<adRemICMSRet>{adrem:.4f}</adRemICMSRet>"
            f"<vICMSMonoRet>{v:.2f}</vICMSMonoRet></ICMS61>"
        )
    elif cst == "60":
        # ETANOL / GNV - ICMS-ST tradicional ja retido (CST 60).
        icms = (
            f"<ICMS60><orig>{it.get('origem','0')}</orig><CST>60</CST>"
            f"<vBCSTRet>{float(it.get('vbc_st_ret',0)):.2f}</vBCSTRet>"
            f"<pST>{float(it.get('aliq_icms',0)):.4f}</pST>"
            f"<vICMSSTRet>{float(it.get('vicms_st_ret',0)):.2f}</vICMSSTRet></ICMS60>"
        )
    else:
        # tratamento normal (ex.: ARLA, conveniencia) - CST 00 simples
        vprod = float(it["vProd"])
        aliq = float(it.get("aliq_icms", 0))
        vicms = round(vprod * aliq / 100, 2)
        icms = (
            f"<ICMS00><orig>{it.get('origem','0')}</orig><CST>00</CST>"
            f"<modBC>3</modBC><vBC>{vprod:.2f}</vBC>"
            f"<pICMS>{aliq:.4f}</pICMS><vICMS>{vicms:.2f}</vICMS></ICMS00>"
        )

    cst_pis = it.get("cst_pis") or "04"
    cst_cof = it.get("cst_cofins") or "04"
    pis = f"<PIS><PISNT><CST>{cst_pis}</CST></PISNT></PIS>"
    cof = f"<COFINS><COFINSNT><CST>{cst_cof}</CST></COFINSNT></COFINS>"
    return f"<imposto><ICMS>{icms}</ICMS>{pis}{cof}</imposto>"


def _comb_item(it):
    if str(it.get("ind_combustivel") or "N") != "S":
        return ""
    pbio = ""
    if it.get("perc_bio"):
        pbio = f"<pBio>{float(it['perc_bio']):.4f}</pBio>"
    return (
        f"<comb><cProdANP>{it['cod_anp']}</cProdANP>"
        f"<descANP>{it.get('desc_anp','')}</descANP>{pbio}"
        f"<UFCons>{it.get('uf_cons','MG')}</UFCons></comb>"
    )


def _det_item(it, n):
    return (
        f'<det nItem="{n}"><prod>'
        f"<cProd>{it['cProd']}</cProd>"
        f"<cEAN>{it.get('cEAN','SEM GTIN')}</cEAN>"
        f"<xProd>{it['xProd']}</xProd>"
        f"<NCM>{it['ncm']}</NCM>"
        + (f"<CEST>{it['cest']}</CEST>" if it.get("cest") else "")
        + f"<CFOP>{it['cfop']}</CFOP>"
        f"<uCom>{it['uCom']}</uCom>"
        f"<qCom>{float(it['qCom']):.4f}</qCom>"
        f"<vUnCom>{float(it['vUnCom']):.10f}</vUnCom>"
        f"<vProd>{float(it['vProd']):.2f}</vProd>"
        f"<cEANTrib>{it.get('cEANTrib','SEM GTIN')}</cEANTrib>"
        f"<uTrib>{it.get('uTrib', it['uCom'])}</uTrib>"
        f"<qTrib>{float(it['qCom']):.4f}</qTrib>"
        f"<vUnTrib>{float(it['vUnCom']):.10f}</vUnTrib>"
        f"<indTot>1</indTot>"
        + _comb_item(it)
        + "</prod>"
        + _imposto_item(it)
        + "</det>"
    )


# ---------------------------------------------------------------------------
# 3) Monta a infNFe completa
# ---------------------------------------------------------------------------
def montar_infnfe(nota, ambiente):
    tp_amb = "1" if ambiente == "producao" else "2"
    emit = nota["emitente"]
    dest = nota["destinatario"]
    cuf = UF_CODIGO.get(emit.get("uf", "MG"), "31")

    # data de emissao com timezone -03:00 (Brasilia)
    dh = datetime.now(timezone(timedelta(hours=-3)))
    dh_emi = dh.strftime("%Y-%m-%dT%H:%M:%S-03:00")

    cnpj_emit = re.sub(r"\D", "", emit["cnpj"])
    cnf = nota.get("cnf") or "12345678"          # gerar aleatorio de verdade no caller
    numero = nota["numero"]
    serie = nota.get("serie", 1)
    modelo = "55"
    tp_emis = "1"

    chave = montar_chave(cuf, dh, cnpj_emit, modelo, serie, numero, tp_emis, cnf)
    cnf_fmt = str(cnf).zfill(8)
    cdv = chave[-1]

    # itens + totais
    dets = "".join(_det_item(it, i + 1) for i, it in enumerate(nota["itens"]))
    v_prod = sum(float(it["vProd"]) for it in nota["itens"])
    # ICMS proprio = 0 (CST 61/60). vICMSMono soma dos monofasicos:
    v_icms_mono = sum(
        round(float(it["qCom"]) * float(it.get("aliq_icms_ad_rem") or 0), 2)
        for it in nota["itens"] if str(it.get("cst_icms")) == "61"
    )

    # destinatario: CNPJ ou CPF
    doc_dest = re.sub(r"\D", "", dest.get("cnpj_cpf", ""))
    tag_doc = "CNPJ" if len(doc_dest) == 14 else "CPF"

    ide = (
        f"<ide><cUF>{cuf}</cUF><cNF>{cnf_fmt}</cNF>"
        f"<natOp>{nota.get('natureza_op','VENDA')}</natOp>"
        f"<mod>{modelo}</mod><serie>{serie}</serie><nNF>{numero}</nNF>"
        f"<dhEmi>{dh_emi}</dhEmi><tpNF>1</tpNF><idDest>1</idDest>"
        f"<cMunFG>{emit.get('c_mun','3118601')}</cMunFG>"
        f"<tpImp>1</tpImp><tpEmis>{tp_emis}</tpEmis><cDV>{cdv}</cDV>"
        f"<tpAmb>{tp_amb}</tpAmb><finNFe>1</finNFe><indFinal>1</indFinal>"
        f"<indPres>1</indPres><procEmi>0</procEmi><verProc>Octano1.0</verProc></ide>"
    )
    emit_xml = (
        f"<emit><CNPJ>{cnpj_emit}</CNPJ><xNome>{emit['nome']}</xNome>"
        f"<enderEmit><xLgr>{emit.get('logradouro','')}</xLgr>"
        f"<nro>{emit.get('numero','S/N')}</nro><xBairro>{emit.get('bairro','')}</xBairro>"
        f"<cMun>{emit.get('c_mun','3118601')}</cMun><xMun>{emit.get('municipio','')}</xMun>"
        f"<UF>{emit.get('uf','MG')}</UF><CEP>{re.sub(r'D','',emit.get('cep',''))}</CEP>"
        f"<cPais>1058</cPais><xPais>BRASIL</xPais></enderEmit>"
        f"<IE>{emit.get('ie','')}</IE><CRT>{emit.get('crt','3')}</CRT></emit>"
    )
    dest_xml = (
        f"<dest><{tag_doc}>{doc_dest}</{tag_doc}><xNome>{dest['nome']}</xNome>"
        f"<indIEDest>{dest.get('ind_ie','9')}</indIEDest>"
        + (f"<IE>{dest['ie']}</IE>" if dest.get("ie") else "")
        + "</dest>"
    )
    total = (
        f"<total><ICMSTot><vBC>0.00</vBC><vICMS>0.00</vICMS>"
        f"<vICMSDeson>0.00</vICMSDeson><vFCP>0.00</vFCP><vBCST>0.00</vBCST>"
        f"<vST>0.00</vST><vFCPST>0.00</vFCPST><vFCPSTRet>0.00</vFCPSTRet>"
        f"<vProd>{v_prod:.2f}</vProd><vFrete>0.00</vFrete><vSeg>0.00</vSeg>"
        f"<vDesc>0.00</vDesc><vII>0.00</vII><vIPI>0.00</vIPI><vIPIDevol>0.00</vIPIDevol>"
        f"<vPIS>0.00</vPIS><vCOFINS>0.00</vCOFINS><vOutro>0.00</vOutro>"
        f"<vNF>{v_prod:.2f}</vNF><vICMSMono>{v_icms_mono:.2f}</vICMSMono></ICMSTot></total>"
    )
    transp = f"<transp><modFrete>{nota.get('mod_frete','9')}</modFrete></transp>"
    pag = "<pag><detPag><tPag>01</tPag><vPag>%.2f</vPag></detPag></pag>" % v_prod

    inf = (
        f'<infNFe versao="4.00" Id="NFe{chave}">'
        f"{ide}{emit_xml}{dest_xml}{dets}{total}{transp}{pag}</infNFe>"
    )
    nfe = f'<NFe xmlns="{NS}">{inf}</NFe>'
    return nfe, chave


# ---------------------------------------------------------------------------
# 4) Assinatura ESPECIFICA de NF-e (Reference ao Id, enveloped + c14n, SHA-256)
# ---------------------------------------------------------------------------
def assinar_nfe(xml_nfe: str, cert_file: str, key_file: str) -> str:
    from signxml import XMLSigner, methods
    root = etree.fromstring(xml_nfe.encode("utf-8"))
    inf = root.find(f"{{{NS}}}infNFe")
    ref_uri = "#" + inf.get("Id")

    with open(cert_file, "rb") as cf, open(key_file, "rb") as kf:
        cert_data, key_data = cf.read(), kf.read()

    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm="rsa-sha256",
        digest_algorithm="sha256",
        c14n_algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    # assina referenciando o Id da infNFe; a Signature deve ficar como filha de <NFe>
    signed_inf = signer.sign(root, key=key_data, cert=cert_data, reference_uri=ref_uri)
    return etree.tostring(signed_inf, encoding="unicode")


# ---------------------------------------------------------------------------
# 5) Validacao XSD (opcional; so roda se o schema existir)
# ---------------------------------------------------------------------------
def validar_xsd(xml_str: str, xsd_nome="leiauteNFe_v4.00.xsd"):
    caminho = os.path.join(SCHEMAS_DIR, xsd_nome)
    if not os.path.exists(caminho):
        return None  # schema nao disponivel -> pula validacao (avisar no log)
    schema = etree.XMLSchema(etree.parse(caminho))
    doc = etree.fromstring(xml_str.encode("utf-8"))
    if schema.validate(doc):
        return []
    return [str(e) for e in schema.error_log]


# ---------------------------------------------------------------------------
# 6) Envio ao NFeAutorizacao4 (sincrono)
# ---------------------------------------------------------------------------
def _soap_autorizacao(xml_nfe_assinada, id_lote):
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"><soap12:Body>'
        '<nfeAutorizacaoLote xmlns="http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4">'
        '<nfeDadosMsg>'
        f'<enviNFe versao="4.00" xmlns="{NS}"><idLote>{id_lote}</idLote>'
        f'<indSinc>1</indSinc>{xml_nfe_assinada}</enviNFe>'
        '</nfeDadosMsg></nfeAutorizacaoLote></soap12:Body></soap12:Envelope>'
    )


def emitir_nfe(nota, cert_base64, cert_senha, ambiente="homologacao"):
    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        # 1) monta + 2) assina
        xml_nfe, chave = montar_infnfe(nota, ambiente)
        xml_assinada = assinar_nfe(xml_nfe, cert_file, key_file)

        # 3) valida no XSD localmente (NAO bloqueia: a SEFAZ e quem da o veredito).
        #    Se o schema local nao casar com a raiz, apenas registramos e seguimos.
        aviso_xsd = None
        try:
            erros = validar_xsd(xml_assinada)
            if erros:
                aviso_xsd = erros[:5]
                print("AVISO validacao XSD local (nao-bloqueante):", aviso_xsd)
        except Exception as e:
            print("AVISO: validacao XSD pulada:", str(e))

        # 4) envia
        url = URLS_AUTORIZACAO[ambiente]
        soap = _soap_autorizacao(xml_assinada, nota.get("id_lote", "1"))
        resp = requests.post(
            url, data=soap.encode("utf-8"),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            cert=(cert_file, key_file), timeout=60, verify=True,
        )
        if resp.status_code != 200:
            print("ERRO HTTP SEFAZ:", resp.status_code, "| corpo:", resp.text[:1500])
            return {"ok": False, "etapa": "http", "chave": chave,
                    "status": resp.status_code, "detalhes": resp.text[:800]}

        # 5) trata retorno (cStat 100 = autorizado; 104 lote processado -> ver protNFe)
        root = etree.fromstring(resp.content)
        def t(tag):
            el = root.find(f".//{{{NS}}}{tag}")
            return el.text if el is not None else None
        cstat = t("cStat")
        xmotivo = t("xMotivo")
        prot = root.find(f".//{{{NS}}}protNFe")
        cstat_nfe = None
        nprot = None
        if prot is not None:
            inf_prot = prot.find(f"{{{NS}}}infProt")
            if inf_prot is not None:
                cstat_nfe = inf_prot.findtext(f"{{{NS}}}cStat")
                xmotivo = inf_prot.findtext(f"{{{NS}}}xMotivo") or xmotivo
                nprot = inf_prot.findtext(f"{{{NS}}}nProt")

        autorizado = (cstat_nfe == "100")
        return {
            "ok": autorizado,
            "etapa": "sefaz",
            "chave": chave,
            "cstat_lote": cstat,
            "cstat_nfe": cstat_nfe,
            "xmotivo": xmotivo,
            "protocolo": nprot,
            "aviso_xsd": aviso_xsd,
            "xml_assinado": xml_assinada if autorizado else None,
        }
    finally:
        limpar_arquivos(cert_file, key_file)
