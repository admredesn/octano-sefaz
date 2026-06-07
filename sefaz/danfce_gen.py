"""
sefaz/danfce_gen.py  -  Gera o DANFCE (cupom da NFC-e) em PDF formato 80mm.

A brazilfiscalreport NAO gera DANFCE, entao montamos o cupom a mao com fpdf2
(ja disponivel, e dependencia da brazilfiscalreport) + qrcode para o QR Code.
Le os dados direto do XML autorizado (nfeProc).

Layout: bobina de 80mm de largura (impressora termica), altura dinamica.
"""

import re
from lxml import etree

NS = "http://www.portalfiscal.inf.br/nfe"


def _txt(el, tag):
    if el is None:
        return ""
    found = el.find(f".//{{{NS}}}{tag}")
    return found.text if found is not None and found.text else ""


def _fmt_moeda(v):
    try:
        return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "0,00"


def _fmt_qtd(v):
    try:
        return f"{float(v):.3f}".replace(".", ",")
    except Exception:
        return "0"


def _fmt_dh(dh):
    # 2026-06-02T08:53:50-03:00 -> 02/06/2026 08:53:50
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", dh or "")
    if not m:
        return dh or ""
    a, mes, d, h, mi, s = m.groups()
    return f"{d}/{mes}/{a} {h}:{mi}:{s}"


def gerar_danfce_pdf(xml_proc):
    """Recebe o nfeProc (str) e devolve os bytes do PDF do cupom 80mm."""
    from fpdf import FPDF
    import qrcode
    import io

    root = etree.fromstring(xml_proc.encode("utf-8") if isinstance(xml_proc, str) else xml_proc)

    inf = root.find(f".//{{{NS}}}infNFe")
    emit = root.find(f".//{{{NS}}}emit")
    ide = root.find(f".//{{{NS}}}ide")
    total = root.find(f".//{{{NS}}}total")
    dest = root.find(f".//{{{NS}}}dest")
    inf_supl = root.find(f".//{{{NS}}}infNFeSupl")
    prot = root.find(f".//{{{NS}}}infProt")

    chave = (inf.get("Id") or "").replace("NFe", "") if inf is not None else ""
    emit_nome = _txt(emit, "xNome")
    emit_cnpj = _txt(emit, "CNPJ")
    ender = emit.find(f"{{{NS}}}enderEmit") if emit is not None else None
    emit_lgr = _txt(ender, "xLgr")
    emit_nro = _txt(ender, "nro")
    emit_bairro = _txt(ender, "xBairro")
    emit_mun = _txt(ender, "xMun")
    emit_uf = _txt(ender, "UF")
    emit_ie = _txt(emit, "IE")

    numero = _txt(ide, "nNF")
    serie = _txt(ide, "serie")
    dh_emi = _fmt_dh(_txt(ide, "dhEmi"))
    tp_amb = _txt(ide, "tpAmb")

    v_nf = _txt(total, "vNF")
    v_prod = _txt(total, "vProd")
    v_desc = _txt(total, "vDesc")

    n_prot = _txt(prot, "nProt")
    dh_recb = _fmt_dh(_txt(prot, "dhRecbto"))

    qr_code = _txt(inf_supl, "qrCode")
    url_chave = _txt(inf_supl, "urlChave")

    # consumidor
    cpf_dest = _txt(dest, "CPF") or _txt(dest, "CNPJ")

    # itens
    dets = root.findall(f".//{{{NS}}}det")

    # ---- monta o PDF 80mm ----
    largura = 80  # mm
    pdf = FPDF(orientation="P", unit="mm", format=(largura, 297))
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.set_margins(4, 4, 4)
    x_util = largura - 8  # area util

    def linha_sep(char="-"):
        pdf.set_font("Courier", "", 7)
        pdf.cell(x_util, 3, char * 46, ln=1, align="C")

    def texto(t, size=7, style="", align="L", h=3.2):
        pdf.set_font("Courier", style, size)
        # quebra manual para caber na largura
        pdf.multi_cell(x_util, h, t, align=align)

    # Cabecalho - emitente
    texto(emit_nome, size=8, style="B", align="C", h=3.6)
    texto(f"CNPJ: {emit_cnpj}   IE: {emit_ie}", size=6, align="C")
    end_txt = f"{emit_lgr}, {emit_nro} - {emit_bairro}"
    texto(end_txt, size=6, align="C")
    texto(f"{emit_mun} - {emit_uf}", size=6, align="C")
    linha_sep()
    texto("DANFE NFC-e - Documento Auxiliar", size=6, align="C")
    texto("da Nota Fiscal de Consumidor Eletronica", size=6, align="C")
    if tp_amb == "2":
        texto("*** HOMOLOGACAO - SEM VALOR FISCAL ***", size=6, style="B", align="C")
    linha_sep()

    # Itens - cabecalho
    pdf.set_font("Courier", "B", 6)
    pdf.cell(x_util, 3, "COD  DESCRICAO", ln=1)
    pdf.cell(x_util, 3, "QTD x UNIT             TOTAL", ln=1)
    linha_sep()
    for d in dets:
        prod = d.find(f"{{{NS}}}prod")
        cprod = _txt(prod, "cProd")
        xprod = _txt(prod, "xProd")
        qcom = _fmt_qtd(_txt(prod, "qCom"))
        vun = _fmt_moeda(_txt(prod, "vUnCom"))
        vprod = _fmt_moeda(_txt(prod, "vProd"))
        ucom = _txt(prod, "uCom")
        pdf.set_font("Courier", "", 6)
        pdf.multi_cell(x_util, 3, f"{cprod} {xprod}", align="L")
        pdf.cell(x_util, 3, f"{qcom} {ucom} x {vun}      {vprod}", ln=1, align="L")
    linha_sep()

    # Totais
    pdf.set_font("Courier", "", 7)
    n_itens = len(dets)
    pdf.cell(x_util / 2, 3.4, f"Qtd itens: {n_itens}", align="L")
    pdf.cell(x_util / 2, 3.4, "", ln=1)
    if v_desc and float(v_desc or 0) > 0:
        pdf.cell(x_util / 2, 3.4, "Desconto", align="L")
        pdf.cell(x_util / 2, 3.4, _fmt_moeda(v_desc), ln=1, align="R")
    pdf.set_font("Courier", "B", 9)
    pdf.cell(x_util / 2, 5, "TOTAL R$", align="L")
    pdf.cell(x_util / 2, 5, _fmt_moeda(v_nf), ln=1, align="R")

    # Pagamento
    pdf.set_font("Courier", "", 7)
    pags = root.findall(f".//{{{NS}}}detPag")
    FORMAS = {"01": "Dinheiro", "02": "Cheque", "03": "Cartao Credito",
              "04": "Cartao Debito", "05": "Credito Loja", "15": "Boleto",
              "17": "PIX", "99": "Outros"}
    for p in pags:
        tpag = _txt(p, "tPag")
        vpag = _fmt_moeda(_txt(p, "vPag"))
        pdf.cell(x_util / 2, 3.4, FORMAS.get(tpag, "Pagamento"), align="L")
        pdf.cell(x_util / 2, 3.4, vpag, ln=1, align="R")
    linha_sep()

    # Consumidor
    if cpf_dest:
        texto(f"Consumidor CPF/CNPJ: {cpf_dest}", size=6, align="C")
    else:
        texto("Consumidor nao identificado", size=6, align="C")
    linha_sep()

    # Dados da NFC-e
    texto(f"NFC-e No {numero}  Serie {serie}", size=6, align="C")
    texto(f"Emissao: {dh_emi}", size=6, align="C")
    texto("Chave de acesso:", size=6, align="C")
    # chave em grupos de 4
    chave_fmt = " ".join(chave[i:i+4] for i in range(0, len(chave), 4))
    texto(chave_fmt, size=6, align="C")
    texto("Consulte pela chave em:", size=6, align="C")
    texto(url_chave, size=5, align="C")
    if n_prot:
        texto(f"Protocolo: {n_prot}", size=6, align="C")
        texto(f"Autorizado em: {dh_recb}", size=6, align="C")
    linha_sep()

    # QR Code
    if qr_code:
        img = qrcode.make(qr_code)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        qr_size = 38
        x_qr = (largura - qr_size) / 2
        pdf.image(buf, x=x_qr, y=pdf.get_y() + 1, w=qr_size, h=qr_size)
        pdf.ln(qr_size + 2)
    texto("Consulta via leitor de QR Code", size=5, align="C")

    out = pdf.output()
    return bytes(out)
