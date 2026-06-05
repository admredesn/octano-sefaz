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
def _imposto_item(it, crt="3"):
    cst = str(it.get("cst_icms") or "").strip()
    orig = it.get("origem", "0")
    simples = str(crt) == "1"

    if cst == "61":
        # COMBUSTIVEL MONOFASICO (gasolina/diesel) - posto SUBSTITUIDO.
        # O grupo ICMS61 vale tambem para o Simples Nacional (monofasico).
        q = float(it["qCom"])
        adrem = float(it.get("aliq_icms_ad_rem") or 0)
        v = round(q * adrem, 2)
        icms = (
            f"<ICMS61><orig>{orig}</orig><CST>61</CST>"
            f"<qBCMonoRet>{q:.4f}</qBCMonoRet>"
            f"<adRemICMSRet>{adrem:.4f}</adRemICMSRet>"
            f"<vICMSMonoRet>{v:.2f}</vICMSMonoRet></ICMS61>"
        )
    elif cst == "60":
        # ETANOL / GNV - ICMS-ST ja retido. No Simples -> CSOSN 500.
        if simples:
            icms = (
                f"<ICMSSN500><orig>{orig}</orig><CSOSN>500</CSOSN>"
                f"<vBCSTRet>{float(it.get('vbc_st_ret',0)):.2f}</vBCSTRet>"
                f"<vICMSSTRet>{float(it.get('vicms_st_ret',0)):.2f}</vICMSSTRet></ICMSSN500>"
            )
        else:
            # XML autorizado do posto usa ICMS60 enxuto (so orig + CST).
            icms = f"<ICMS60><orig>{orig}</orig><CST>60</CST></ICMS60>"
    else:
        # demais (lubrificante, conveniencia, ARLA...).
        if simples:
            # Simples Nacional sem credito de ICMS -> CSOSN 102 (ICMSSN102).
            csosn = it.get("csosn") or "102"
            icms = (
                f"<ICMSSN102><orig>{orig}</orig><CSOSN>{csosn}</CSOSN></ICMSSN102>"
            )
        else:
            vprod = float(it["vProd"])
            aliq = float(it.get("aliq_icms", 0))
            vicms = round(vprod * aliq / 100, 2)
            icms = (
                f"<ICMS00><orig>{orig}</orig><CST>00</CST>"
                f"<modBC>3</modBC><vBC>{vprod:.2f}</vBC>"
                f"<pICMS>{aliq:.4f}</pICMS><vICMS>{vicms:.2f}</vICMS></ICMS00>"
            )

    # PIS/COFINS: grupo "Outr" exige CST de Outras Operacoes (ex.: 49, 99).
    # O cadastro pode trazer CST 01 (leiaute antigo/PISAliq), invalido aqui -> força 49.
    cst_pis = str(it.get("cst_pis") or "49")
    cst_cof = str(it.get("cst_cofins") or "49")
    if cst_pis in ("01", "02", "03"):
        cst_pis = "49"
    if cst_cof in ("01", "02", "03"):
        cst_cof = "49"
    vprod_pc = float(it["vProd"])

    # Combustivel monofasico (CST ICMS 61): PIS/COFINS NAO-TRIBUTADO CST 04
    # (grupo PISNT/COFINSNT, sem vBC). Ver secao 2.2 da referencia.
    if cst == "61":
        pis = "<PIS><PISNT><CST>04</CST></PISNT></PIS>"
        cof = "<COFINS><COFINSNT><CST>04</CST></COFINSNT></COFINS>"
    else:
        # demais: espelha o XML autorizado do posto -> grupo "Outr" (CST 49) zerado.
        # (revenda de lubrificante: tributo ja recolhido na cadeia)
        pis = (f"<PIS><PISOutr><CST>{cst_pis}</CST>"
               f"<vBC>0.00</vBC><pPIS>0.00</pPIS><vPIS>0.00</vPIS></PISOutr></PIS>")
        cof = (f"<COFINS><COFINSOutr><CST>{cst_cof}</CST>"
               f"<vBC>0.00</vBC><pCOFINS>0.00</pCOFINS><vCOFINS>0.00</vCOFINS></COFINSOutr></COFINS>")

    # IPI: nao tributado (CST 99), zerado - como no XML do posto.
    ipi = ("<IPI><cEnq>999</cEnq><IPITrib><CST>99</CST>"
           "<vBC>0.00</vBC><pIPI>0.00</pIPI><vIPI>0.00</vIPI></IPITrib></IPI>")

    # IBS/CBS (Reforma Tributaria - obrigatorio em 2026).
    if cst == "61":
        # COMBUSTIVEL MONOFASICO -> CST 620 + gIBSCBSMono/gMonoRet (secao 2.2).
        # Posto e substituido (tributo retido na cadeia) -> valores zerados.
        cst_rt = it.get("cst_ibscbs") or "620"
        cclass = it.get("cclasstrib") or "620006"
        ibscbs = (
            f"<IBSCBS><CST>{cst_rt}</CST><cClassTrib>{cclass}</cClassTrib>"
            f"<gIBSCBSMono>"
            f"<gMonoRet><qBCMonoRet>0.0000</qBCMonoRet>"
            f"<adRemIBSRet>0.0000</adRemIBSRet><vIBSMonoRet>0.00</vIBSMonoRet>"
            f"<adRemCBSRet>0.0000</adRemCBSRet><vCBSMonoRet>0.00</vCBSMonoRet></gMonoRet>"
            f"<vTotIBSMonoItem>0.00</vTotIBSMonoItem><vTotCBSMonoItem>0.00</vTotCBSMonoItem>"
            f"</gIBSCBSMono></IBSCBS>"
        )
    else:
        # Aliquotas-teste 2026: IBS estadual 0,10%, IBS municipal 0%, CBS 0,90%.
        vbc_rt = vprod_pc
        p_ibs_uf, p_ibs_mun, p_cbs = 0.10, 0.00, 0.90
        v_ibs_uf = round(vbc_rt * p_ibs_uf / 100, 2)
        v_ibs_mun = round(vbc_rt * p_ibs_mun / 100, 2)
        v_ibs = round(v_ibs_uf + v_ibs_mun, 2)
        v_cbs = round(vbc_rt * p_cbs / 100, 2)
        cst_rt = it.get("cst_ibscbs") or "000"
        cclass = it.get("cclasstrib") or "000001"
        ibscbs = (
            f"<IBSCBS><CST>{cst_rt}</CST><cClassTrib>{cclass}</cClassTrib>"
            f"<gIBSCBS><vBC>{vbc_rt:.2f}</vBC>"
            f"<gIBSUF><pIBSUF>{p_ibs_uf:.4f}</pIBSUF><vIBSUF>{v_ibs_uf:.2f}</vIBSUF></gIBSUF>"
            f"<gIBSMun><pIBSMun>{p_ibs_mun:.4f}</pIBSMun><vIBSMun>{v_ibs_mun:.2f}</vIBSMun></gIBSMun>"
            f"<vIBS>{v_ibs:.2f}</vIBS>"
            f"<gCBS><pCBS>{p_cbs:.4f}</pCBS><vCBS>{v_cbs:.2f}</vCBS></gCBS>"
            f"</gIBSCBS></IBSCBS>"
        )

    return f"<imposto><ICMS>{icms}</ICMS>{ipi}{pis}{cof}{ibscbs}</imposto>"


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


def _det_item(it, n, crt="3"):
    return (
        f'<det nItem="{n}"><prod>'
        f"<cProd>{it['cProd']}</cProd>"
        f"<cEAN>{it.get('cEAN','SEM GTIN')}</cEAN>"
        f"<xProd>{it['xProd']}</xProd>"
        f"<NCM>{it['ncm']}</NCM>"
        + (f"<CEST>{it['cest']}</CEST>" if it.get("cest") else "")
        + (f"<indEscala>{it.get('ind_escala','S')}</indEscala>" if it.get("cest") else "")
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
        + _imposto_item(it, crt)
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
    crt_emit = str(emit.get("crt", "3"))
    dets = "".join(_det_item(it, i + 1, crt_emit) for i, it in enumerate(nota["itens"]))
    v_prod = sum(float(it["vProd"]) for it in nota["itens"])
    # ICMS proprio = 0 (CST 61/60). vICMSMono soma dos monofasicos:
    v_icms_mono = sum(
        round(float(it["qCom"]) * float(it.get("aliq_icms_ad_rem") or 0), 2)
        for it in nota["itens"] if str(it.get("cst_icms")) == "61"
    )
    # totais IBS/CBS (Reforma) - aliquotas-teste 2026: IBS-UF 0,10%, IBS-Mun 0%, CBS 0,90%
    # Itens monofasicos (CST 61) NAO entram no gIBS/gCBS; entram no gMono (zerados).
    nao_mono = [it for it in nota["itens"] if str(it.get("cst_icms")) != "61"]
    tem_mono = any(str(it.get("cst_icms")) == "61" for it in nota["itens"])
    v_ibs_uf_tot = sum(round(float(it["vProd"]) * 0.10 / 100, 2) for it in nao_mono)
    v_ibs_mun_tot = 0.00
    v_ibs_tot = round(v_ibs_uf_tot + v_ibs_mun_tot, 2)
    v_cbs_tot = sum(round(float(it["vProd"]) * 0.90 / 100, 2) for it in nao_mono)
    v_bc_rt_tot = sum(float(it["vProd"]) for it in nao_mono)

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
    cep_emit = re.sub(r"\D", "", emit.get("cep", ""))
    ie_emit = re.sub(r"\D", "", emit.get("ie", ""))
    emit_xml = (
        f"<emit><CNPJ>{cnpj_emit}</CNPJ><xNome>{emit['nome']}</xNome>"
        f"<enderEmit><xLgr>{emit.get('logradouro','')}</xLgr>"
        f"<nro>{emit.get('numero','S/N')}</nro><xBairro>{emit.get('bairro','')}</xBairro>"
        f"<cMun>{emit.get('c_mun','3118601')}</cMun><xMun>{emit.get('municipio','')}</xMun>"
        f"<UF>{emit.get('uf','MG')}</UF><CEP>{cep_emit}</CEP>"
        f"<cPais>1058</cPais><xPais>BRASIL</xPais></enderEmit>"
        f"<IE>{ie_emit}</IE><CRT>{emit.get('crt','3')}</CRT></emit>"
    )
    cep_dest = re.sub(r"\D", "", str(dest.get("cep", "") or "")) or "35610000"
    ender_dest = (
        f"<enderDest><xLgr>{dest.get('logradouro','SEM ENDERECO')}</xLgr>"
        f"<nro>{dest.get('numero','S/N')}</nro><xBairro>{dest.get('bairro','CENTRO')}</xBairro>"
        f"<cMun>{dest.get('c_mun','3123205')}</cMun><xMun>{dest.get('municipio','DORES DO INDAIA')}</xMun>"
        f"<UF>{dest.get('uf','MG')}</UF><CEP>{cep_dest}</CEP>"
        f"<cPais>1058</cPais><xPais>BRASIL</xPais></enderDest>"
    )
    dest_xml = (
        f"<dest><{tag_doc}>{doc_dest}</{tag_doc}><xNome>{dest['nome']}</xNome>"
        f"{ender_dest}"
        f"<indIEDest>{dest.get('ind_ie','9')}</indIEDest>"
        + (f"<IE>{dest['ie']}</IE>" if dest.get("ie") else "")
        + "</dest>"
    )
    # vICMSMono so entra quando ha item monofasico (CST 61).
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
    # bloco IBSCBSTot (Reforma) - espelha o XML autorizado do posto
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
    transp = f"<transp><modFrete>{nota.get('mod_frete','9')}</modFrete></transp>"
    pag = "<pag><detPag><tPag>01</tPag><vPag>%.2f</vPag></detPag></pag>" % v_prod

    inf = (
        f'<infNFe versao="4.00" Id="NFe{chave}">'
        f"{ide}{emit_xml}{dest_xml}{dets}{total}{transp}{pag}</infNFe>"
    )
    nfe = f'<NFe xmlns="{NS}">{inf}</NFe>'
    return nfe, chave


# ---------------------------------------------------------------------------
# 4) Assinatura ESPECIFICA de NF-e (Reference ao Id, enveloped + C14N, RSA-SHA1)
#    Implementacao manual com cryptography: a NF-e usa SHA-1 (padrao do leiaute)
#    e o signxml 4.x bloqueia SHA-1. Montamos a Signature a mao, sem prefixo ds:.
# ---------------------------------------------------------------------------
DS_NS = "http://www.w3.org/2000/09/xmldsig#"
_C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"  # C14N 1.0 inclusiva


def _c14n_bytes(elem) -> bytes:
    # Canonicalizacao C14N 1.0 inclusiva, sem comentarios.
    # IMPORTANTE: usar etree.tostring(method="c14n") direto sobre um sub-elemento
    # com namespace default herdado faz o lxml inserir xmlns="" nos filhos, o que
    # ALTERA o digest e causa rejeicao 297. A forma correta e serializar o elemento
    # (que mantem o xmlns do proprio elemento, sem xmlns="" nos filhos) e entao
    # canonicalizar essa string.
    return etree.canonicalize(etree.tostring(elem).decode("utf-8")).encode("utf-8")


def assinar_nfe(xml_nfe: str, cert_file: str, key_file: str) -> str:
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.x509 import load_pem_x509_certificate

    root = etree.fromstring(xml_nfe.encode("utf-8"))
    inf = root.find(f"{{{NS}}}infNFe")
    ref_id = inf.get("Id")

    with open(cert_file, "rb") as cf, open(key_file, "rb") as kf:
        cert_pem, key_pem = cf.read(), kf.read()

    private_key = serialization.load_pem_private_key(key_pem, password=None)
    cert = load_pem_x509_certificate(cert_pem)
    # certificado em base64 (DER), sem cabecalhos PEM - vai no X509Certificate
    cert_der_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()

    # --- 1) Monta a Signature (sem DigestValue ainda) e anexa ao root ---
    #     O digest sera calculado DEPOIS, sobre a infNFe no contexto final,
    #     aplicando a transformacao enveloped (remover a Signature) — exatamente
    #     como a SEFAZ valida. Calcular antes (infNFe isolada) gera digest
    #     divergente por causa de namespaces herdados -> rejeicao 297.
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
    dig_v = etree.SubElement(ref, ds("DigestValue"))  # texto preenchido no passo 2

    signature = etree.Element(ds("Signature"), nsmap={None: DS_NS})
    signature.append(signed_info)
    sv = etree.SubElement(signature, ds("SignatureValue"))
    key_info = etree.SubElement(signature, ds("KeyInfo"))
    x509_data = etree.SubElement(key_info, ds("X509Data"))
    x509_cert = etree.SubElement(x509_data, ds("X509Certificate"))
    x509_cert.text = cert_der_b64
    root.append(signature)  # Signature como ultima filha de <NFe>, irma de infNFe

    # --- 2) DigestValue: aplica enveloped (remove Signature de uma copia) e
    #     canoniza a infNFe NO CONTEXTO do documento final, depois SHA1 ---
    root_tmp = etree.fromstring(etree.tostring(root))
    sig_tmp = root_tmp.find(ds("Signature"))
    root_tmp.remove(sig_tmp)
    inf_tmp = root_tmp.find(f"{{{NS}}}infNFe")
    inf_c14n = _c14n_bytes(inf_tmp)
    digest = hashes.Hash(hashes.SHA1())
    digest.update(inf_c14n)
    dig_v.text = base64.b64encode(digest.finalize()).decode()

    # --- 3) canoniza o SignedInfo no contexto final e assina (RSA-SHA1) ---
    signed_info_c14n = _c14n_bytes(signed_info)
    assinatura = private_key.sign(signed_info_c14n, padding.PKCS1v15(), hashes.SHA1())
    sv.text = base64.b64encode(assinatura).decode()

    return etree.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# 5) Validacao XSD (opcional; so roda se o schema existir)
# ---------------------------------------------------------------------------
def validar_xsd(xml_str: str, xsd_nome="nfe_v4.00.xsd"):
    # tenta o nfe_v4.00.xsd (declara NFe como elemento global); cai para o leiaute se nao houver
    for nome in (xsd_nome, "leiauteNFe_v4.00.xsd"):
        caminho = os.path.join(SCHEMAS_DIR, nome)
        if os.path.exists(caminho):
            try:
                schema = etree.XMLSchema(etree.parse(caminho))
                doc = etree.fromstring(xml_str.encode("utf-8"))
                if schema.validate(doc):
                    return []
                return [f"[{nome}] {e.message} (linha {e.line})" for e in schema.error_log]
            except Exception as e:
                return [f"[{nome}] erro ao validar: {e}"]
    return None  # nenhum schema disponivel


# ---------------------------------------------------------------------------
# 6) Envio ao NFeAutorizacao4 (sincrono)
# ---------------------------------------------------------------------------
def _soap_autorizacao(xml_nfe_assinada, id_lote):
    # SOAP 1.2: o corpo leva nfeDadosMsg (namespace do wsdl NFeAutorizacao4)
    # contendo o enviNFe. O nome da operacao (nfeAutorizacaoLote) vai na 'action'
    # do Content-Type, NAO como elemento do corpo.
    wsdl_ns = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap12:Envelope '
        'xmlns:soap12="http://www.w3.org/2003/05/soap-envelope"><soap12:Body>'
        f'<nfeDadosMsg xmlns="{wsdl_ns}">'
        f'<enviNFe versao="4.00" xmlns="{NS}"><idLote>{id_lote}</idLote>'
        f'<indSinc>1</indSinc>{xml_nfe_assinada}</enviNFe>'
        '</nfeDadosMsg></soap12:Body></soap12:Envelope>'
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
            # valida o XML SEM assinatura contra o XSD novo (PL_010b, com IBS/CBS).
            erros = validar_xsd(xml_nfe)
            if erros:
                # so ignora o falso-positivo de Signature ausente (xml ainda nao assinado)
                erros = [e for e in erros if "Signature" not in e and "infNFeSupl" not in e]
                if erros:
                    aviso_xsd = erros[:8]
                    print("AVISO validacao XSD local (nao-bloqueante):", aviso_xsd)
        except Exception as e:
            print("AVISO: validacao XSD pulada:", str(e))

        # 4) envia
        url = URLS_AUTORIZACAO[ambiente]
        soap = _soap_autorizacao(xml_assinada, nota.get("id_lote", "1"))
        # SOAP 1.2: a 'action' (metodo) vai DENTRO do Content-Type, senao a SEFAZ
        # responde "Nao e possivel localizar o metodo de despacho".
        action = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeAutorizacao4/nfeAutorizacaoLote"
        ctype = f'application/soap+xml; charset=utf-8; action="{action}"'
        resp = requests.post(
            url, data=soap.encode("utf-8"),
            headers={"Content-Type": ctype},
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

        # Monta o nfeProc completo (NFe assinada + protNFe) - este e o XML de
        # DISTRIBUICAO oficial, o que vale como documento fiscal e o que o DANFE
        # precisa para ser gerado. So existe quando autorizado (tem protNFe).
        nfe_proc = None
        if autorizado and prot is not None:
            prot_xml = etree.tostring(prot, encoding="unicode")
            nfe_proc = (
                f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<nfeProc versao="4.00" xmlns="{NS}">'
                f'{xml_assinada}{prot_xml}'
                f'</nfeProc>'
            )

        return {
            "ok": autorizado,
            "etapa": "sefaz",
            "chave": chave,
            "cstat_lote": cstat,
            "cstat_nfe": cstat_nfe,
            "xmotivo": xmotivo,
            "protocolo": nprot,
            "aviso_xsd": aviso_xsd,
            "xml_debug": xml_assinada,
            "xml_assinado": xml_assinada,
            "nfe_proc": nfe_proc,
        }
    finally:
        limpar_arquivos(cert_file, key_file)
