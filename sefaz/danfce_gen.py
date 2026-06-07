"""
sefaz/danfce_gen.py  -  DANFCE (cupom da NFC-e) em PDF 80mm, layout fiel ao convencional.
Gerador proprio com fpdf2 + qrcode (a brazilfiscalreport nao gera DANFCE).
Le os dados do XML autorizado (nfeProc).
"""

import re
from lxml import etree

NS = "http://www.portalfiscal.inf.br/nfe"


def _txt(el, tag):
    if el is None:
        return ""
    f = el.find(f".//{{{NS}}}{tag}")
    return f.text if f is not None and f.text else ""


def _moeda(v):
    try:
        return f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return "0,00"


def _num(v, casas=3):
    try:
        return f"{float(v):.{casas}f}".replace(".", ",")
    except Exception:
        return "0"


def _dh(dh):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", dh or "")
    if not m:
        return dh or ""
    a, mes, d, h, mi, s = m.groups()
    return f"{d}/{mes}/{a} {h}:{mi}:{s}"


def _fmt_cnpj(c):
    c = re.sub(r"\D", "", c or "")
    if len(c) == 14:
        return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"
    return c


def _fmt_cep(c):
    c = re.sub(r"\D", "", c or "")
    return f"{c[:5]}-{c[5:]}" if len(c) == 8 else c


def gerar_danfce_pdf(xml_proc, extras=None):
    from fpdf import FPDF
    import qrcode
    import io

    extras = extras or {}
    root = etree.fromstring(xml_proc.encode("utf-8") if isinstance(xml_proc, str) else xml_proc)

    inf = root.find(f".//{{{NS}}}infNFe")
    emit = root.find(f".//{{{NS}}}emit")
    ide = root.find(f".//{{{NS}}}ide")
    total = root.find(f".//{{{NS}}}total")
    dest = root.find(f".//{{{NS}}}dest")
    inf_supl = root.find(f".//{{{NS}}}infNFeSupl")
    prot = root.find(f".//{{{NS}}}infProt")
    ender = emit.find(f"{{{NS}}}enderEmit") if emit is not None else None

    chave = (inf.get("Id") or "").replace("NFe", "") if inf is not None else ""
    emit_nome = _txt(emit, "xNome")
    emit_cnpj = _fmt_cnpj(_txt(emit, "CNPJ"))
    emit_ie = _txt(emit, "IE")
    emit_lgr = _txt(ender, "xLgr")
    emit_nro = _txt(ender, "nro")
    emit_bairro = _txt(ender, "xBairro")
    emit_mun = _txt(ender, "xMun")
    emit_uf = _txt(ender, "UF")
    emit_cep = _fmt_cep(_txt(ender, "CEP"))

    numero = (_txt(ide, "nNF") or "").zfill(9)
    serie = (_txt(ide, "serie") or "").zfill(3)
    dh_emi = _dh(_txt(ide, "dhEmi"))
    tp_amb = _txt(ide, "tpAmb")

    v_nf = _txt(total, "vNF")
    v_desc = _txt(total, "vDesc")
    v_trib = _txt(total, "vTotTrib")

    n_prot = _txt(prot, "nProt")
    dh_recb = _dh(_txt(prot, "dhRecbto"))

    qr_code = _txt(inf_supl, "qrCode")
    url_chave = _txt(inf_supl, "urlChave") or "https://portalsped.fazenda.mg.gov.br/portalnfce"

    cpf_dest = _txt(dest, "CPF") or _txt(dest, "CNPJ")
    dets = root.findall(f".//{{{NS}}}det")

    # ---- PDF 80mm ----
    LARG = 80
    ML = 3
    XU = LARG - 2 * ML
    pdf = FPDF(orientation="P", unit="mm", format=(LARG, 500))
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.set_margins(ML, 4, ML)
    pdf.set_x(ML)
    F = "Courier"

    # helpers consistentes: cada um comeca/termina o cursor em X=ML, com ln=1
    def C(txt, size=7, style="", h=3.0):
        pdf.set_x(ML); pdf.set_font(F, style, size)
        pdf.multi_cell(XU, h, txt, align="C")
    def L(txt, size=7, style="", h=3.0):
        pdf.set_x(ML); pdf.set_font(F, style, size)
        pdf.multi_cell(XU, h, txt, align="L")
    def DOIS(esq, dir_, size=7, style="", h=3.4, frac=0.6):
        pdf.set_x(ML); pdf.set_font(F, style, size)
        pdf.cell(XU * frac, h, esq, align="L")
        pdf.cell(XU * (1 - frac), h, dir_, align="R")
        pdf.ln(h)
    def SEP():
        pdf.set_x(ML); pdf.set_font(F, "", 7)
        pdf.cell(XU, 2.6, "-" * 46, align="C"); pdf.ln(2.6)

    # ===== CABECALHO =====
    C(f"CNPJ: {emit_cnpj}", size=7, style="B", h=3.0)
    C(emit_nome, size=7, style="B", h=3.2)
    if emit_lgr:
        C(f"{emit_lgr}, {emit_nro} {emit_bairro}", size=6)
        C(f"{emit_mun}-{emit_uf} {emit_cep}", size=6)
    C(f"I.E.: {emit_ie}", size=6)
    SEP()
    C("Documento Auxiliar da Nota Fiscal", size=6, h=2.8)
    C("de Consumidor Eletronica", size=6, h=2.8)
    SEP()

    # ===== ITENS =====
    L("# Codigo Descricao", size=6, h=2.8)
    L("  Qtde Un X Valor unit. = Valor total", size=6, h=2.8)
    SEP()
    for idx, d in enumerate(dets, 1):
        prod = d.find(f"{{{NS}}}prod")
        cprod = _txt(prod, "cProd")
        xprod = _txt(prod, "xProd")
        qcom = _num(_txt(prod, "qCom"), 3)
        vun = _moeda(_txt(prod, "vUnCom"))
        vprod = _moeda(_txt(prod, "vProd"))
        ucom = _txt(prod, "uCom")
        L(f"{str(idx).zfill(3)} {cprod} {xprod}", size=6, h=2.9)
        DOIS(f"   {qcom} {ucom} X {vun}", vprod, size=6, h=2.9, frac=0.6)
    SEP()

    # ===== TOTAIS =====
    DOIS("Qtde. total de itens", str(len(dets)).zfill(3), size=7, h=3.2)
    DOIS("Valor total R$", _moeda(v_nf), size=9, style="B", h=4.5, frac=0.5)
    if v_desc and float(v_desc or 0) > 0:
        DOIS("Desconto R$", _moeda(v_desc), size=7, h=3.2)

    # ===== PAGAMENTO =====
    DOIS("FORMA DE PAGAMENTO", "VALOR PAGO R$", size=7, h=3.2, frac=0.5)
    FORMAS = {"01": "Dinheiro", "02": "Cheque", "03": "Cartao Credito",
              "04": "Cartao Debito", "05": "Credito Loja", "15": "Boleto",
              "17": "PIX", "99": "Outros"}
    for p in root.findall(f".//{{{NS}}}detPag"):
        DOIS(FORMAS.get(_txt(p, "tPag"), "Pagamento"), _moeda(_txt(p, "vPag")), size=7, h=3.2)
    SEP()

    # ===== CHAVE / CONSULTA =====
    C("Consulte pela Chave de Acesso em", size=6, h=2.8)
    C(url_chave, size=5, h=2.6)
    chave_fmt = " ".join(chave[i:i+4] for i in range(0, len(chave), 4))
    C(chave_fmt, size=6, h=2.8)
    if cpf_dest:
        C(f"CONSUMIDOR CPF/CNPJ: {cpf_dest}", size=6, h=2.8)
    else:
        C("CONSUMIDOR NAO IDENTIFICADO", size=6, h=2.8)
    if tp_amb == "2":
        C("*** HOMOLOGACAO - SEM VALOR FISCAL ***", size=6, style="B", h=3.0)
    SEP()

    # ===== DADOS NFC-e =====
    C(f"NFC-e n {numero} Serie {serie}", size=6, h=2.8)
    C(dh_emi, size=6, h=2.8)
    C(f"Protocolo de Autorizacao: {n_prot}", size=6, h=2.8)
    C(f"Data de Autorizacao {dh_recb}", size=6, h=2.8)

    # ===== QR CODE =====
    if qr_code:
        img = qrcode.make(qr_code)
        buf = io.BytesIO(); img.save(buf, format="PNG"); buf.seek(0)
        qs = 34
        pdf.ln(1)
        y = pdf.get_y()
        pdf.image(buf, x=(LARG - qs) / 2, y=y, w=qs, h=qs)
        pdf.set_y(y + qs + 2)
    SEP()

    # ===== ENCERRANTE (combustivel) =====
    enc_partes = []
    for d in dets:
        enc = d.find(f".//{{{NS}}}encerrante")
        if enc is not None:
            enc_partes.append(
                f"#NFC-e:nBico:{_txt(enc,'nBico')} nBomba:{_txt(enc,'nBomba')} "
                f"nTanque:{_txt(enc,'nTanque')} vEncIni:{_txt(enc,'vEncIni')} "
                f"vEncFin:{_txt(enc,'vEncFin')}"
            )
    if enc_partes:
        L(" ".join(enc_partes), size=5, h=2.5)

    # ===== TRIBUTOS (IBPT) =====
    trib = extras.get("tributos")
    if trib:
        L(trib, size=5, h=2.5)
    elif v_trib and float(v_trib or 0) > 0:
        pct = (float(v_trib) / float(v_nf) * 100) if float(v_nf or 0) else 0
        L(f"Val. Aprox. Tributos: R${_moeda(v_trib)}({pct:.2f}%) Fonte IBPT", size=5, h=2.5)

    # ===== VENDEDOR / OPERADOR =====
    vend = extras.get("vendedor"); oper = extras.get("operador"); turno = extras.get("turno")
    if vend or oper:
        linha = ""
        if vend: linha += f"Vendedor: {vend} "
        if oper: linha += f"Operador: {oper}"
        if turno: linha += f" Turno:{turno}"
        L(linha, size=5, h=2.5)

    out = pdf.output()
    return bytes(out)
