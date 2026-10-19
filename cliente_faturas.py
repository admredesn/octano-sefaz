# -*- coding: utf-8 -*-
"""
cliente_faturas.py — ÁREA DO CLIENTE: faturas, extrato, boleto/PDF e fotos dos cupons assinados.
(App Postos SN e versão web /app/ — o mesmo código serve celular e PC.)

Rotas (todas exigem o login do cliente do portal — header Authorization, como o resto do app):
  GET /cashback/api/faturas/resumo                        -> {ok, ativo, postos:[...]}   (o que mostrar no Início/Perfil)
  GET /cashback/api/faturas?posto=<empresa_id>            -> faturas abertas, pagas e compras ainda não faturadas
  GET /cashback/api/faturas/<id>                          -> detalhe: extrato (cupom/produto/placa), boleto, cupons com foto
  GET /cashback/api/faturas/<id>/arquivo?tipo=fatura|boleto|nfe -> {ok, url} (link temporário do PDF)
  GET /cashback/api/faturas/nota/<nota_id>/foto           -> {ok, url} (link temporário da foto do cupom assinado)
  GET /cashback/api/faturas/<id>/pix.png                  -> QR do Pix copia e cola, se o boleto veio com Pix
  POST /cashback/api/pagar  {nota_id | fatura_id}         -> pede o Pix de UMA compra a prazo (botao "Pagar") ou da fatura;
                                                             o gateway octano-sicoob cria a cobranca e da' a baixa quando paga
  GET  /cashback/api/pagar/<id>                           -> estado do pedido (pedido/ativa/paga/expirada/erro) + copia e cola
  GET  /cashback/api/pagar/<id>/qr.png                    -> QR do Pix copia e cola

QUEM VÊ O QUÊ (decisão de 13/10/2026, pedido do Ronan):
  * a conta (CPF) vê as faturas do PRÓPRIO cadastro no posto;
  * colaborador de empresa só vê as da EMPRESA se o cadastro dele tiver `app_ve_faturas = true`
    (marcado no retaguarda, em Pessoas > empresa > colaboradores autorizados). Sem a marca: nada da empresa.
  * fatura/foto de outro cliente responde 404 — nunca confirma que o registro existe.

LIGA/DESLIGA: env AREA_CLIENTE_FATURAS = lista de empresa_id separados por vírgula, ou `all`.
Vazio = DESLIGADO (a área some do app). Começa pelo Florestal.

Links de PDF/foto: assinados pelo Supabase, validade de 5 minutos (os buckets são privados).
"""
import datetime as dt
import io
import time
from concurrent.futures import ThreadPoolExecutor

import requests as rq
from flask import Blueprint, Response, jsonify, request

import cashback_portal as cp

bp_faturas = Blueprint("cliente_faturas", __name__)

# consultas independentes ao banco rodam juntas (cada uma custa ~0,1-0,2 s de ida e volta; em fila somavam 1 s ou mais)
_POOL = ThreadPoolExecutor(max_workers=12)


def _par(*fns):
    futs = [_POOL.submit(f) for f in fns]
    return [f.result() for f in futs]

BUCKET_DOCS = "octano-documentos"     # fatura-<n>.pdf, boleto-<nosso>.pdf, nfe
BUCKET_NOTAS = "notas_prazo"          # foto do cupom assinado (PDV)
LINK_SEG = 300
JANELA_PAGAS_DIAS = 365
SEM_CACHE = {"Cache-Control": "no-store"}


# ------------------------------------------------------------------ liga/desliga
def _ligados():
    import os
    v = os.environ.get("AREA_CLIENTE_FATURAS", "").strip().lower()
    if not v:
        return set()
    if v in ("all", "todos", "1", "true"):
        return {"*"}
    return {x.strip().lower() for x in v.split(",") if x.strip()}


def _posto_ligado(empresa_id):
    lig = _ligados()
    return "*" in lig or str(empresa_id or "").lower() in lig


# ------------------------------------------------------------------ contexto do cliente
def _contas(cli):
    """{empresa_id: {cliente_id: (rotulo, nome)}} — quem esta conta pode ver, por posto ligado.
    rotulo: 'proprio' | 'empresa'."""
    cpf = cli["cpf"]
    sel = "id,nome,empresa_id,frota_empresa_id"
    try:
        rows = cp._sget(f"oct_pessoas?documento=eq.{cpf}&select={sel},app_ve_faturas&limit=50")
    except Exception:
        # coluna app_ve_faturas ainda não criada: só o próprio cadastro
        rows = cp._sget(f"oct_pessoas?documento=eq.{cpf}&select={sel}&limit=50")
    out = {}
    vinc = []
    for p in rows or []:
        emp = p.get("empresa_id")
        if not _posto_ligado(emp):
            continue
        ids = out.setdefault(emp, {})
        ids[p["id"]] = ("proprio", p.get("nome") or "")
        if p.get("frota_empresa_id") and p.get("app_ve_faturas") is True:
            vinc.append((emp, p["frota_empresa_id"]))
    if vinc:                                   # UMA consulta para todas as empresas vinculadas
        es = cp._sget(f"oct_pessoas?id={_in(sorted({v for _, v in vinc}))}&select=id,nome&limit=100")
        nomes = {e["id"]: e.get("nome") or "" for e in es or []}
        for emp, fid in vinc:
            if fid in nomes:
                out[emp][fid] = ("empresa", nomes[fid])
    return out


def _in(ids):
    return "in.(" + ",".join(ids) + ")"


def _bloqueio(ids):
    """(bloqueado, origem, motivo) do cadastro do cliente ou da empresa que ele pode ver."""
    lst = list(ids)
    if not lst:
        return False, None, None
    try:
        rows = cp._sget(f"oct_pessoas?id={_in(lst)}&select=id,credito_bloqueado,credito_bloqueio_origem,credito_bloqueio_motivo")
    except Exception:
        rows = cp._sget(f"oct_pessoas?id={_in(lst)}&select=id,credito_bloqueado")     # colunas do bloqueio ainda nao criadas
    bl = [r for r in rows or [] if r.get("credito_bloqueado") is True]
    if not bl:
        return False, None, None
    return True, bl[0].get("credito_bloqueio_origem"), bl[0].get("credito_bloqueio_motivo")


_PIX_CACHE = {}


def _conta_pix_ok(empresa_id):
    """O posto tem Pix habilitado: oct_sicoob_contas.pix_ativo e chave_pix (cache de 60 s por processo)."""
    agora = time.time()
    c = _PIX_CACHE.get(empresa_id)
    if c and agora - c[0] < 60:
        return c[1]
    try:
        rows = cp._sget(f"oct_sicoob_contas?empresa_id=eq.{empresa_id}&ativo=eq.true&select=pix_ativo,chave_pix&limit=1")
        ok = bool(rows and rows[0].get("pix_ativo") is True and str(rows[0].get("chave_pix") or "").strip())
    except Exception:
        ok = False                       # colunas ainda nao criadas
    _PIX_CACHE[empresa_id] = (agora, ok)
    return ok


def _pagamento_app_ligado(empresa_id):
    """Pagar pelo app (Pix): env PIX_APP_ATIVO = empresa_id,... ou all (vazio = desligado) E o posto com
    pix_ativo + chave_pix em oct_sicoob_contas."""
    import os
    v = os.environ.get("PIX_APP_ATIVO", "").strip().lower()
    if not v:
        return False
    if not (v in ("all", "todos", "1", "true") or str(empresa_id or "").lower() in [x.strip() for x in v.split(",")]):
        return False
    return _conta_pix_ok(empresa_id)


def _cliente_ou_401():
    cli = cp._cliente_do_token()
    if not cli:
        return None, (jsonify({"erro": "sessão expirada"}), 401)
    if not cp._limite_ok("fat:" + cli["cpf"], 150, 60):
        return None, cp._muitas("Muitas consultas seguidas. Espere um minuto.")
    return cli, None


def _fatura_do_cliente(fatura_id, cli):
    """(fatura, ids_permitidos) ou (None, None) — 404 sem confirmar existência."""
    fid = cp._uuid_ok(fatura_id)
    if not fid:
        return None, None
    rows = cp._sget(f"oct_faturas?id=eq.{fid}&select=*&limit=1")
    if not rows:
        return None, None
    f = rows[0]
    ids = _contas(cli).get(f.get("empresa_id")) or {}
    if f.get("cliente_id") not in ids or f.get("status") in ("cancelada",):
        return None, None
    return f, ids


def _valor(f):
    v = f.get("valor_liquido")
    return float(v if v is not None else (f.get("valor") or 0))


def _hoje():
    return dt.date.today().isoformat()


# ------------------------------------------------------------------ link assinado
def _assinar(bucket, caminho, seg=LINK_SEG):
    if not caminho:
        return None
    url, _ = cp._supa()
    r = rq.post(f"{url}/storage/v1/object/sign/{bucket}/{caminho}", json={"expiresIn": seg},
                headers=cp._st_headers({"Content-Type": "application/json"}), timeout=15)
    if r.status_code >= 400:
        return None
    j = r.json() or {}
    u = j.get("signedURL") or j.get("signedUrl") or ""
    return (f"{url}/storage/v1{u}" if u.startswith("/") else u) or None


def _log(cli, o):
    print(f"[faturas] cpf {cp._cpf_mascara(cli['cpf'])} {o}")


# ------------------------------------------------------------------ resumo
@bp_faturas.route("/cashback/api/faturas/resumo", methods=["GET"])
def api_resumo():
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    if not _ligados():
        return jsonify({"ok": True, "ativo": False, "postos": []}), 200, SEM_CACHE
    contas = _contas(cli)
    postos = []
    hoje = _hoje()
    for emp, ids in contas.items():
        lst, nao, nome, (bloq, origem, motivo) = _par(
            lambda: cp._sget(
                f"oct_faturas?empresa_id=eq.{emp}&cliente_id={_in(list(ids))}&status=eq.aberta"
                f"&select=id,valor,valor_liquido,vencimento&limit=500"),
            lambda: cp._sget(
                f"oct_pdv_notas_prazo?empresa_id=eq.{emp}&cliente_id={_in(list(ids))}"
                f"&status=eq.aberto&fatura_id=is.null&select=valor&limit=1000"),
            lambda: _nome_empresa(emp),
            lambda: _bloqueio(ids))
        venc = [f for f in lst if str(f.get("vencimento") or "9999")[:10] < hoje]
        prox = sorted(str(f.get("vencimento"))[:10] for f in lst if f.get("vencimento"))
        if not lst and not nao and not bloq and not _tem_alguma(emp, ids):
            continue
        postos.append({
            "empresa_id": emp, "posto": nome,
            "bloqueado": bloq, "bloqueio_origem": origem, "bloqueio_motivo": motivo,
            "pagamento_app": _pagamento_app_ligado(emp),
            "titular": sorted({v[1] for v in ids.values() if v[0] == "empresa"} or {v[1] for v in ids.values()}),
            "abertas_qtd": len(lst), "abertas_valor": round(sum(_valor(f) for f in lst), 2),
            "vencidas_qtd": len(venc), "vencidas_valor": round(sum(_valor(f) for f in venc), 2),
            "proximo_vencimento": prox[0] if prox else None,
            "nao_faturado_qtd": len(nao), "nao_faturado_valor": round(sum(float(n.get("valor") or 0) for n in nao), 2),
        })
    return jsonify({"ok": True, "ativo": bool(postos), "postos": postos}), 200, SEM_CACHE


_NOMES = {}


def _nome_empresa(emp):
    c = _NOMES.get(emp)
    if c and time.time() - c[0] < 600:
        return c[1]
    nome = ""
    try:
        nome = (cp._sget(f"oct_empresas?id=eq.{emp}&select=nome&limit=1") or [{}])[0].get("nome") or ""
    except Exception:
        pass
    if nome:
        _NOMES[emp] = (time.time(), nome)
    return nome


def _tem_alguma(emp, ids):
    r = cp._sget(f"oct_faturas?empresa_id=eq.{emp}&cliente_id={_in(list(ids))}&select=id&limit=1")
    return bool(r)


# ------------------------------------------------------------------ lista
@bp_faturas.route("/cashback/api/faturas", methods=["GET"])
def api_lista():
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    emp = cp._uuid_ok(request.args.get("posto"))
    ids = _contas(cli).get(emp) if emp else None
    if not ids:
        return jsonify({"erro": "Nenhuma fatura encontrada neste posto."}), 404
    campos = ("id,numero,emissao,vencimento,valor,valor_liquido,acrescimo,desconto,status,liquidado_em,"
              "parcela_num,parcela_total,cliente_id,cliente_nome")
    todas, nao = _par(
        lambda: cp._sget(
            f"oct_faturas?empresa_id=eq.{emp}&cliente_id={_in(list(ids))}&status=in.(aberta,liquidada)"
            f"&select={campos}&order=emissao.desc&limit=400"),
        lambda: cp._sget(
            f"oct_pdv_notas_prazo?empresa_id=eq.{emp}&cliente_id={_in(list(ids))}"
            f"&status=eq.aberto&fatura_id=is.null&select=id,numero_nfe,valor,registrado_em,foto_path"
            f"&order=registrado_em.desc&limit=200"))
    hoje = _hoje()
    corte = (dt.date.today() - dt.timedelta(days=JANELA_PAGAS_DIAS)).isoformat()

    def item(f):
        venc = str(f.get("vencimento") or "")[:10]
        return {
            "id": f["id"], "numero": f.get("numero"), "emissao": f.get("emissao"), "vencimento": venc or None,
            "valor": round(_valor(f), 2), "status": f.get("status"),
            "vencida": f.get("status") == "aberta" and bool(venc) and venc < hoje,
            "dias_atraso": max(0, (dt.date.today() - dt.date.fromisoformat(venc)).days)
            if (f.get("status") == "aberta" and venc and venc < hoje) else 0,
            "parcela": (f"{f.get('parcela_num')}/{f.get('parcela_total')}" if f.get("parcela_num") else None),
            "titular": ids.get(f.get("cliente_id"), ("", ""))[1], "da_empresa": ids.get(f.get("cliente_id"), ("",))[0] == "empresa",
            "liquidado_em": f.get("liquidado_em"),
        }
    abertas = [item(f) for f in todas if f.get("status") == "aberta"]
    abertas.sort(key=lambda x: x["vencimento"] or "9999")
    pagas = [item(f) for f in todas if f.get("status") == "liquidada"
             and str(f.get("liquidado_em") or f.get("emissao") or "")[:10] >= corte]
    return jsonify({
        "ok": True,
        "abertas": abertas, "pagas": pagas,
        "nao_faturadas": {
            "qtd": len(nao), "valor": round(sum(float(n.get("valor") or 0) for n in nao), 2),
            "itens": [{"nota_id": n["id"], "cupom": n.get("numero_nfe"), "data": n.get("registrado_em"),
                       "valor": round(float(n.get("valor") or 0), 2), "tem_foto": bool(n.get("foto_path"))} for n in nao],
        },
    }), 200, SEM_CACHE


# ------------------------------------------------------------------ detalhe
@bp_faturas.route("/cashback/api/faturas/<fatura_id>", methods=["GET"])
def api_detalhe(fatura_id):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    f, ids = _fatura_do_cliente(fatura_id, cli)
    if not f:
        return jsonify({"erro": "Fatura não encontrada."}), 404
    emp = f["empresa_id"]
    # parcela: os títulos ficam na fatura ORIGINAL (o extrato da parcela é o da original)
    origem_id = f.get("parcela_de") or f["id"]
    titulos, bol = _par(
        lambda: cp._sget(
            f"oct_pdv_notas_prazo?fatura_id=eq.{origem_id}&select=id,numero_nfe,valor,registrado_em,foto_path"
            f"&order=registrado_em&limit=500"),
        lambda: cp._sget(
            f"oct_boletos?fatura_id=eq.{f['id']}&status=in.(registrado,liquidado)"
            f"&select=id,status,valor,vencimento,linha_digitavel,codigo_barras,nosso_numero,pix_copia_cola,pago_em"
            f"&order=id.desc&limit=1"))
    nums = [str(t["numero_nfe"]) for t in titulos if t.get("numero_nfe") and str(t["numero_nfe"]).isdigit()]
    vendas = {}
    if nums:
        for v in cp._sget(f"oct_pdv_vendas?empresa_id=eq.{emp}&numero=in.({','.join(nums)})"
                          f"&select=numero,data_venda,placa,odometro,veiculo,itens,prazo_dados&limit=500") or []:
            vendas[str(v["numero"])] = v
    linhas, cupons = [], []
    for t in titulos:
        v = vendas.get(str(t.get("numero_nfe")))
        # placa/KM da venda do PDV do Octano ficam em prazo_dados (19/10/2026, igual ao portal empresas e ao PDF da fatura)
        pd = (v or {}).get("prazo_dados") if isinstance((v or {}).get("prazo_dados"), dict) else {}
        placa = (v or {}).get("placa") or pd.get("placa")
        odometro = (v or {}).get("odometro") or pd.get("km") or pd.get("odometro")
        veiculo = (v or {}).get("veiculo") or pd.get("veiculo")
        quando = (v or {}).get("data_venda") or t.get("registrado_em")
        itens = (v or {}).get("itens") if isinstance((v or {}).get("itens"), list) else []
        cupons.append({"nota_id": t["id"], "cupom": t.get("numero_nfe"), "data": quando,
                       "valor": round(float(t.get("valor") or 0), 2), "tem_foto": bool(t.get("foto_path")),
                       "placa": placa})
        if not itens:
            linhas.append({"data": quando, "cupom": t.get("numero_nfe"), "produto": "Compra a prazo", "qtd": 0, "unit": 0,
                           "total": round(float(t.get("valor") or 0), 2), "placa": placa})
            continue
        for it in itens:
            qtd = float(it.get("qtd") or 0)
            unit = float(it.get("unit") or 0)
            total = float(it["total"]) if it.get("total") is not None else qtd * unit
            linhas.append({"data": quando, "cupom": t.get("numero_nfe"), "produto": it.get("desc") or it.get("produto") or "",
                           "qtd": qtd, "unit": round(unit, 3), "total": round(total, 2),
                           "placa": placa, "odometro": odometro,
                           "veiculo": veiculo, "litros": it.get("tipo") == "abastecimento"})
    b = bol[0] if bol else None
    venc = str(f.get("vencimento") or "")[:10]
    _log(cli, f"fatura {f.get('numero')} ({emp[:8]})")
    return jsonify({
        "ok": True,
        "fatura": {
            "id": f["id"], "numero": f.get("numero"), "emissao": f.get("emissao"), "vencimento": venc or None,
            "valor": round(_valor(f), 2), "valor_bruto": round(float(f.get("valor") or 0), 2),
            "acrescimo": round(float(f.get("acrescimo") or 0), 2), "desconto": round(float(f.get("desconto") or 0), 2),
            "status": f.get("status"), "liquidado_em": f.get("liquidado_em"),
            "vencida": f.get("status") == "aberta" and bool(venc) and venc < _hoje(),
            "parcela": (f"{f.get('parcela_num')}/{f.get('parcela_total')}" if f.get("parcela_num") else None),
            "titular": ids.get(f.get("cliente_id"), ("", ""))[1],
            "da_empresa": ids.get(f.get("cliente_id"), ("",))[0] == "empresa",
        },
        "boleto": ({
            "status": b.get("status"), "linha_digitavel": b.get("linha_digitavel"), "codigo_barras": b.get("codigo_barras"),
            "vencimento": b.get("vencimento"), "valor": b.get("valor"), "pago_em": b.get("pago_em"),
            "pix": bool(b.get("pix_copia_cola")), "pix_copia_cola": b.get("pix_copia_cola"),
        } if b else None),
        # fatura: sempre pedível (o gateway gera sob pedido); boleto/NF-e só quando já estão arquivados
        "docs": {"fatura": True, "boleto": bool(f.get("boleto_pdf_path")), "nfe": bool(f.get("nfe_pdf_path"))},
        "pagamento_app": f.get("status") == "aberta" and _pagamento_app_ligado(emp),
        "linhas": linhas, "cupons": cupons,
    }), 200, SEM_CACHE


# ------------------------------------------------------------------ PDF
@bp_faturas.route("/cashback/api/faturas/<fatura_id>/arquivo", methods=["GET"])
def api_arquivo(fatura_id):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    f, _ids = _fatura_do_cliente(fatura_id, cli)
    if not f:
        return jsonify({"erro": "Fatura não encontrada."}), 404
    tipo = (request.args.get("tipo") or "fatura").strip().lower()
    campo = {"fatura": "fatura_pdf_path", "boleto": "boleto_pdf_path", "nfe": "nfe_pdf_path"}.get(tipo)
    if not campo:
        return jsonify({"erro": "Tipo de documento inválido."}), 400
    caminho = f.get(campo)
    if not caminho:
        if tipo == "fatura":
            # o gateway gera sob pedido (mesmo campo que o retaguarda usa) — a tela pergunta de novo em instantes
            try:
                agora = dt.datetime.now(dt.timezone.utc).isoformat()
                if not f.get("fatura_pdf_pedido_em"):
                    cp._spatch(f"oct_faturas?id=eq.{f['id']}&fatura_pdf_path=is.null", {"fatura_pdf_pedido_em": agora})
            except Exception:
                pass
            return jsonify({"ok": True, "gerando": True}), 200, SEM_CACHE
        return jsonify({"erro": "Este documento ainda não está disponível. Tente em alguns minutos."}), 404
    url = _assinar(BUCKET_DOCS, caminho)
    if not url:
        return jsonify({"erro": "O arquivo não está mais disponível."}), 404
    _log(cli, f"pdf {tipo} fatura {f.get('numero')}")
    return jsonify({"ok": True, "url": url, "validade_seg": LINK_SEG}), 200, SEM_CACHE


# ------------------------------------------------------------------ foto do cupom assinado
@bp_faturas.route("/cashback/api/faturas/nota/<nota_id>/foto", methods=["GET"])
def api_foto(nota_id):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    nid = cp._uuid_ok(nota_id)
    rows = cp._sget(f"oct_pdv_notas_prazo?id=eq.{nid}&select=id,empresa_id,cliente_id,foto_path,numero_nfe&limit=1") if nid else []
    n = rows[0] if rows else None
    ids = _contas(cli).get(n.get("empresa_id")) if n else None
    if not n or not ids or n.get("cliente_id") not in ids:
        return jsonify({"erro": "Comprovante não encontrado."}), 404
    url = _assinar(BUCKET_NOTAS, n.get("foto_path"))
    if not url:
        return jsonify({"erro": "A foto deste cupom não está disponível."}), 404
    _log(cli, f"foto do cupom {n.get('numero_nfe')}")
    return jsonify({"ok": True, "url": url, "cupom": n.get("numero_nfe"), "validade_seg": LINK_SEG}), 200, SEM_CACHE


# ------------------------------------------------------------------ Pix (quando o boleto vier com QR)
@bp_faturas.route("/cashback/api/faturas/<fatura_id>/pix.png", methods=["GET"])
def api_pix_png(fatura_id):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    f, _ids = _fatura_do_cliente(fatura_id, cli)
    if not f:
        return jsonify({"erro": "Fatura não encontrada."}), 404
    bol = cp._sget(f"oct_boletos?fatura_id=eq.{f['id']}&status=eq.registrado&select=pix_copia_cola&order=id.desc&limit=1")
    codigo = (bol[0].get("pix_copia_cola") if bol else None) or ""
    if not codigo:
        return jsonify({"erro": "Esta fatura não tem Pix disponível."}), 404
    import qrcode
    buf = io.BytesIO()
    qrcode.make(codigo, box_size=8, border=2).save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png", headers=SEM_CACHE)


# ------------------------------------------------------------------ pagar pelo app (Pix)
def _saldo_fatura(f):
    rec = cp._sget(f"oct_recebimentos_titulo?fatura_id=eq.{f['id']}&select=valor")
    return round(_valor(f) - sum(float(r.get("valor") or 0) for r in rec or []), 2)


def _cobranca_do_cliente(cid, cli):
    """(linha de oct_pix_cobrancas, ids) — so' se for do cliente logado (ou da empresa que ele pode ver)."""
    try:
        n = int(cid)
    except (TypeError, ValueError):
        return None, None
    rows = cp._sget(f"oct_pix_cobrancas?id=eq.{n}&select=*&limit=1")
    if not rows:
        return None, None
    c = rows[0]
    ids = _contas(cli).get(c.get("empresa_id")) or {}
    if c.get("cliente_id") not in ids:
        return None, None
    return c, ids


@bp_faturas.route("/cashback/api/pagar", methods=["POST"])
def api_pagar():
    """Pede o Pix de UMA compra a prazo (botao "Pagar" ao lado da foto do cupom) ou de uma FATURA.
    Grava o pedido em oct_pix_cobrancas; o gateway octano-sicoob cria a cobranca no Sicoob em poucos segundos e
    da' a baixa sozinho quando o banco confirma. O app acompanha por GET /cashback/api/pagar/<id>."""
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    corpo = request.get_json(silent=True) or {}
    nid = cp._uuid_ok(corpo.get("nota_id"))
    fid = cp._uuid_ok(corpo.get("fatura_id"))
    if bool(nid) == bool(fid):
        return jsonify({"erro": "Informe a compra ou a fatura a pagar."}), 400
    if nid:
        rows = cp._sget(f"oct_pdv_notas_prazo?id=eq.{nid}&select=id,empresa_id,cliente_id,valor,status,fatura_id&limit=1")
        n = rows[0] if rows else None
        ids = _contas(cli).get(n.get("empresa_id")) if n else None
        if not n or not ids or n.get("cliente_id") not in ids:
            return jsonify({"erro": "Compra não encontrada."}), 404
        if n.get("status") != "aberto" or n.get("fatura_id"):
            return jsonify({"erro": "Esta compra já foi paga ou já entrou numa fatura."}), 409
        emp, cliente, valor = n["empresa_id"], n["cliente_id"], round(float(n.get("valor") or 0), 2)
        alvo = {"nota_prazo_id": nid}
        filtro = f"nota_prazo_id=eq.{nid}"
    else:
        f, _ids = _fatura_do_cliente(fid, cli)
        if not f:
            return jsonify({"erro": "Fatura não encontrada."}), 404
        if f.get("status") != "aberta":
            return jsonify({"erro": "Esta fatura não está em aberto."}), 409
        emp, cliente, valor = f["empresa_id"], f["cliente_id"], _saldo_fatura(f)
        alvo = {"fatura_id": fid}
        filtro = f"fatura_id=eq.{fid}"
    if not _pagamento_app_ligado(emp):
        return jsonify({"erro": "O pagamento pelo app ainda não está habilitado neste posto. Pague no caixa do posto."}), 503
    if valor <= 0:
        return jsonify({"erro": "Não há valor a pagar neste título."}), 409
    if not cp._limite_ok("pix:" + cli["cpf"], 20, 3600):
        return cp._muitas("Muitos pedidos de Pix seguidos. Espere um pouco.")
    # ja' existe um pedido em andamento para o mesmo alvo? devolve o mesmo (nao cria outra cobranca)
    ab = cp._sget(f"oct_pix_cobrancas?{filtro}&status=in.(pedido,criando,ativa)&order=id.desc&limit=1&select=id,status,expira_em,valor")
    if ab:
        a = ab[0]
        vivo = a.get("status") in ("pedido", "criando") or (a.get("expira_em") and str(a["expira_em"]) > dt.datetime.now(dt.timezone.utc).isoformat())
        if vivo:
            return jsonify({"ok": True, "id": a["id"], "status": a["status"], "valor": float(a.get("valor") or valor)}), 200, SEM_CACHE
    nova = cp._spost("oct_pix_cobrancas", dict(alvo, empresa_id=emp, cliente_id=cliente, valor=valor, status="pedido",
                                              origem="app", criado_por="app " + cp._cpf_mascara(cli["cpf"])))
    row = (nova or [{}])[0]
    _log(cli, f"pedido de Pix {row.get('id')} R$ {valor:.2f} ({emp[:8]})")
    return jsonify({"ok": True, "id": row.get("id"), "status": "pedido", "valor": valor}), 200, SEM_CACHE


@bp_faturas.route("/cashback/api/pagar/<cid>", methods=["GET"])
def api_pagar_status(cid):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    c, _ids = _cobranca_do_cliente(cid, cli)
    if not c:
        return jsonify({"erro": "Pedido não encontrado."}), 404
    st = c.get("status")
    out = {"ok": True, "id": c["id"], "status": st, "valor": float(c.get("valor") or 0)}
    if st == "ativa":
        out.update(copia_cola=c.get("copia_cola"), qr_url=f"/cashback/api/pagar/{c['id']}/qr.png", expira_em=c.get("expira_em"))
    elif st == "paga":
        out.update(pago_em=c.get("pago_em"), baixa=c.get("baixa"))
    elif st == "erro":
        out["erro"] = "Não consegui gerar o Pix agora. Tente de novo em instantes ou pague no caixa do posto."
    elif st == "expirada":
        out["erro"] = "Este Pix expirou. Gere um novo."
    return jsonify(out), 200, SEM_CACHE


@bp_faturas.route("/cashback/api/pagar/<cid>/qr.png", methods=["GET"])
def api_pagar_qr(cid):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    c, _ids = _cobranca_do_cliente(cid, cli)
    if not c or c.get("status") != "ativa" or not c.get("copia_cola"):
        return jsonify({"erro": "QR não disponível."}), 404
    import qrcode
    buf = io.BytesIO()
    qrcode.make(c["copia_cola"], box_size=8, border=2).save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png", headers=SEM_CACHE)
