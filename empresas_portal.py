# -*- coding: utf-8 -*-
"""
empresas_portal.py — PORTAL EMPRESAS (web) da Rede SN (14/10/2026, pedido do Ronan).

Empresa com frota acompanha, pelo navegador: placas (cadastrar, desativar), consumo de CADA abastecimento com a foto do
cupom assinado, faturas (PDF, Excel), Pix e boleto novo. Mesmo login do app (CPF + senha, /cashback/api/login).

QUEM ENTRA: só o FINANCEIRO — a pessoa (CPF) que é colaborador da empresa com "vê as faturas" marcado
(oct_pessoas.app_ve_faturas) E a empresa está com o portal ligado na ficha (oct_pessoas.portal_empresas). O motorista nunca
vê fatura da empresa: ele só enxerga o próprio consumo e a placa vinculada ao cadastro dele, e isso é no app, não aqui.

Rotas (todas JSON, Authorization: Bearer <token do login>):
  GET  /empresas/api/eu                                   quem sou e quais empresas posso administrar
  GET  /empresas/api/visao?empresa=                       resumo, consumo por placa (mês / 30 / 90 dias), faturas
  GET  /empresas/api/placas?empresa=                      lista de placas com situação
  GET  /empresas/api/colaboradores?empresa=               motoristas vinculados (para escolher na placa)
  POST /empresas/api/placas                               cadastra (ou reativa) uma placa
  POST /empresas/api/placas/<id>                          edita veículo / motorista
  POST /empresas/api/placas/<id>/ativo                    ativa / desativa (desativar também bloqueia no caixa)
  POST /empresas/api/placas/importar                      lista colada (aplicar=false confere, true grava)
  GET  /empresas/api/consumo?empresa=&de=&ate=&placa=...  cada abastecimento + totais + resumo por placa
  GET  /empresas/api/consumo/export?formato=xlsx|pdf      o mesmo, em arquivo
  GET  /empresas/api/cupom/<nota_id>?empresa=             dados do cupom + link temporário da foto assinada
  GET  /empresas/api/faturas?empresa=                     lista (com multa e juros se vencida e boleto/Pix)
  GET  /empresas/api/faturas/<id>?empresa=                detalhe: compras por placa
  GET  /empresas/api/faturas/<id>/arquivo?tipo=fatura|boleto
  GET  /empresas/api/faturas/export?formato=xlsx|pdf      lista de faturas
  GET  /empresas/api/faturas/<id>/export                  compras da fatura em Excel
  POST /empresas/api/faturas/<id>/pix                     pede o Pix (valor com multa e juros se vencida)
  GET  /empresas/api/pix/<id>   |  /empresas/api/pix/<id>/qr.png
  POST /empresas/api/faturas/<id>/boleto                  gera NOVO boleto (vence hoje, com multa e juros) e cancela o antigo
  GET  /empresas/api/faturas/<id>/boleto                  estado do pedido de boleto
Multa e juros: parâmetros do posto (Parâmetros › Cobrança): cobranca_multa_pct e cobranca_juros_mes_pct.
"""
import datetime as dt
import io
import re
import time
import zipfile
from xml.sax.saxutils import escape as _xesc

from flask import Blueprint, Response, jsonify, request

import cashback_portal as cp
import cliente_faturas as cf

bp_empresas = Blueprint("empresas_portal", __name__)
SEM_CACHE = {"Cache-Control": "no-store"}
ABERTAS_PIX_ALVO = ("pedido", "criando", "ativa")
_RE_PLACA = re.compile(r"^[A-Z]{3}\d[A-Z0-9]\d{2}$")


# ------------------------------------------------------------------ utilidades
def _hoje_br():
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).date()


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def _brl(v):
    s = f"{abs(float(v)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if float(v) < 0 else "") + "R$ " + s


def _norm_placa(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _fmt_placa(p):
    p = _norm_placa(p)
    if len(p) == 7 and p[:3].isalpha() and p[3:].isdigit():
        return p[:3] + "-" + p[3:]               # modelo antigo AAA-0000
    return p


def _placa_ok(p):
    return bool(_RE_PLACA.match(_norm_placa(p)))


def _iso_data(s, padrao=None):
    s = str(s or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        return s
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return padrao


def _data_br(iso):
    iso = str(iso or "")[:10]
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}" if len(iso) == 10 else ""


def _hora_br(iso_ts):
    """'2026-10-14T21:52:12.996+00:00' -> '14/10 18:52' (hora de Brasília)."""
    try:
        t = dt.datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")) - dt.timedelta(hours=3)
        return t.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return ""


def _log(cli, empresa, acao, detalhe=None):
    print(f"[empresas] cpf {cp._cpf_mascara(cli['cpf'])} {empresa[:8]} {acao}")
    try:
        cp._spost("oct_portal_empresas_log", {"pessoa_id": empresa, "cpf_mascara": cp._cpf_mascara(cli["cpf"]),
                                              "acao": acao, "detalhe": detalhe or {}}, prefer="return=minimal")
    except Exception:
        pass                                  # trilha é "melhor esforço": nunca derruba a ação


# ------------------------------------------------------------------ multa e juros
_PARAMS = {}


def _params_cobranca(posto_id):
    """(multa_pct, juros_mes_pct) do posto; ausente/inválido = 0 (sem encargo)."""
    c = _PARAMS.get(posto_id)
    if c and time.time() - c[0] < 60:
        return c[1]
    multa = juros = 0.0
    try:
        rows = cp._sget(f"oct_parametros?empresa_id=eq.{posto_id}&chave=in.(cobranca_multa_pct,cobranca_juros_mes_pct)&select=chave,valor")
        for r in rows or []:
            v = str(r.get("valor") if r.get("valor") is not None else "").strip().strip('"').replace(",", ".")
            n = _num(v, 0.0)
            if r.get("chave") == "cobranca_multa_pct":
                multa = max(0.0, min(100.0, n))
            elif r.get("chave") == "cobranca_juros_mes_pct":
                juros = max(0.0, min(100.0, n))
    except Exception:
        pass
    _PARAMS[posto_id] = (time.time(), (multa, juros))
    return multa, juros


def encargos(saldo, vencimento_iso, hoje, multa_pct, juros_mes_pct):
    """Multa (uma vez, % do saldo) + juros (% ao mês, proporcional: ao dia = mês/30) por dia de atraso.
    -> {dias, multa, juros, total}. Sem atraso (vence hoje ou depois) = tudo zero."""
    try:
        venc = dt.date.fromisoformat(str(vencimento_iso)[:10])
    except Exception:
        return {"dias": 0, "multa": 0.0, "juros": 0.0, "total": 0.0}
    dias = (hoje - venc).days
    if dias <= 0 or saldo <= 0:
        return {"dias": max(dias, 0), "multa": 0.0, "juros": 0.0, "total": 0.0}
    multa = round(saldo * multa_pct / 100.0, 2)
    juros = round(saldo * (juros_mes_pct / 100.0 / 30.0) * dias, 2)
    return {"dias": dias, "multa": multa, "juros": juros, "total": round(multa + juros, 2)}


# ------------------------------------------------------------------ acesso
_ACESSO = {}


def _empresas(cli):
    """Empresas (pessoa PJ de um posto) que este CPF administra: colaborador com 'vê as faturas' e portal ligado."""
    cpf = cli["cpf"]
    c = _ACESSO.get(cpf)
    if c and time.time() - c[0] < 20:
        return c[1]
    try:
        eu = cp._sget(f"oct_pessoas?documento=eq.{cpf}&select=id,nome,empresa_id,frota_empresa_id,app_ve_faturas&limit=50")
    except Exception:
        eu = []
    alvos = sorted({r["frota_empresa_id"] for r in eu or [] if r.get("frota_empresa_id") and r.get("app_ve_faturas") is True})
    out = []
    if alvos:
        try:
            emps = cp._sget(f"oct_pessoas?id=in.({','.join(alvos)})&select=id,nome,documento,empresa_id,portal_empresas,ativo,"
                            f"limite_nota_prazo&limit=50")
        except Exception:
            emps = []                         # coluna portal_empresas ainda não criada: portal desligado
        for e in emps or []:
            if e.get("portal_empresas") is True and e.get("ativo") is not False:
                nome_posto = ""
                try:
                    nome_posto = cf._nome_empresa(e["empresa_id"])
                except Exception:
                    pass
                out.append({"id": e["id"], "nome": e.get("nome") or "", "documento": e.get("documento") or "",
                            "posto_id": e["empresa_id"], "posto": nome_posto, "limite": _num(e.get("limite_nota_prazo"))})
    nome = (eu[0].get("nome") if eu else "") or ""
    _ACESSO[cpf] = (time.time(), (out, nome))
    return out, nome


def _acesso(cli, empresa_id):
    """(empresa, None) ou (None, resposta de erro)."""
    pid = cp._uuid_ok(empresa_id)
    emps, _nome = _empresas(cli)
    e = next((x for x in emps if x["id"] == pid), None) if pid else None
    if not e:
        return None, (jsonify({"erro": "Empresa não encontrada ou sem acesso ao Portal Empresas."}), 404)
    return e, None


def _entrada():
    cli, erro = cf._cliente_ou_401()
    return cli, erro


# ------------------------------------------------------------------ consumo (vendas a prazo da empresa)
def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _consumo_linhas(emp, de_iso, ate_iso):
    """Uma linha por item de cada compra a prazo da empresa no período, já com placa/km/motorista/foto/fatura."""
    q = (f"oct_pdv_notas_prazo?empresa_id=eq.{emp['posto_id']}&cliente_id=eq.{emp['id']}"
         f"&registrado_em=gte.{de_iso}T00:00:00-03:00&registrado_em=lte.{ate_iso}T23:59:59-03:00"
         f"&select=id,numero_nfe,valor,registrado_em,foto_path,fatura_id,status,assinado_por,assinado_em"
         f"&order=registrado_em.desc&limit=3000")
    notas = [n for n in (cp._sget(q) or []) if str(n.get("status") or "").lower() not in ("cancelado", "cancelada")]
    return _linhas_de_notas(emp, notas)


def _dia_br(ts):
    """'2026-10-15T00:36:59+00:00' -> '2026-10-14' (dia em Brasília)."""
    try:
        return (dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")) - dt.timedelta(hours=3)).date().isoformat()
    except Exception:
        return str(ts or "")[:10]


def _linhas_de_notas(emp, notas):
    """Junta cada compra a prazo (nota) com a venda do PDV: placa, km, motorista, itens, fatura e foto."""
    nums = sorted({str(n["numero_nfe"]) for n in notas if str(n.get("numero_nfe") or "").isdigit()})
    vendas = {}
    for lote in _chunks(nums, 120):
        for v in cp._sget(f"oct_pdv_vendas?empresa_id=eq.{emp['posto_id']}&numero=in.({','.join(lote)})"
                          f"&select=numero,data_venda,placa,odometro,veiculo,itens,prazo_dados&limit=500") or []:
            vendas[str(v["numero"])] = v
    fat_ids = sorted({n["fatura_id"] for n in notas if n.get("fatura_id")})
    fat_num = {}
    for lote in _chunks(fat_ids, 80):
        for f in cp._sget(f"oct_faturas?id=in.({','.join(lote)})&select=id,numero&limit=200") or []:
            fat_num[f["id"]] = f.get("numero")
    linhas = []
    for n in notas:
        v = vendas.get(str(n.get("numero_nfe")))
        pd = (v or {}).get("prazo_dados") if isinstance((v or {}).get("prazo_dados"), dict) else {}
        placa = _norm_placa((v or {}).get("placa") or pd.get("placa"))
        km = (v or {}).get("odometro") or pd.get("km")
        quando = (v or {}).get("data_venda") or n.get("registrado_em")
        base = {
            "nota_id": n["id"], "cupom": n.get("numero_nfe"), "quando": quando, "placa": placa,
            "veiculo": (v or {}).get("veiculo") or pd.get("veiculo") or "", "motorista": str(pd.get("motorista") or "").strip(),
            "km": int(re.sub(r"\D", "", str(km))) if re.sub(r"\D", "", str(km or "")) else None,
            "fatura_id": n.get("fatura_id"), "fatura": fat_num.get(n.get("fatura_id")),
            "tem_foto": bool(n.get("foto_path")), "assinado_por": n.get("assinado_por"),
        }
        itens = (v or {}).get("itens") if isinstance((v or {}).get("itens"), list) else []
        if not itens:
            linhas.append(dict(base, produto="Compra a prazo", combustivel=False, litros=None, preco=None, valor=round(_num(n.get("valor")), 2)))
            continue
        for it in itens:
            qtd = _num(it.get("qtd"))
            unit = _num(it.get("unit"))
            total = _num(it.get("total"), qtd * unit)
            comb = it.get("tipo") == "abastecimento"
            linhas.append(dict(base, produto=str(it.get("desc") or it.get("produto") or "").strip(), combustivel=comb,
                               litros=round(qtd, 2) if comb else None, preco=round(unit, 3) if comb else None, valor=round(total, 2)))
    linhas.sort(key=lambda l: str(l["quando"] or ""), reverse=True)
    return linhas


def _filtrar(linhas, placa="", produto="", fatura=""):
    placa = _norm_placa(placa)
    produto = str(produto or "").strip().lower()
    fatura = str(fatura or "").strip().lower()
    out = []
    for l in linhas:
        if placa and l["placa"] != placa:
            continue
        if produto and produto not in str(l["produto"]).lower():
            continue
        if fatura == "a_faturar" and l["fatura_id"]:
            continue
        if fatura and fatura != "a_faturar" and str(l["fatura"] or "") != fatura:
            continue
        out.append(l)
    return out


def _totais(linhas):
    comb = [l for l in linhas if l["combustivel"]]
    litros = sum(l["litros"] or 0 for l in comb)
    valor_comb = sum(l["valor"] for l in comb)
    return {"abastecimentos": len(comb), "litros": round(litros, 2), "valor": round(sum(l["valor"] for l in linhas), 2),
            "preco_medio": round(valor_comb / litros, 3) if litros > 0 else None}


def _resumo_placas(linhas):
    por = {}
    for l in linhas:
        if not l["placa"]:
            continue
        r = por.setdefault(l["placa"], {"placa": l["placa"], "veiculo": l["veiculo"], "abastecimentos": 0, "litros": 0.0,
                                        "valor": 0.0, "_km": []})
        r["valor"] += l["valor"]
        if l["combustivel"]:
            r["abastecimentos"] += 1
            r["litros"] += l["litros"] or 0
            if l["km"]:
                r["_km"].append((l["km"], l["litros"] or 0))
        if not r["veiculo"] and l["veiculo"]:
            r["veiculo"] = l["veiculo"]
    out = []
    for r in por.values():
        km = sorted(r.pop("_km"))
        rod = (km[-1][0] - km[0][0]) if len(km) >= 2 else None
        litros_uso = sum(x[1] for x in km[1:]) if len(km) >= 2 else 0           # o 1º abastecimento enche o tanque, não conta
        r["km_rodados"] = rod if rod and rod > 0 else None
        r["media_kml"] = round(rod / litros_uso, 2) if rod and rod > 0 and litros_uso > 0 else None
        r["litros"] = round(r["litros"], 2)
        r["valor"] = round(r["valor"], 2)
        out.append(r)
    out.sort(key=lambda x: x["litros"], reverse=True)
    return out


def _periodo(args):
    hoje = _hoje_br()
    ate = _iso_data(args.get("ate"), hoje.isoformat())
    de = _iso_data(args.get("de"), hoje.replace(day=1).isoformat())
    if de > ate:
        de = ate
    return de, ate


# ------------------------------------------------------------------ Excel (sem biblioteca: zip + XML)
def _col(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def xlsx_bytes(aba, cabecalhos, linhas, larguras=None):
    """Planilha de uma aba. Células numéricas ficam numéricas; o resto vai como texto. Cabeçalho em negrito."""
    def cel(ref, v, estilo=0):
        if v is None or v == "":
            return f'<c r="{ref}" s="{estilo}"/>' if estilo else ""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return f'<c r="{ref}" s="{estilo}"><v>{v}</v></c>'
        return f'<c r="{ref}" s="{estilo}" t="inlineStr"><is><t xml:space="preserve">{_xesc(str(v))}</t></is></c>'
    rows = ['<row r="1">' + "".join(cel(f"{_col(i)}1", h, 1) for i, h in enumerate(cabecalhos)) + "</row>"]
    for r, lin in enumerate(linhas, start=2):
        rows.append(f'<row r="{r}">' + "".join(cel(f"{_col(i)}{r}", v, 2 if isinstance(v, float) else 0) for i, v in enumerate(lin)) + "</row>")
    cols = ""
    if larguras:
        cols = "<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(larguras)) + "</cols>"
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             f'{cols}<sheetData>{"".join(rows)}</sheetData></worksheet>')
    estilos = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
               '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
               '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
               '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
               '<fill><patternFill patternType="solid"><fgColor rgb="FFE7EEF5"/></patternFill></fill></fills>'
               '<borders count="1"><border/></borders>'
               '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
               '<cellXfs count="3"><xf/><xf fontId="1" fillId="2" applyFont="1" applyFill="1"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs>'
               '</styleSheet>')
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f'<sheets><sheet name="{_xesc(aba[:31])}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr("xl/styles.xml", estilos)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def pdf_tabela(titulo, subtitulo, cabecalhos, linhas, larguras, rodape=""):
    """PDF A4 paisagem com uma tabela (fpdf2, fonte padrão: texto convertido para latin-1)."""
    from fpdf import FPDF

    def t(x):
        return str(x if x is not None else "").encode("latin-1", "replace").decode("latin-1")
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 8, t(titulo), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(90, 100, 110)
    pdf.cell(0, 5.5, t(subtitulo), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)
    total = sum(larguras)
    esc = 277.0 / total if total else 1
    w = [x * esc for x in larguras]

    def cab():
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_fill_color(231, 238, 245)
        for h, wi in zip(cabecalhos, w):
            pdf.cell(wi, 6.5, t(h), border=0, fill=True)
        pdf.ln(6.5)
        pdf.set_font("Helvetica", "", 8.5)
    cab()
    for lin in linhas:
        if pdf.get_y() > 190:
            pdf.add_page()
            cab()
        for v, wi in zip(lin, w):
            txt = t(v)
            while pdf.get_string_width(txt) > wi - 1.5 and len(txt) > 1:
                txt = txt[:-1]
            pdf.cell(wi, 5.6, txt, border="B")
        pdf.ln(5.6)
    if rodape:
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 9.5)
        pdf.cell(0, 6, t(rodape), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def _baixar(nome, conteudo, tipo):
    return Response(conteudo, mimetype=tipo, headers={"Content-Disposition": f'attachment; filename="{nome}"', **SEM_CACHE})


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ------------------------------------------------------------------ rotas: quem sou
@bp_empresas.route("/empresas/api/eu", methods=["GET"])
def api_eu():
    cli, erro = _entrada()
    if erro:
        return erro
    emps, nome = _empresas(cli)
    if not emps:
        return jsonify({"erro": "Este CPF não tem acesso ao Portal Empresas. Peça ao posto para liberar o seu acesso "
                                "(colaborador do financeiro da empresa)."}), 403
    return jsonify({"ok": True, "usuario": nome or cli.get("nome") or "", "empresas": emps}), 200, SEM_CACHE


# ------------------------------------------------------------------ placas
def _placas_da_empresa(emp):
    veic = cp._sget(f"oct_frota_veiculos?pessoa_id=eq.{emp['id']}&select=*&order=placa&limit=1000")
    bloq = []
    try:
        bloq = cp._sget(f"oct_placas_bloqueadas?empresa_id=eq.{emp['posto_id']}&ativo=eq.true&select=placa,motivo,origem,pessoa_id,criado_em&limit=2000")
    except Exception:
        try:
            bloq = cp._sget(f"oct_placas_bloqueadas?empresa_id=eq.{emp['posto_id']}&ativo=eq.true&select=placa,motivo,criado_em&limit=2000")
        except Exception:
            bloq = []
    bq = {}
    for b in bloq or []:
        pn_b = _norm_placa(b.get("placa"))
        atual_b = bq.get(pn_b)
        # o bloqueio do POSTO manda: se existir um, é ele que a tela mostra (a empresa não o libera)
        if atual_b is None or (b.get("origem") in (None, "", "posto") and atual_b.get("origem") not in (None, "", "posto")):
            bq[pn_b] = b
    mots = {}
    ids_m = sorted({v["motorista_pessoa_id"] for v in veic or [] if v.get("motorista_pessoa_id")})
    for lote in _chunks(ids_m, 80):
        for p in cp._sget(f"oct_pessoas?id=in.({','.join(lote)})&select=id,nome&limit=200") or []:
            mots[p["id"]] = p.get("nome") or ""
    out = []
    for v in veic or []:
        pn = _norm_placa(v.get("placa"))
        b = bq.get(pn)
        do_posto = bool(b) and (b.get("origem") in (None, "", "posto"))
        if do_posto:
            sit = "bloqueada_posto"
        elif v.get("ativo") is False:
            sit = "desativada"
        else:
            sit = "ativa"
        out.append({"id": v["id"], "placa": pn, "placa_fmt": _fmt_placa(pn), "veiculo": v.get("veiculo") or "",
                    "motorista_id": v.get("motorista_pessoa_id"), "motorista": mots.get(v.get("motorista_pessoa_id"), ""),
                    "cadastrada_em": str(v.get("criado_em") or "")[:10], "situacao": sit,
                    "motivo": (b or {}).get("motivo") if do_posto else "",
                    "desativada_em": str(v.get("desativado_em") or "")[:10], "desativada_por": v.get("desativado_por") or ""})
    return out


@bp_empresas.route("/empresas/api/placas", methods=["GET"])
def api_placas():
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    placas = _placas_da_empresa(emp)
    hoje = _hoje_br()
    try:
        ini = hoje.replace(day=1).isoformat()
        cons = _resumo_placas(_consumo_linhas(emp, ini, hoje.isoformat()))
        mapa = {c["placa"]: c for c in cons}
        for p in placas:
            p["litros_mes"] = mapa.get(p["placa"], {}).get("litros", 0)
    except Exception:
        for p in placas:
            p["litros_mes"] = None
    return jsonify({"ok": True, "placas": placas}), 200, SEM_CACHE


@bp_empresas.route("/empresas/api/colaboradores", methods=["GET"])
def api_colaboradores():
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    rows = cp._sget(f"oct_pessoas?frota_empresa_id=eq.{emp['id']}&select=id,nome&order=nome&limit=300")
    return jsonify({"ok": True, "colaboradores": [{"id": r["id"], "nome": r.get("nome") or ""} for r in rows or []]}), 200, SEM_CACHE


def _bloquear_empresa(emp, placa, autor):
    cp._spost("oct_placas_bloqueadas", {"empresa_id": emp["posto_id"], "placa": placa, "motivo": "Desativada pela empresa no Portal Empresas",
                                        "autor": autor, "ativo": True, "origem": "empresa", "pessoa_id": emp["id"]}, prefer="return=minimal")


def _bloqueada_pelo_posto(emp, placa):
    rows = cp._sget(f"oct_placas_bloqueadas?empresa_id=eq.{emp['posto_id']}&ativo=eq.true&select=placa,origem&limit=2000")
    return any(_norm_placa(r.get("placa")) == placa and r.get("origem") in (None, "", "posto") for r in rows or [])


def _liberar_empresa(emp, placa):
    """Tira só o bloqueio que a EMPRESA criou. Devolve False se o posto também bloqueou (a empresa não libera)."""
    rows = cp._sget(f"oct_placas_bloqueadas?empresa_id=eq.{emp['posto_id']}&ativo=eq.true&select=id,placa,origem,pessoa_id&limit=2000")
    mesmos = [r for r in rows or [] if _norm_placa(r.get("placa")) == placa]
    if any(r.get("origem") in (None, "", "posto") for r in mesmos):
        return False
    agora = dt.datetime.now(dt.timezone.utc).isoformat()
    for r in mesmos:
        if r.get("origem") == "empresa" and r.get("pessoa_id") == emp["id"]:
            cp._spatch(f"oct_placas_bloqueadas?id=eq.{r['id']}", {"ativo": False, "liberado_em": agora})
    return True


def _cadastrar_placa(emp, cli, placa, veiculo, motorista_id):
    """-> ('nova' | 'reativada' | 'existente' | 'bloqueada_posto', registro)"""
    pn = _norm_placa(placa)
    ja = cp._sget(f"oct_frota_veiculos?pessoa_id=eq.{emp['id']}&select=*&limit=1000")
    atual = next((v for v in ja or [] if _norm_placa(v.get("placa")) == pn), None)
    autor = "portal " + cp._cpf_mascara(cli["cpf"])
    if atual:
        corpo = {}
        if veiculo and veiculo != (atual.get("veiculo") or ""):
            corpo["veiculo"] = veiculo
        if motorista_id and motorista_id != atual.get("motorista_pessoa_id"):
            corpo["motorista_pessoa_id"] = motorista_id
        if atual.get("ativo") is False:
            if not _liberar_empresa(emp, pn):
                return "bloqueada_posto", atual
            corpo.update({"ativo": True, "desativado_em": None, "desativado_por": None})
            cp._spatch(f"oct_frota_veiculos?id=eq.{atual['id']}", corpo)
            return "reativada", atual
        if corpo:
            cp._spatch(f"oct_frota_veiculos?id=eq.{atual['id']}", corpo)
        return "existente", atual
    novo = {"empresa_id": emp["posto_id"], "pessoa_id": emp["id"], "placa": pn, "veiculo": veiculo or None,
            "motorista_pessoa_id": motorista_id or None, "ativo": True, "criado_por": autor}
    try:
        r = cp._spost("oct_frota_veiculos", novo)
    except Exception:
        novo.pop("criado_por", None)                      # coluna ainda não criada
        r = cp._spost("oct_frota_veiculos", novo)
    return "nova", (r or [novo])[0]


def _motorista_valido(emp, mid):
    mid = cp._uuid_ok(mid)
    if not mid:
        return None
    r = cp._sget(f"oct_pessoas?id=eq.{mid}&frota_empresa_id=eq.{emp['id']}&select=id&limit=1")
    return mid if r else None


@bp_empresas.route("/empresas/api/placas", methods=["POST"])
def api_placa_nova():
    cli, erro = _entrada()
    if erro:
        return erro
    d = request.get_json(silent=True) or {}
    emp, erro = _acesso(cli, d.get("empresa"))
    if erro:
        return erro
    if not _placa_ok(d.get("placa")):
        return jsonify({"erro": "Placa inválida. Use AAA0A00 (Mercosul) ou AAA-0000."}), 400
    veiculo = str(d.get("veiculo") or "").strip()[:60]
    res, reg = _cadastrar_placa(emp, cli, d.get("placa"), veiculo, _motorista_valido(emp, d.get("motorista_id")))
    if res == "bloqueada_posto":
        return jsonify({"erro": "Esta placa está bloqueada pelo posto. Fale com o posto para liberar."}), 409
    _log(cli, emp["id"], "placa_nova", {"placa": _norm_placa(d.get("placa")), "resultado": res})
    return jsonify({"ok": True, "resultado": res}), 200, SEM_CACHE


def _veiculo_da_empresa(emp, vid):
    vid = cp._uuid_ok(vid)
    r = cp._sget(f"oct_frota_veiculos?id=eq.{vid}&pessoa_id=eq.{emp['id']}&select=*&limit=1") if vid else []
    return r[0] if r else None


@bp_empresas.route("/empresas/api/placas/<vid>", methods=["POST"])
def api_placa_editar(vid):
    cli, erro = _entrada()
    if erro:
        return erro
    d = request.get_json(silent=True) or {}
    emp, erro = _acesso(cli, d.get("empresa"))
    if erro:
        return erro
    v = _veiculo_da_empresa(emp, vid)
    if not v:
        return jsonify({"erro": "Placa não encontrada."}), 404
    corpo = {}
    if "veiculo" in d:
        corpo["veiculo"] = str(d.get("veiculo") or "").strip()[:60] or None
    if "motorista_id" in d:
        corpo["motorista_pessoa_id"] = _motorista_valido(emp, d.get("motorista_id"))
    if corpo:
        cp._spatch(f"oct_frota_veiculos?id=eq.{v['id']}", corpo)
    _log(cli, emp["id"], "placa_editar", {"placa": _norm_placa(v.get("placa"))})
    return jsonify({"ok": True}), 200, SEM_CACHE


@bp_empresas.route("/empresas/api/placas/<vid>/ativo", methods=["POST"])
def api_placa_ativo(vid):
    cli, erro = _entrada()
    if erro:
        return erro
    d = request.get_json(silent=True) or {}
    emp, erro = _acesso(cli, d.get("empresa"))
    if erro:
        return erro
    v = _veiculo_da_empresa(emp, vid)
    if not v:
        return jsonify({"erro": "Placa não encontrada."}), 404
    pn = _norm_placa(v.get("placa"))
    quer = d.get("ativo") is True
    autor = "portal " + cp._cpf_mascara(cli["cpf"])
    agora = dt.datetime.now(dt.timezone.utc).isoformat()
    if _bloqueada_pelo_posto(emp, pn):
        return jsonify({"erro": "Esta placa está bloqueada pelo posto. Fale com o posto para liberar."}), 409
    if quer:
        if not _liberar_empresa(emp, pn):
            return jsonify({"erro": "Esta placa está bloqueada pelo posto. Fale com o posto para liberar."}), 409
        try:
            cp._spatch(f"oct_frota_veiculos?id=eq.{v['id']}", {"ativo": True, "desativado_em": None, "desativado_por": None})
        except Exception:
            cp._spatch(f"oct_frota_veiculos?id=eq.{v['id']}", {"ativo": True})
        _log(cli, emp["id"], "placa_ativa", {"placa": pn})
    else:
        try:
            cp._spatch(f"oct_frota_veiculos?id=eq.{v['id']}", {"ativo": False, "desativado_em": agora, "desativado_por": autor})
        except Exception:
            cp._spatch(f"oct_frota_veiculos?id=eq.{v['id']}", {"ativo": False})
        try:
            _bloquear_empresa(emp, pn, autor)
        except Exception as e:                            # sem o bloqueio o caixa ainda aceitaria a placa: não finge que desativou
            cp._spatch(f"oct_frota_veiculos?id=eq.{v['id']}", {"ativo": True})
            return jsonify({"erro": "Não consegui desativar a placa no caixa agora. Tente de novo."}), 503
        _log(cli, emp["id"], "placa_desativa", {"placa": pn})
    return jsonify({"ok": True, "ativo": quer}), 200, SEM_CACHE


def _interpretar_lista(texto):
    out, vistos = [], set()
    for bruto in str(texto or "").replace("\r", "\n").split("\n"):
        lin = bruto.strip()
        if not lin:
            continue
        partes = [p.strip() for p in re.split(r"[;\t]", lin)]
        placa = partes[0]
        veic = partes[1] if len(partes) > 1 else ""
        mot = partes[2] if len(partes) > 2 else ""
        pn = _norm_placa(placa)
        if not _placa_ok(pn):
            out.append({"linha": lin, "placa": placa, "ok": False, "erro": "Formato inválido. Use AAA0A00 ou AAA-0000."})
        elif pn in vistos:
            out.append({"linha": lin, "placa": pn, "ok": False, "erro": "Repetida na lista."})
        else:
            vistos.add(pn)
            out.append({"linha": lin, "placa": pn, "ok": True, "veiculo": veic[:60], "motorista": mot[:60]})
    return out


@bp_empresas.route("/empresas/api/placas/importar", methods=["POST"])
def api_placas_importar():
    cli, erro = _entrada()
    if erro:
        return erro
    d = request.get_json(silent=True) or {}
    emp, erro = _acesso(cli, d.get("empresa"))
    if erro:
        return erro
    itens = _interpretar_lista(d.get("texto"))
    if len(itens) > 300:
        return jsonify({"erro": "Máximo de 300 placas por importação."}), 400
    existentes = {_norm_placa(v.get("placa")): v for v in cp._sget(f"oct_frota_veiculos?pessoa_id=eq.{emp['id']}&select=placa,ativo&limit=2000") or []}
    for it in itens:
        if it["ok"]:
            atual = existentes.get(it["placa"])
            it["situacao"] = "nova" if not atual else ("reativar" if atual.get("ativo") is False else "existente")
    if d.get("aplicar") is True:
        colab = {}
        for p in cp._sget(f"oct_pessoas?frota_empresa_id=eq.{emp['id']}&select=id,nome&limit=500") or []:
            colab[re.sub(r"\s+", " ", str(p.get("nome") or "").strip().lower())] = p["id"]
        for it in itens:
            if not it["ok"]:
                continue
            mid = colab.get(re.sub(r"\s+", " ", it.get("motorista", "").strip().lower())) if it.get("motorista") else None
            try:
                res, _ = _cadastrar_placa(emp, cli, it["placa"], it.get("veiculo", ""), mid)
                it["resultado"] = res
            except Exception as e:
                it["ok"] = False
                it["erro"] = "Não consegui gravar esta placa."
        _log(cli, emp["id"], "placas_importar", {"total": len(itens), "gravadas": sum(1 for i in itens if i.get("resultado"))})
    return jsonify({"ok": True, "itens": itens,
                    "resumo": {"validas": sum(1 for i in itens if i["ok"]), "erros": sum(1 for i in itens if not i["ok"])}}), 200, SEM_CACHE


# ------------------------------------------------------------------ consumo
@bp_empresas.route("/empresas/api/consumo", methods=["GET"])
def api_consumo():
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    de, ate = _periodo(request.args)
    todas = _consumo_linhas(emp, de, ate)
    linhas = _filtrar(todas, request.args.get("placa"), request.args.get("produto"), request.args.get("fatura"))
    tam = max(10, min(200, int(_num(request.args.get("tam"), 50))))
    pagina = max(1, int(_num(request.args.get("pagina"), 1)))
    fatia = linhas[(pagina - 1) * tam: pagina * tam]
    return jsonify({
        "ok": True, "de": de, "ate": ate, "totais": _totais(linhas), "resumo_placas": _resumo_placas(linhas),
        "pagina": pagina, "paginas": max(1, -(-len(linhas) // tam)), "total_linhas": len(linhas),
        "linhas": [{k: l[k] for k in ("nota_id", "cupom", "quando", "placa", "veiculo", "produto", "combustivel", "litros", "preco",
                                      "valor", "km", "motorista", "fatura_id", "fatura", "tem_foto")} for l in fatia],
        "placas_disponiveis": sorted({l["placa"] for l in todas if l["placa"]}),
        "faturas_disponiveis": sorted({str(l["fatura"]) for l in todas if l["fatura"]}, reverse=True),
    }), 200, SEM_CACHE


_COLS_CONSUMO = ["Data e hora", "Placa", "Veículo", "Produto", "Litros", "R$/L", "Valor", "Km", "Motorista", "Cupom", "Fatura"]


def _linhas_planilha(linhas):
    return [[_hora_br(l["quando"]), _fmt_placa(l["placa"]), l["veiculo"], l["produto"], l["litros"], l["preco"], l["valor"],
             l["km"], l["motorista"], l["cupom"], (str(l["fatura"]) if l["fatura"] else "A faturar")] for l in linhas]


@bp_empresas.route("/empresas/api/consumo/export", methods=["GET"])
def api_consumo_export():
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    de, ate = _periodo(request.args)
    linhas = _filtrar(_consumo_linhas(emp, de, ate), request.args.get("placa"), request.args.get("produto"), request.args.get("fatura"))
    tot = _totais(linhas)
    fmt = (request.args.get("formato") or "xlsx").lower()
    nome = f"consumo-{de}-a-{ate}"
    _log(cli, emp["id"], "exporta", {"o_que": "consumo", "formato": fmt, "linhas": len(linhas)})
    dados = _linhas_planilha(linhas)
    if fmt == "pdf":
        sub = f"{emp['nome']} · {_data_br(de)} a {_data_br(ate)} · {tot['abastecimentos']} abastecimentos · {tot['litros']:.1f} L · {_brl(tot['valor'])}"
        corpo = [[l[0], l[1], l[2], l[3], ("" if l[4] is None else f"{l[4]:.1f}"), ("" if l[5] is None else f"{l[5]:.2f}"),
                  f"{l[6]:.2f}", ("" if l[7] is None else str(l[7])), l[8], l[9], l[10]] for l in dados]
        return _baixar(nome + ".pdf", pdf_tabela("Consumo da frota", sub, _COLS_CONSUMO, corpo, [26, 20, 34, 30, 14, 12, 18, 16, 32, 14, 16],
                                                  f"Total: {_brl(tot['valor'])}"), "application/pdf")
    return _baixar(nome + ".xlsx", xlsx_bytes("Consumo", _COLS_CONSUMO, dados, [18, 12, 26, 22, 10, 9, 12, 11, 22, 9, 11]), XLSX)


@bp_empresas.route("/empresas/api/cupom/<nota_id>", methods=["GET"])
def api_cupom(nota_id):
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    nid = cp._uuid_ok(nota_id)
    rows = cp._sget(f"oct_pdv_notas_prazo?id=eq.{nid}&cliente_id=eq.{emp['id']}&select=*&limit=1") if nid else []
    n = rows[0] if rows else None
    if not n:
        return jsonify({"erro": "Cupom não encontrado."}), 404
    url = cf._assinar(cf.BUCKET_NOTAS, n.get("foto_path")) if n.get("foto_path") else None
    linhas = [l for l in _consumo_linhas(emp, str(n.get("registrado_em"))[:10], str(n.get("registrado_em"))[:10])
              if l["nota_id"] == n["id"]]
    itens = [{"produto": l["produto"], "litros": l["litros"], "preco": l["preco"], "valor": l["valor"]} for l in linhas]
    cab = linhas[0] if linhas else {}
    _log(cli, emp["id"], "cupom", {"cupom": n.get("numero_nfe")})
    return jsonify({
        "ok": True, "cupom": n.get("numero_nfe"), "quando": cab.get("quando") or n.get("registrado_em"),
        "placa": cab.get("placa") or "", "veiculo": cab.get("veiculo") or "", "motorista": cab.get("motorista") or "",
        "km": cab.get("km"), "valor": round(_num(n.get("valor")), 2), "itens": itens, "fatura": cab.get("fatura"),
        "assinado_por": n.get("assinado_por"), "assinado_em": n.get("assinado_em"), "codigo": n.get("auth_codigo"),
        "foto_url": url, "validade_seg": cf.LINK_SEG,
    }), 200, SEM_CACHE


# ------------------------------------------------------------------ faturas
_CAMPOS_FAT = ("id,numero,emissao,vencimento,valor,valor_liquido,acrescimo,desconto,status,liquidado_em,forma_liquidacao,"
               "parcela_de,parcela_num,parcela_total,cliente_nome,empresa_id")


def _valor_fat(f):
    v = f.get("valor_liquido")
    return round(_num(v if v is not None else f.get("valor")), 2)


def _faturas_da_empresa(emp):
    return cp._sget(f"oct_faturas?empresa_id=eq.{emp['posto_id']}&cliente_id=eq.{emp['id']}&status=in.(aberta,liquidada)"
                    f"&select={_CAMPOS_FAT}&order=emissao.desc&limit=400") or []


def _estado_boleto(b, hoje):
    if not b:
        return {"estado": "nao_gerado"}
    st = b.get("status")
    base = {"vencimento": str(b.get("vencimento") or "")[:10], "valor": _num(b.get("valor")), "nosso_numero": b.get("nosso_numero"),
            "linha_digitavel": b.get("linha_digitavel")}
    if st == "pendente":
        return dict(base, estado="gerando")
    if st == "liquidado":
        return dict(base, estado="pago")
    if st == "registrado":
        venc = base["vencimento"]
        return dict(base, estado="vencido" if venc and venc < hoje.isoformat() else "disponivel")
    if st == "erro":
        return {"estado": "erro"}
    return {"estado": "nao_gerado"}                       # cancelado / simulado


def _montar_faturas(emp, fats, com_detalhes=True):
    hoje = _hoje_br()
    multa_pct, juros_pct = _params_cobranca(emp["posto_id"])
    ids = [f["id"] for f in fats]
    recebido, boletos, qtd = {}, {}, {}
    for lote in _chunks(ids, 60):
        lista = ",".join(lote)
        for r in cp._sget(f"oct_recebimentos_titulo?fatura_id=in.({lista})&select=fatura_id,valor&limit=3000") or []:
            recebido[r["fatura_id"]] = recebido.get(r["fatura_id"], 0.0) + _num(r.get("valor"))
        for b in cp._sget(f"oct_boletos?fatura_id=in.({lista})&select=id,fatura_id,status,vencimento,valor,nosso_numero,linha_digitavel"
                          f"&order=id.desc&limit=1500") or []:
            boletos.setdefault(b["fatura_id"], b)         # o mais novo (id desc)
        if com_detalhes:
            for n in cp._sget(f"oct_pdv_notas_prazo?fatura_id=in.({lista})&select=fatura_id&limit=5000") or []:
                qtd[n["fatura_id"]] = qtd.get(n["fatura_id"], 0) + 1
    pix_ligado = cf._pagamento_app_ligado(emp["posto_id"])
    out = []
    for f in fats:
        liq = _valor_fat(f)
        paga = f.get("status") == "liquidada"
        saldo = 0.0 if paga else max(0.0, round(liq - recebido.get(f["id"], 0.0), 2))
        venc = str(f.get("vencimento") or "")[:10]
        enc = encargos(saldo, venc, hoje, multa_pct, juros_pct) if not paga else {"dias": 0, "multa": 0.0, "juros": 0.0, "total": 0.0}
        vencida = (not paga) and bool(venc) and venc < hoje.isoformat()
        try:
            dias_ate = (dt.date.fromisoformat(venc) - hoje).days if venc else None
        except Exception:
            dias_ate = None
        out.append({
            "id": f["id"], "numero": f.get("numero"), "emissao": str(f.get("emissao") or "")[:10], "vencimento": venc,
            "valor": liq, "saldo": saldo, "status": "paga" if paga else "aberta", "paga": paga, "vencida": vencida,
            "dias_atraso": enc["dias"] if vencida else 0, "dias_para_vencer": dias_ate if (not paga and not vencida) else None,
            "multa": enc["multa"], "juros": enc["juros"], "valor_atualizado": round(saldo + enc["total"], 2),
            "paga_em": str(f.get("liquidado_em") or "")[:10] if paga else "", "forma": f.get("forma_liquidacao") or "",
            "parcela": (f"{f.get('parcela_num')}/{f.get('parcela_total')}" if f.get("parcela_num") else None),
            "qtd": qtd.get(f.get("parcela_de") or f["id"], qtd.get(f["id"], 0)),
            "boleto": _estado_boleto(boletos.get(f["id"]), hoje), "pix_ligado": bool(pix_ligado),
        })
    return out, {"multa_pct": multa_pct, "juros_mes_pct": juros_pct}


@bp_empresas.route("/empresas/api/faturas", methods=["GET"])
def api_faturas():
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    itens, taxas = _montar_faturas(emp, _faturas_da_empresa(emp))
    itens.sort(key=lambda x: (x["paga"], x["vencimento"] if not x["paga"] else ""))
    pagas = [i for i in itens if i["paga"]]
    pagas.sort(key=lambda x: x["paga_em"], reverse=True)
    abertas = [i for i in itens if not i["paga"]]
    return jsonify({"ok": True, "abertas": abertas, "pagas": pagas, "taxas": taxas}), 200, SEM_CACHE


def _fatura_da_empresa(emp, fid):
    fid = cp._uuid_ok(fid)
    r = cp._sget(f"oct_faturas?id=eq.{fid}&cliente_id=eq.{emp['id']}&empresa_id=eq.{emp['posto_id']}&select=*&limit=1") if fid else []
    return r[0] if r else None


def _notas_da_fatura(f):
    origem = f.get("parcela_de") or f["id"]
    return cp._sget(f"oct_pdv_notas_prazo?fatura_id=eq.{origem}&select=id,numero_nfe,valor,registrado_em,foto_path,fatura_id,status,"
                    f"assinado_por,assinado_em&order=registrado_em&limit=3000") or []


@bp_empresas.route("/empresas/api/faturas/<fid>", methods=["GET"])
def api_fatura_detalhe(fid):
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    f = _fatura_da_empresa(emp, fid)
    if not f:
        return jsonify({"erro": "Fatura não encontrada."}), 404
    item, taxas = _montar_faturas(emp, [f])
    linhas = _linhas_de_notas(emp, _notas_da_fatura(f))
    resumo = _resumo_placas(linhas)
    return jsonify({"ok": True, "fatura": item[0], "taxas": taxas, "por_placa": resumo, "totais": _totais(linhas),
                    "acrescimo": round(_num(f.get("acrescimo")), 2), "desconto": round(_num(f.get("desconto")), 2),
                    "valor_bruto": round(_num(f.get("valor")), 2)}), 200, SEM_CACHE


@bp_empresas.route("/empresas/api/faturas/<fid>/export", methods=["GET"])
def api_fatura_export(fid):
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    f = _fatura_da_empresa(emp, fid)
    if not f:
        return jsonify({"erro": "Fatura não encontrada."}), 404
    linhas = _linhas_de_notas(emp, _notas_da_fatura(f))
    _log(cli, emp["id"], "exporta", {"o_que": "fatura", "numero": f.get("numero"), "linhas": len(linhas)})
    return _baixar(f"fatura-{f.get('numero')}-compras.xlsx",
                   xlsx_bytes(f"Fatura {f.get('numero')}", _COLS_CONSUMO, _linhas_planilha(linhas), [18, 12, 26, 22, 10, 9, 12, 11, 22, 9, 11]), XLSX)


@bp_empresas.route("/empresas/api/faturas/export", methods=["GET"])
def api_faturas_export():
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    itens, _t = _montar_faturas(emp, _faturas_da_empresa(emp), com_detalhes=True)
    aba = (request.args.get("aba") or "todas").lower()
    if aba == "abertas":
        itens = [i for i in itens if not i["paga"]]
    elif aba == "pagas":
        itens = [i for i in itens if i["paga"]]
    cab = ["Fatura", "Emissão", "Vencimento", "Valor", "Saldo", "Situação", "Pago em", "Forma", "Abastecimentos"]

    def sit(i):
        if i["paga"]:
            return "Paga"
        return f"Vencida há {i['dias_atraso']} dias" if i["vencida"] else "Em aberto"
    dados = [[i["numero"], _data_br(i["emissao"]), _data_br(i["vencimento"]), i["valor"], i["saldo"], sit(i),
              _data_br(i["paga_em"]), i["forma"], i["qtd"]] for i in itens]
    fmt = (request.args.get("formato") or "xlsx").lower()
    _log(cli, emp["id"], "exporta", {"o_que": "faturas", "formato": fmt, "linhas": len(dados)})
    if fmt == "pdf":
        corpo = [[d[0], d[1], d[2], f"{d[3]:.2f}", f"{d[4]:.2f}", d[5], d[6], d[7], d[8]] for d in dados]
        return _baixar("faturas.pdf", pdf_tabela("Faturas", f"{emp['nome']} · {len(dados)} faturas", cab, corpo, [16, 22, 24, 24, 24, 40, 24, 22, 22],
                                                  f"Em aberto: {_brl(sum(i['saldo'] for i in itens))}"), "application/pdf")
    return _baixar("faturas.xlsx", xlsx_bytes("Faturas", cab, dados, [10, 12, 12, 14, 14, 24, 12, 14, 14]), XLSX)


@bp_empresas.route("/empresas/api/faturas/<fid>/arquivo", methods=["GET"])
def api_fatura_arquivo(fid):
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    f = _fatura_da_empresa(emp, fid)
    if not f:
        return jsonify({"erro": "Fatura não encontrada."}), 404
    tipo = (request.args.get("tipo") or "fatura").strip().lower()
    campo = {"fatura": "fatura_pdf_path", "boleto": "boleto_pdf_path"}.get(tipo)
    if not campo:
        return jsonify({"erro": "Tipo de documento inválido."}), 400
    caminho = f.get(campo)
    if not caminho:
        if tipo == "fatura":                              # o gateway gera sob pedido (mesmo campo da retaguarda)
            try:
                if not f.get("fatura_pdf_pedido_em"):
                    cp._spatch(f"oct_faturas?id=eq.{f['id']}&fatura_pdf_path=is.null",
                               {"fatura_pdf_pedido_em": dt.datetime.now(dt.timezone.utc).isoformat()})
            except Exception:
                pass
            return jsonify({"ok": True, "gerando": True}), 200, SEM_CACHE
        return jsonify({"erro": "O boleto em PDF ainda não está disponível. Tente em instantes."}), 404
    url = cf._assinar(cf.BUCKET_DOCS, caminho)
    if not url:
        return jsonify({"erro": "O arquivo não está mais disponível."}), 404
    _log(cli, emp["id"], "exporta", {"o_que": "pdf_" + tipo, "numero": f.get("numero")})
    return jsonify({"ok": True, "url": url, "validade_seg": cf.LINK_SEG}), 200, SEM_CACHE


# ------------------------------------------------------------------ Pix da fatura
def _alvo_pagavel(emp, fid):
    """(fatura, saldo, encargos, valor_a_pagar) ou (None, ...) quando não está em aberto."""
    f = _fatura_da_empresa(emp, fid)
    if not f or f.get("status") != "aberta":
        return None, 0.0, None, 0.0
    rec = cp._sget(f"oct_recebimentos_titulo?fatura_id=eq.{f['id']}&select=valor&limit=2000") or []
    saldo = max(0.0, round(_valor_fat(f) - sum(_num(r.get("valor")) for r in rec), 2))
    multa_pct, juros_pct = _params_cobranca(emp["posto_id"])
    enc = encargos(saldo, f.get("vencimento"), _hoje_br(), multa_pct, juros_pct)
    return f, saldo, enc, round(saldo + enc["total"], 2)


@bp_empresas.route("/empresas/api/faturas/<fid>/pix", methods=["POST"])
def api_fatura_pix(fid):
    cli, erro = _entrada()
    if erro:
        return erro
    d = request.get_json(silent=True) or {}
    emp, erro = _acesso(cli, d.get("empresa") or request.args.get("empresa"))
    if erro:
        return erro
    f, saldo, enc, valor = _alvo_pagavel(emp, fid)
    if not f:
        return jsonify({"erro": "Esta fatura não está em aberto."}), 409
    if valor <= 0:
        return jsonify({"erro": "Não há valor a pagar nesta fatura."}), 409
    if not cf._pagamento_app_ligado(emp["posto_id"]):
        return jsonify({"erro": "O pagamento por Pix ainda não está habilitado neste posto. Use o boleto ou pague no posto."}), 503
    if not cp._limite_ok("pixemp:" + cli["cpf"], 30, 3600):
        return cp._muitas("Muitos pedidos de Pix seguidos. Espere um pouco.")
    ab = cp._sget(f"oct_pix_cobrancas?fatura_id=eq.{f['id']}&status=in.(pedido,criando,ativa)&order=id.desc&limit=1&select=id,status,expira_em,valor")
    if ab:
        a = ab[0]
        vivo = a.get("status") in ("pedido", "criando") or (a.get("expira_em") and str(a["expira_em"]) > dt.datetime.now(dt.timezone.utc).isoformat())
        if vivo:
            return jsonify({"ok": True, "id": a["id"], "status": a["status"], "valor": _num(a.get("valor"), valor)}), 200, SEM_CACHE
    nova = cp._spost("oct_pix_cobrancas", {"empresa_id": emp["posto_id"], "cliente_id": emp["id"], "fatura_id": f["id"], "valor": valor,
                                           "status": "pedido", "origem": "portal_empresas",
                                           "criado_por": "portal " + cp._cpf_mascara(cli["cpf"])})
    row = (nova or [{}])[0]
    _log(cli, emp["id"], "pix", {"fatura": f.get("numero"), "valor": valor, "multa": enc["multa"], "juros": enc["juros"]})
    return jsonify({"ok": True, "id": row.get("id"), "status": "pedido", "valor": valor}), 200, SEM_CACHE


def _pix_da_empresa(emp, cid):
    try:
        n = int(cid)
    except (TypeError, ValueError):
        return None
    r = cp._sget(f"oct_pix_cobrancas?id=eq.{n}&cliente_id=eq.{emp['id']}&select=*&limit=1")
    return r[0] if r else None


@bp_empresas.route("/empresas/api/pix/<cid>", methods=["GET"])
def api_pix_status(cid):
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    c = _pix_da_empresa(emp, cid)
    if not c:
        return jsonify({"erro": "Pedido não encontrado."}), 404
    st = c.get("status")
    out = {"ok": True, "id": c["id"], "status": st, "valor": _num(c.get("valor"))}
    if st == "ativa":
        out.update(copia_cola=c.get("copia_cola"), expira_em=c.get("expira_em"))
    elif st == "paga":
        out.update(pago_em=c.get("pago_em"))
    elif st == "erro":
        out["erro"] = "Não consegui gerar o Pix agora. Tente de novo em instantes ou use o boleto."
    elif st == "expirada":
        out["erro"] = "Este Pix expirou. Gere um novo."
    return jsonify(out), 200, SEM_CACHE


@bp_empresas.route("/empresas/api/pix/<cid>/qr.png", methods=["GET"])
def api_pix_qr(cid):
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    c = _pix_da_empresa(emp, cid)
    if not c or c.get("status") != "ativa" or not c.get("copia_cola"):
        return jsonify({"erro": "QR não disponível."}), 404
    import qrcode
    buf = io.BytesIO()
    qrcode.make(c["copia_cola"], box_size=8, border=2).save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png", headers=SEM_CACHE)


# ------------------------------------------------------------------ boleto novo
def _boleto_do_pedido(fid):
    r = cp._sget(f"oct_boletos?fatura_id=eq.{fid}&order=id.desc&limit=1&select=id,status,erro,vencimento,valor,nosso_numero,linha_digitavel,criado_em,origem,criado_por")
    return r[0] if r else None


@bp_empresas.route("/empresas/api/faturas/<fid>/boleto", methods=["POST"])
def api_fatura_boleto_novo(fid):
    """Gera um NOVO boleto da fatura: vence HOJE, com multa e juros do atraso; o boleto antigo é cancelado no banco quando
    o novo é registrado (o gateway faz as duas coisas, nessa ordem). Sem depender do financeiro do posto."""
    cli, erro = _entrada()
    if erro:
        return erro
    d = request.get_json(silent=True) or {}
    emp, erro = _acesso(cli, d.get("empresa") or request.args.get("empresa"))
    if erro:
        return erro
    f, saldo, enc, valor = _alvo_pagavel(emp, fid)
    if not f:
        return jsonify({"erro": "Esta fatura não está em aberto."}), 409
    if valor <= 0:
        return jsonify({"erro": "Não há valor a pagar nesta fatura."}), 409
    conta = (cp._sget(f"oct_sicoob_contas?empresa_id=eq.{emp['posto_id']}&ativo=eq.true&select=cobranca_ativa,numero_cliente&limit=1") or [{}])[0]
    if not conta.get("cobranca_ativa") or not conta.get("numero_cliente"):
        return jsonify({"erro": "A emissão de boleto não está habilitada neste posto. Use o Pix ou fale com o posto."}), 503
    if not cp._limite_ok("bolemp:" + cli["cpf"], 30, 3600):
        return cp._muitas("Muitos pedidos de boleto seguidos. Espere um pouco.")
    ult = _boleto_do_pedido(f["id"])
    agora = dt.datetime.now(dt.timezone.utc)
    if ult and ult.get("status") == "pendente":                       # já tem um pedido na fila: devolve o mesmo
        return jsonify({"ok": True, "id": ult["id"], "estado": "gerando"}), 200, SEM_CACHE
    hoje = _hoje_br()
    atual = cp._sget(f"oct_boletos?fatura_id=eq.{f['id']}&status=eq.registrado&order=id.desc&limit=1&select=id,nosso_numero,vencimento,valor")
    # já existe boleto registrado que vence HOJE ou depois e vale o mesmo valor: não gera outro à toa
    if atual and str(atual[0].get("vencimento") or "")[:10] >= hoje.isoformat() and abs(_num(atual[0].get("valor")) - valor) < 0.005:
        return jsonify({"ok": True, "id": atual[0]["id"], "estado": "disponivel", "ja_existia": True}), 200, SEM_CACHE
    multa_pct, juros_pct = _params_cobranca(emp["posto_id"])
    pedido = {"empresa_id": emp["posto_id"], "fatura_id": f["id"], "cliente_id": emp["id"], "cliente_nome": f.get("cliente_nome") or emp["nome"],
              "valor": valor, "vencimento": hoje.isoformat(), "status": "pendente",
              "criado_por": "portal " + cp._cpf_mascara(cli["cpf"]), "origem": "portal_empresas",
              "valor_emitir": valor, "vencimento_emitir": hoje.isoformat(), "multa_pct": multa_pct, "juros_mes_pct": juros_pct,
              "substitui_nosso_numero": (atual[0].get("nosso_numero") if atual else None)}
    try:
        nova = cp._spost("oct_boletos", pedido)
    except Exception as e:
        return jsonify({"erro": "Não consegui pedir o boleto agora. Tente de novo em instantes."}), 502
    row = (nova or [{}])[0]
    _log(cli, emp["id"], "boleto", {"fatura": f.get("numero"), "valor": valor, "multa": enc["multa"], "juros": enc["juros"],
                                    "substitui": pedido["substitui_nosso_numero"]})
    return jsonify({"ok": True, "id": row.get("id"), "estado": "gerando", "valor": valor, "vencimento": hoje.isoformat()}), 200, SEM_CACHE


@bp_empresas.route("/empresas/api/faturas/<fid>/boleto", methods=["GET"])
def api_fatura_boleto_estado(fid):
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    f = _fatura_da_empresa(emp, fid)
    if not f:
        return jsonify({"erro": "Fatura não encontrada."}), 404
    b = _boleto_do_pedido(f["id"])
    est = _estado_boleto(b, _hoje_br())
    out = {"ok": True, "estado": est["estado"], "boleto": est}
    if est["estado"] == "erro":
        out["erro"] = "Não consegui gerar o boleto agora. Tente de novo em instantes ou fale com o posto."
    out["tem_pdf"] = bool(f.get("boleto_pdf_path")) and est["estado"] in ("disponivel", "vencido")
    return jsonify(out), 200, SEM_CACHE


# ------------------------------------------------------------------ visão geral
@bp_empresas.route("/empresas/api/visao", methods=["GET"])
def api_visao():
    cli, erro = _entrada()
    if erro:
        return erro
    emp, erro = _acesso(cli, request.args.get("empresa"))
    if erro:
        return erro
    hoje = _hoje_br()
    ini90 = (hoje - dt.timedelta(days=89)).isoformat()
    linhas90, fats, placas = cf._par(lambda: _consumo_linhas(emp, ini90, hoje.isoformat()),
                                     lambda: _montar_faturas(emp, _faturas_da_empresa(emp)),
                                     lambda: _placas_da_empresa(emp))
    itens, taxas = fats
    mes_ini = hoje.replace(day=1).isoformat()
    d30_ini = (hoje - dt.timedelta(days=29)).isoformat()

    def desde(ini):
        return [l for l in linhas90 if _dia_br(l["quando"]) >= ini]

    def grafico(lin):
        res = _resumo_placas(lin)
        return [{"placa": r["placa"], "placa_fmt": _fmt_placa(r["placa"]), "veiculo": r["veiculo"], "litros": r["litros"], "valor": r["valor"]}
                for r in res if r["litros"] > 0 or r["valor"] > 0][:8]
    mes, d30 = desde(mes_ini), desde(d30_ini)
    lmes = {r["placa"]: r["litros"] for r in _resumo_placas(mes)}
    for p in placas:
        p["litros_mes"] = lmes.get(p["placa"], 0)
    abertas = [i for i in itens if not i["paga"]]
    pagas = sorted([i for i in itens if i["paga"]], key=lambda x: x["paga_em"], reverse=True)[:3]
    nao_fat = round(sum(l["valor"] for l in linhas90 if not l["fatura_id"]), 2)
    abertas_valor = round(sum(i["saldo"] for i in abertas), 2)
    venc = [i for i in abertas if i["vencida"]]
    limite = emp.get("limite") or 0.0
    return jsonify({
        "ok": True, "hoje": hoje.isoformat(),
        "kpi": {
            "consumo_mes_valor": _totais(mes)["valor"], "consumo_mes_litros": _totais(mes)["litros"],
            "abertas_valor": abertas_valor, "abertas_qtd": len(abertas),
            "vencidas_qtd": len(venc), "vencidas_valor": round(sum(i["saldo"] for i in venc), 2),
            "limite": limite, "limite_usado": round(abertas_valor + nao_fat, 2),
            "nao_faturado": nao_fat,
            "placas_ativas": sum(1 for p in placas if p["situacao"] in ("ativa", "bloqueada_posto")),
            "placas_desativadas": sum(1 for p in placas if p["situacao"] == "desativada"),
            "placas_bloqueadas": sum(1 for p in placas if p["situacao"] == "bloqueada_posto"),
        },
        "grafico": {"mes": grafico(mes), "d30": grafico(d30), "d90": grafico(linhas90)},
        "totais": {"mes": _totais(mes), "d30": _totais(d30), "d90": _totais(linhas90)},
        "faturas_abertas": abertas, "faturas_pagas": pagas, "taxas": taxas,
        "placas": placas,
    }), 200, SEM_CACHE


# ------------------------------------------------------------------ a página (arquivos estáticos em empresas_www/)
@bp_empresas.route("/empresas", methods=["GET"])
def empresas_raiz():
    from flask import redirect
    return redirect("/empresas/", code=302)


@bp_empresas.route("/empresas/", methods=["GET"])
@bp_empresas.route("/empresas/<path:arq>", methods=["GET"])
def empresas_pagina(arq="index.html"):
    import os
    from flask import send_from_directory
    pasta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "empresas_www")
    resp = send_from_directory(pasta, arq)               # send_from_directory recusa ../
    resp.headers["Cache-Control"] = "no-cache"
    return resp
