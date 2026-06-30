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
    elif cst == "00" or cst == "000":
        # CST 00 - tributacao integral do ICMS (produto da loja tributado, ex. aditivos).
        # Destaca vBC, pICMS e vICMS com a aliquota cadastrada (ex. 18%).
        p_icms = float(it.get("aliq_icms") or 0)
        v_icms = round(vprod * p_icms / 100, 2)
        icms = (f"<ICMS><ICMS00><orig>{orig}</orig><CST>00</CST>"
                f"<modBC>3</modBC><vBC>{vprod:.2f}</vBC>"
                f"<pICMS>{p_icms:.4f}</pICMS><vICMS>{v_icms:.2f}</vICMS></ICMS00></ICMS>")
        ppis = float(it.get("aliq_pis") or 1.65)
        pcof = float(it.get("aliq_cofins") or 7.60)
        vpis = round(vprod * ppis / 100, 2)
        vcof = round(vprod * pcof / 100, 2)
        pis = (f"<PIS><PISAliq><CST>01</CST><vBC>{vprod:.2f}</vBC>"
               f"<pPIS>{ppis:.4f}</pPIS><vPIS>{vpis:.2f}</vPIS></PISAliq></PIS>")
        cof = (f"<COFINS><COFINSAliq><CST>01</CST><vBC>{vprod:.2f}</vBC>"
               f"<pCOFINS>{pcof:.4f}</pCOFINS><vCOFINS>{vcof:.2f}</vCOFINS></COFINSAliq></COFINS>")
        v_ibs_uf = round(vprod * 0.10 / 100, 2)
        v_cbs = round(vprod * 0.90 / 100, 2)
        ibscbs = (f"<IBSCBS><CST>000</CST><cClassTrib>000001</cClassTrib>"
                  f"<gIBSCBS><vBC>{vprod:.2f}</vBC>"
                  f"<gIBSUF><pIBSUF>0.1000</pIBSUF><vIBSUF>{v_ibs_uf:.2f}</vIBSUF></gIBSUF>"
                  f"<gIBSMun><pIBSMun>0.0000</pIBSMun><vIBSMun>0.00</vIBSMun></gIBSMun>"
                  f"<vIBS>{v_ibs_uf:.2f}</vIBS>"
                  f"<gCBS><pCBS>0.9000</pCBS><vCBS>{v_cbs:.2f}</vCBS></gCBS></gIBSCBS></IBSCBS>")
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
    SEM IPI. O posto trata combustivel E lubrificante com grupo <comb> (cProdANP).

    Combustivel monofasico em MG exige:
    - unidade tributavel "L" (validado contra a SEFAZ-MG; "LT"/"LITRO"/"LTS" dao 854)
    - grupo <encerrante> dentro do <comb> com nBico/nTanque/vEncIni/vEncFin (rej. 378)
    """
    cest = it.get("cest")
    tem_anp = bool(it.get("cod_anp"))

    # unidade: combustivel (com cod_anp) usa SEMPRE "L" na NFC-e MG.
    unidade = "L" if tem_anp else (it.get("uCom") or "UN")

    comb = ""
    if tem_anp:
        # grupo encerrante (obrigatorio p/ combustivel em MG). Os valores vem do
        # concentrador via PDV: enc_ini/enc_fin (leitura do totalizador da bomba),
        # n_bico e n_tanque. Se faltar algum, usa fallback seguro para nao travar.
        enc_ini = it.get("enc_ini")
        enc_fin = it.get("enc_fin")
        n_bico = it.get("n_bico") or it.get("bico") or 1
        n_tanque = it.get("n_tanque") or it.get("tanque") or 1
        # Robustez: o encerrante FINAL e o que o concentrador sempre fornece.
        # Se o INICIAL faltar, deriva-o de (final - litros) — o totalizador antes
        # do abastecimento. Assim o grupo nunca fica incompleto (evita rej. 378).
        try:
            litros_x = float(it.get("qCom") or 0)
        except (TypeError, ValueError):
            litros_x = 0.0
        if enc_fin is not None and enc_ini is None:
            enc_ini = float(enc_fin) - litros_x
        if enc_ini is not None and enc_fin is None:
            enc_fin = float(enc_ini) + litros_x
        enc = ""
        if enc_ini is not None and enc_fin is not None:
            enc = (f"<encerrante>"
                   f"<nBico>{int(n_bico)}</nBico>"
                   f"<nTanque>{int(n_tanque)}</nTanque>"
                   f"<vEncIni>{float(enc_ini):.3f}</vEncIni>"
                   f"<vEncFin>{float(enc_fin):.3f}</vEncFin>"
                   f"</encerrante>")
        comb = (f"<comb><cProdANP>{it['cod_anp']}</cProdANP>"
                f"<descANP>{it.get('desc_anp', it['xProd'])[:95]}</descANP>"
                f"<UFCons>{it.get('uf_cons','MG')}</UFCons>{enc}</comb>")
    prod = (
        f'<det nItem="{n}"><prod>'
        f"<cProd>{it['cProd']}</cProd><cEAN>{it.get('cEAN','SEM GTIN')}</cEAN>"
        f"<xProd>{it['xProd']}</xProd><NCM>{it['ncm']}</NCM>"
        + (f"<CEST>{cest}</CEST><indEscala>N</indEscala>" if cest else "")
        + f"<CNPJFab>{cnpj_emit}</CNPJFab>"
        f"<CFOP>{it['cfop']}</CFOP><uCom>{unidade}</uCom>"
        f"<qCom>{float(it['qCom']):.4f}</qCom><vUnCom>{float(it['vUnCom']):.10f}</vUnCom>"
        f"<vProd>{float(it['vProd']):.2f}</vProd>"
        f"<cEANTrib>{it.get('cEANTrib','SEM GTIN')}</cEANTrib>"
        f"<uTrib>{unidade}</uTrib>"
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
    "homologacao": "https://hportalsped.fazenda.mg.gov.br/portalnfce",
}


def _gerar_qrcode(chave, tp_amb, csc_id, csc, ambiente, dh_emi_hex=None):
    """Monta a URL do QR Code da NFC-e no formato EXATO dos cupons autorizados de MG:
        ...qrcode.xhtml?p=<chave>|<versaoQR>|<tpAmb>|<cIdToken>|<HASH>
    onde HASH = SHA1( "<chave>|<versaoQR>|<tpAmb>|<cIdToken>" + CSC ).hexdigest().upper()
    (modelo ONLINE, versao 2 do QR Code).
    """
    base = URL_QRCODE[ambiente]
    versao = "2"
    # cIdToken sem zeros a esquerda (cupons reais usam "1", nao "000001")
    id_token = str(csc_id).lstrip("0") or "0"
    dados = f"{chave}|{versao}|{tp_amb}|{id_token}"
    chash = hashlib.sha1((dados + csc).encode("utf-8")).hexdigest().upper()
    qr = f"{base}?p={dados}|{chash}"
    return qr


def _gerar_qrcode_offline(chave, tp_amb, csc_id, csc, ambiente, dia_emi, v_nf, dig_val):
    """QR Code v2 OFFLINE (contingencia tpEmis=9). Difere do online: inclui
    dia da emissao, valor total e DigestValue ANTES do cIdToken, e o hash SHA-1
    e calculado sobre TODOS esses parametros + CSC.

    Formato da URL (NT/Manual DANFE NFC-e v2 offline):
      ...?p=<chave>|<versao>|<tpAmb>|<diaEmi>|<vNF>|<digVal>|<cIdToken>|<HASH>
    onde HASH = SHA1("<chave>|2|<tpAmb>|<diaEmi>|<vNF>|<digVal>|<cIdToken>" + CSC).upper()

    - dia_emi: dois digitos (ex. "07"), so o DIA do dhEmi.
    - v_nf: valor total como string com ponto decimal (ex. "60.90"), 2 casas.
    - dig_val: o DigestValue da NFC-e convertido para HEXADECIMAL (lowercase),
      exatamente como exige o manual (entra o hexa do digest, nao o base64).
    """
    base = URL_QRCODE[ambiente]
    versao = "2"
    id_token = str(csc_id).lstrip("0") or "0"
    dados = f"{chave}|{versao}|{tp_amb}|{dia_emi}|{v_nf}|{dig_val}|{id_token}"
    chash = hashlib.sha1((dados + csc).encode("utf-8")).hexdigest().upper()
    qr = f"{base}?p={dados}|{chash}"
    return qr


def _digval_para_hexa(dig_val_b64):
    """Converte o DigestValue (base64, como aparece na tag <DigestValue> do XML)
    para a representacao HEXADECIMAL exigida no QR offline.

    ATENCAO (validado contra o exemplo oficial do Manual DANFE NFC-e):
    o digVal do QR e o HEXA DOS CARACTERES ASCII da string base64, NAO o hexa
    dos bytes decodificados. Exemplo oficial:
      base64  'yzGYhUx1/XYYzksWB+fPR3Qc50c='
      digVal  '797a4759685578312f5859597a6b7357422b6650523351633530633d'
    (que e cada caractere do base64 em hexa). Usar os bytes decodificados gera
    rejeicao 397 na retransmissao.
    """
    try:
        return str(dig_val_b64).encode("ascii").hex()
    except Exception:
        return str(dig_val_b64)


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
    # tp_emis: "1" = normal (online); "9" = contingencia offline NFC-e.
    # A nota pode pedir contingencia explicitamente (nota["tp_emis"]="9"); o
    # default continua "1" para nao alterar o fluxo online existente.
    tp_emis = str(nota.get("tp_emis", "1"))

    chave = montar_chave(cuf, dh, cnpj_emit, modelo, serie, numero, tp_emis, cnf)
    cnf_fmt = str(cnf).zfill(8)
    cdv = chave[-1]

    # itens (montagem propria da NFC-e, espelhando os cupons autorizados)
    # Em homologacao, a SEFAZ exige que o 1o item tenha esta descricao fixa (cStat 373).
    itens_emit = [dict(it) for it in nota["itens"]]
    if tp_amb == "2" and itens_emit:
        itens_emit[0]["xProd"] = "NOTA FISCAL EMITIDA EM AMBIENTE DE HOMOLOGACAO - SEM VALOR FISCAL"
    dets = "".join(_det_item_nfce(it, i + 1, cnpj_emit) for i, it in enumerate(itens_emit))
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

    # Totais de ICMS dos itens com CST 00 (tributacao integral): soma vBC e vICMS.
    # Itens CST 60 (ST) e 61 (mono) nao entram na BC de ICMS proprio.
    itens_cst00 = [it for it in nota["itens"] if str(it.get("cst_icms")) in ("00", "000")]
    v_bc_icms_tot = sum(float(it["vProd"]) for it in itens_cst00)
    v_icms_tot = sum(
        round(float(it["vProd"]) * float(it.get("aliq_icms") or 0) / 100, 2)
        for it in itens_cst00
    )

    # ide: NFC-e -> mod 65, tpImp 4 (DANFE NFC-e), indPres 1 (presencial)
    # Em contingencia offline (tp_emis=9), o schema exige dhCont (data/hora de
    # entrada em contingencia) e xJust (justificativa, 15-256 chars) APOS verProc.
    cont_xml = ""
    if tp_emis == "9":
        dh_cont = nota.get("dh_cont") or dh_emi
        x_just = (nota.get("x_just") or "Falha de comunicacao com a SEFAZ").strip()
        # xJust: minimo 15 caracteres (regra do schema). Garante o minimo.
        if len(x_just) < 15:
            x_just = (x_just + " - emissao offline NFC-e")[:256]
        cont_xml = f"<dhCont>{dh_cont}</dhCont><xJust>{x_just}</xJust>"

    ide = (
        f"<ide><cUF>{cuf}</cUF><cNF>{cnf_fmt}</cNF>"
        f"<natOp>{nota.get('natureza_op','VENDA AO CONSUMIDOR')}</natOp>"
        f"<mod>{modelo}</mod><serie>{serie}</serie><nNF>{numero}</nNF>"
        f"<dhEmi>{dh_emi}</dhEmi><tpNF>1</tpNF><idDest>1</idDest>"
        f"<cMunFG>{emit.get('c_mun','3123205')}</cMunFG>"
        f"<tpImp>4</tpImp><tpEmis>{tp_emis}</tpEmis><cDV>{cdv}</cDV>"
        f"<tpAmb>{tp_amb}</tpAmb><finNFe>1</finNFe><indFinal>1</indFinal>"
        f"<indPres>1</indPres><procEmi>0</procEmi><verProc>Octano1.0</verProc>"
        f"{cont_xml}</ide>"
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
        # indIEDest=9 (nao contribuinte) e obrigatorio no <dest> da NFC-e
        dest_xml = f"<dest><{tag_doc}>{doc_dest}</{tag_doc}><indIEDest>9</indIEDest></dest>"

    tag_qbcmono = f"<qBCMonoRet>{q_bc_mono:.2f}</qBCMonoRet>" if q_bc_mono > 0 else ""
    icmstot = (
        f"<ICMSTot><vBC>{v_bc_icms_tot:.2f}</vBC><vICMS>{v_icms_tot:.2f}</vICMS>"
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
    # retorna tambem tp_emis, valor total (vNF) e o dia da emissao (dd) — usados
    # na montagem do QR Code OFFLINE de contingencia.
    dia_emi = dh.strftime("%d")
    return nfe, chave, tp_amb, tp_emis, v_prod, dia_emi


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
        nfe_xml, chave, tp_amb, tp_emis, v_nf, dia_emi = montar_infnfce(nota, empresa, ambiente)

        # assina (mesma tecnica SHA-1 do modelo 55)
        xml_assinada = assinar_nfe(nfe_xml, cert_file, key_file)

        # extrai o DigestValue da assinatura (necessario p/ o QR offline)
        dig_val_b64 = None
        try:
            m = re.search(r"<DigestValue>([^<]+)</DigestValue>", xml_assinada)
            if m:
                dig_val_b64 = m.group(1)
        except Exception:
            dig_val_b64 = None

        # QR Code: ONLINE (tpEmis=1) ou OFFLINE de contingencia (tpEmis=9).
        # O offline inclui dia da emissao, valor total e DigestValue (hexa).
        if tp_emis == "9":
            v_nf_str = f"{float(v_nf):.2f}"
            dig_hexa = _digval_para_hexa(dig_val_b64) if dig_val_b64 else ""
            qr = _gerar_qrcode_offline(chave, tp_amb, csc_id, csc, ambiente,
                                       dia_emi, v_nf_str, dig_hexa)
        else:
            qr = _gerar_qrcode(chave, tp_amb, csc_id, csc, ambiente)
        url_chave = URL_CONSULTA[ambiente]
        supl = f"<infNFeSupl><qrCode><![CDATA[{qr}]]></qrCode><urlChave>{url_chave}</urlChave></infNFeSupl>"
        # insere o infNFeSupl logo apos </infNFe> dentro de <NFe>...</NFe>
        # (a Signature vem depois do infNFeSupl no layout, mas a ordem aceita e:
        #  infNFe, infNFeSupl, Signature). Como assinar_nfe ja inseriu a Signature,
        #  inserimos o supl ANTES da <Signature>.
        xml_final = xml_assinada.replace("<Signature", supl + "<Signature", 1)

        # CONTINGENCIA OFFLINE: nao envia a SEFAZ agora. Devolve o XML assinado
        # (com tpEmis=9) para o PDV/nucleo ENFILEIRAR e transmitir depois, quando
        # a SEFAZ voltar. O cupom e impresso imediatamente (DANFE de contingencia).
        if tp_emis == "9":
            return {
                "ok": True,
                "etapa": "contingencia",
                "contingencia": True,
                "chave": chave,
                "qrcode": qr,
                "xml_assinado": xml_final,
                "nfe_proc": None,
                "protocolo": None,
                "cstat_nfe": None,
                "xmotivo": "NFC-e emitida em contingencia offline (aguardando transmissao)",
            }

        # validacao XSD local: se o XML nao bate com o schema, retorna o erro EXATO
        # (elemento/linha) em vez de depender do 215 generico da SEFAZ.
        if nota.get("validar_xsd"):
            import os
            xsd_path = os.path.join(os.path.dirname(__file__), "schemas", "nfe_v4.00.xsd")
            if os.path.isfile(xsd_path):
                try:
                    schema = etree.XMLSchema(etree.parse(xsd_path))
                    doc = etree.fromstring(xml_final.encode("utf-8"))
                    if not schema.validate(doc):
                        erros = [f"linha {e.line}: {e.message}" for e in schema.error_log][:5]
                        return {"ok": False, "etapa": "xsd", "chave": chave,
                                "erro_xsd": erros, "xml_assinado": xml_final}
                except Exception as ex:
                    return {"ok": False, "etapa": "xsd_erro", "erro": str(ex), "xml_assinado": xml_final}

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


# ---------------------------------------------------------------------------
# CONTINGENCIA: status do servico + transmissao de XML ja assinado
# ---------------------------------------------------------------------------

# Webservice de Status do Servico da SEFAZ-MG (NFC-e, modelo 65)
URLS_STATUS_NFCE = {
    "producao":    "https://nfce.fazenda.mg.gov.br/nfce/services/NFeStatusServico4",
    "homologacao": "https://hnfce.fazenda.mg.gov.br/nfce/services/NFeStatusServico4",
}


def status_servico_nfce(cert_base64, cert_senha, ambiente="homologacao", uf="MG"):
    """Consulta o status do servico da SEFAZ (NFeStatusServico4). Usado para
    decidir se da para sair de contingencia e retransmitir a fila.
    Retorna {ok, online, cstat, xmotivo}. online=True quando cStat=107
    (Servico em Operacao).
    """
    tp_amb = "1" if ambiente == "producao" else "2"
    cuf = UF_CODIGO.get(uf, "31")
    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        cons = (
            f'<consStatServ versao="4.00" xmlns="{NS}">'
            f'<tpAmb>{tp_amb}</tpAmb><cUF>{cuf}</cUF><xServ>STATUS</xServ>'
            f'</consStatServ>'
        )
        wsdl_ns = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeStatusServico4"
        soap = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap12:Envelope xmlns:soap12="http://www.w3.org/2003/05/soap-envelope">'
            f'<soap12:Body><nfeDadosMsg xmlns="{wsdl_ns}">{cons}</nfeDadosMsg>'
            '</soap12:Body></soap12:Envelope>'
        )
        action = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeStatusServico4/nfeStatusServicoNF"
        ctype = f'application/soap+xml; charset=utf-8; action="{action}"'
        url = URLS_STATUS_NFCE[ambiente]
        resp = requests.post(
            url, data=soap.encode("utf-8"),
            headers={"Content-Type": ctype},
            cert=(cert_file, key_file), timeout=20, verify=True,
        )
        if resp.status_code != 200:
            return {"ok": False, "online": False, "etapa": "http",
                    "status": resp.status_code, "detalhes": resp.text[:300]}
        root = etree.fromstring(resp.content)
        cstat = root.findtext(f".//{{{NS}}}cStat")
        xmotivo = root.findtext(f".//{{{NS}}}xMotivo")
        return {"ok": True, "online": (cstat == "107"),
                "cstat": cstat, "xmotivo": xmotivo}
    except requests.exceptions.RequestException as e:
        # timeout/conexao = SEFAZ inalcancavel = continua offline
        return {"ok": False, "online": False, "etapa": "conexao", "erro": str(e)}
    finally:
        limpar_arquivos(cert_file, key_file)


def transmitir_nfce_assinada(xml_final, cert_base64, cert_senha, ambiente="homologacao"):
    """Transmite a SEFAZ um XML de NFC-e JA ASSINADO (vindo da fila de
    contingencia). NAO remonta nem reassina nada — a chave e o digest precisam
    ser identicos aos do cupom ja impresso. So envia e interpreta a resposta.

    Retorna a mesma estrutura de emitir_nfce (ok, cstat_nfe, xmotivo, protocolo,
    nfe_proc, etc.), para o nucleo decidir: 100 -> autorizada (sai da fila);
    duplicidade (204/539) -> tratar; demais -> mantem na fila / marca rejeitada.
    """
    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        # extrai a chave do proprio XML (Id="NFe<chave>")
        chave = None
        m = re.search(r'Id="NFe(\d{44})"', xml_final)
        if m:
            chave = m.group(1)

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
        # 539 = duplicidade de NFe com a mesma chave porem ja autorizada antes;
        # tratamos como "ja esta na base" (idempotente: pode sair da fila).
        ja_autorizada = (cstat_nfe == "100" or cstat_lote == "539" or cstat_nfe == "539")
        if autorizado and prot is not None:
            prot_xml = etree.tostring(prot, encoding="unicode")
            nfe_proc = (
                f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<nfeProc versao="4.00" xmlns="{NS}">'
                f'{xml_final}{prot_xml}</nfeProc>'
            )

        return {
            "ok": autorizado,
            "ja_autorizada": ja_autorizada,
            "etapa": "sefaz",
            "chave": chave,
            "cstat_lote": cstat_lote,
            "cstat_nfe": cstat_nfe,
            "xmotivo": xmotivo,
            "protocolo": nprot,
            "nfe_proc": nfe_proc,
            "sefaz_raw": resp.text[:2000] if not autorizado else None,
        }
    except requests.exceptions.RequestException as e:
        # SEFAZ ainda fora: NAO e rejeicao, e falha de comunicacao. Mantem na fila.
        return {"ok": False, "etapa": "conexao", "comunicacao_falhou": True, "erro": str(e)}
    finally:
        limpar_arquivos(cert_file, key_file)
