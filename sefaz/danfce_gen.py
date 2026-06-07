"""
sefaz/danfce_gen.py  -  Gera o DANFCE (cupom da NFC-e) em PDF formato 80mm.

Layout fiel ao cupom convencional (DANFCE padrao SEFAZ-MG).
A brazilfiscalreport NAO gera DANFCE; montamos com fpdf2 + qrcode.
Le os dados direto do XML autorizado (nfeProc).

Camada 1: layout completo com dados do XML.
- Encerrante (combustivel) lido do XML.
- Linha de tributos (IBPT) e Vendedor/Operador: placeholders, recebem dados depois.
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
    if len(c) == 8:
        return f"{c[:5]}-{c[5:]}"
    return c


def gerar_danfce_pdf(xml_proc, extras=None):
    """Recebe o nfeProc (str) e devolve os bytes do PDF do cupom 80mm.
    extras (opcional): dict com 'vendedor', 'operador', 'turno', 'tributos' (IBPT)."""
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
    v_prod = _txt(total, "vProd")
    v_desc = _txt(total, "vDesc")
    v_trib = _txt(total, "vTotTrib")

    n_prot = _txt(prot, "nProt")
    dh_recb = _dh(_txt(prot, "dhRecbto"))

    qr_code = _txt(inf_supl, "qrCode")
    url_chave = _txt(inf_supl, "urlChave") or "https://portalsped.fazenda.mg.gov.br/portalnfce"

    cpf_dest = _txt(dest, "CPF") or _txt(dest, "CNPJ")
    dets = root.findall(f".//{{{NS}}}det")

    # ---- PDF 80mm ----
    largura = 80
    pdf = FPDF(orientation="P", unit="mm", format=(largura, 400))
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    ml = 3
    pdf.set_margins(ml, 4, ml)
    xu = largura - 2 * ml  # largura util

    FONT = "Courier"
    LARG_CHARS = 46  # caracteres por linha em Courier 7

    def C(txt, size=7, style="", h=3.0):
        pdf.set_font(FONT, style, size)
        pdf.multi_cell(xu, h, txt, align="C")

    def L(txt, size=7, style="", h=3.0):
        pdf.set_font(FONT, style, size)
        pdf.multi_cell(xu, h, txt, align="L")

    def LR(esq, dir_, size=7, style="", h=3.2):
        pdf.set_font(FONT, style, size)
        pdf.cell(xu * 0.6, h, esq, align="L")
        pdf.cell(xu * 0.4, h, dir_, align="R", ln=1)

    def sep():
        pdf.set_font(FONT, "", 7)
        pdf.cell(xu, 2.6, "-" * LARG_CHARS, ln=1, align="C")

    # ===== CABECALHO =====
    C(f"CNPJ: {emit_cnpj} {emit_nome}", size=7, style="B", h=3.2)
    C(f"{emit_lgr}, {emit_nro} {emit_bairro} {emit_mun}-{emit_uf} {emit_cep}", size=6)
    C(f"I.E.: {emit_ie}", size=6)
    C("Documento Auxiliar da Nota Fiscal de Consumidor Eletronica", size=6, h=3.0)

    # ===== ITENS =====
    pdf.set_font(FONT, "", 6)
    pdf.cell(xu, 3, "# Codigo Descricao  Qtde Un Valor unit. Valor total", ln=1)
    for idx, d in enumerate(dets, 1):
        prod = d.find(f"{{{NS}}}prod")
        cprod = _txt(prod, "cProd")
        xprod = _txt(prod, "xProd")
        qcom = _num(_txt(prod, "qCom"), 3)
        vun = _moeda(_txt(prod, "vUnCom"))
        vprod = _moeda(_txt(prod, "vProd"))
        ucom = _txt(prod, "uCom")
        # linha 1: seq + codigo + descricao
        pdf.set_font(FONT, "", 6)
        pdf.multi_cell(xu, 3, f"{str(idx).zfill(3)} {cprod} {xprod}", align="L")
        # linha 2: qtd x unit ........ total (alinhado a direita)
        pdf.cell(xu * 0.55, 3, f"   {qcom} {ucom} X {vun}", align="L")
        pdf.cell(xu * 0.45, 3, vprod, align="R", ln=1)

    # ===== TOTAIS =====
    LR("Qtde. total de itens", str(len(dets)).zfill(3), size=7)
    pdf.set_font(FONT, "B", 9)
    pdf.cell(xu * 0.5, 4.5, "Valor total R$", align="L")
    pdf.cell(xu * 0.5, 4.5, _moeda(v_nf), align="R", ln=1)
    if v_desc and float(v_desc or 0) > 0:
        LR("Desconto R$", _moeda(v_desc), size=7)

    # ===== PAGAMENTO =====
    pdf.set_font(FONT, "", 7)
    pdf.cell(xu * 0.5, 3.4, "FORMA DE PAGAMENTO", align="L")
    pdf.cell(xu * 0.5, 3.4, "VALOR PAGO R$", align="R", ln=1)
    FORMAS = {"01": "Dinheiro", "02": "Cheque", "03": "Cartao Credito",
              "04": "Cartao Debito", "05": "Credito Loja", "15": "Boleto",
              "17": "PIX", "99": "Outros"}
    for p in root.findall(f".//{{{NS}}}detPag"):
        LR(FORMAS.get(_txt(p, "tPag"), "Pagamento"), _moeda(_txt(p, "vPag")), size=7)

    # ===== CHAVE / CONSULTA =====
    C("Consulte pela Chave de Acesso em", size=6)
    C(url_chave, size=5)
    chave_fmt = " ".join(chave[i:i+4] for i in range(0, len(chave), 4))
    C(chave_fmt, size=6)
    if cpf_dest:
        C(f"CONSUMIDOR CPF/CNPJ: {cpf_dest}", size=6)
    else:
        C("CONSUMIDOR NAO IDENTIFICADO", size=6)

    if tp_amb == "2":
        C("*** HOMOLOGACAO - SEM VALOR FISCAL ***", size=6, style="B")

    # ===== DADOS NFC-e =====
    C(f"NFC-e n {numero} Serie {serie} {dh_emi}", size=6)
    C(f"Protocolo de Autorizacao: {n_prot}", size=6)
    C(f"Data de Autorizacao {dh_recb}", size=6)

    # ===== QR CODE =====
    if qr_code:
        img = qrcode.make(qr_code)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        qr_size = 36
        pdf.ln(1)
        pdf.image(buf, x=(largura - qr_size) / 2, y=pdf.get_y(), w=qr_size, h=qr_size)
        pdf.ln(qr_size + 1)

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
        L(" ".join(enc_partes), size=5, h=2.6)

    # ===== TRIBUTOS (IBPT) =====
    # Camada 1: usa vTotTrib do XML se houver; extras pode trazer detalhado depois.
    trib = extras.get("tributos")
    if trib:
        L(trib, size=5, h=2.6)
    elif v_trib and float(v_trib or 0) > 0:
        pct = (float(v_trib) / float(v_nf) * 100) if float(v_nf or 0) else 0
        L(f"Val. Aprox. Tributos: R${_moeda(v_trib)}({pct:.2f}%) Fonte IBPT", size=5, h=2.6)

    # ===== VENDEDOR / OPERADOR =====
    vend = extras.get("vendedor")
    oper = extras.get("operador")
    turno = extras.get("turno")
    if vend or oper:
        linha = ""
        if vend:
            linha += f"Vendedor: {vend} "
        if oper:
            linha += f"Operador: {oper}"
        if turno:
            linha += f" Turno:{turno}"
        L(linha, size=5, h=2.6)

    out = pdf.output()
    return bytes(out)
