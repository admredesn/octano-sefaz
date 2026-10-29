# -*- coding: utf-8 -*-
"""
cliente_cartao.py — PAGAR COM CARTÃO DE CRÉDITO PELO APP (20/10/2026, pedido do Ronan).

O cliente paga uma compra a prazo ou uma fatura pelo app Postos SN com cartão de crédito,
parcelado com os JUROS POR CONTA DELE (repasse de taxa do PagBank). Mesmo caminho do Pix pelo
app (cliente_faturas.api_pagar): aprovado = baixa automática da nota/fatura.

Segurança: o cartão é criptografado NO CELULAR pelo SDK do PagBank (chave pública) — o Octano
nunca vê o número. Cartão salvo = token do PagBank (card.id); aqui só o id e o final.

Rotas (login do cliente do app no header Authorization, como o resto do portal):
  GET    /cashback/api/cartao/config?posto=<empresa_id>  -> {ativo, ambiente, env_sdk, public_key, max_parcelas,
                                                            parcela_min, salvos:[{id, bandeira, final, validade}]}
  POST   /cashback/api/cartao/parcelas {nota_id|fatura_id, bin} -> planos com os juros reais do PagBank
  POST   /cashback/api/cartao/sessao3ds                  -> {session, env} para o SDK fazer o 3DS no celular
  POST   /cashback/api/cartao/pagar {nota_id|fatura_id, parcelas, encrypted, bin, titular, salvar,
                                     | cartao_salvo_id, cvv,   auth3ds_id
                                     | carteira:{tipo: GOOGLE_PAY|APPLE_PAY, token}}  -> {id, status, ...}
  GET    /cashback/api/cartao/<id>                       -> estado (reconsulta o PagBank se ficou "em análise")
  DELETE /cashback/api/cartao/salvos/<id>                -> tira o cartão salvo do app

LIGA/DESLIGA (Railway, octano-sefaz):
  CARTAO_APP_ATIVO   = lista de empresa_id ou all (vazio = DESLIGADO)
  PAGBANK_AMBIENTE   = sandbox (padrão) | producao
  PAGBANK_TOKEN_SANDBOX / PAGBANK_TOKEN  = token do PagBank de cada ambiente (conta única do grupo)
  CARTAO_MAX_PARCELAS (6) · CARTAO_PARCELA_MIN (50,00)
Juros: o servidor RECALCULA no PagBank na hora de pagar (o valor que o app mostrou não é confiado).

CARTEIRA DO CELULAR (28/10/2026, Ronan: "puxar o cartão cadastrado no celular"): Google Pay e Apple Pay pelo próprio
PagBank, À VISTA (a carteira não mostra o BIN antes de pagar, e o PagBank só documenta carteira em 1x). O app nativo
(plugin @capgo/capacitor-pay) devolve o token da carteira, que vai inteiro em card.wallet.key — quem abre é o PagBank.
Cada carteira liga sozinha quando a variável dela existe (sufixo _SANDBOX no sandbox):
  GOOGLE_PAY_GATEWAY_MERCHANT_ID[_SANDBOX] = ID da conta no PagBank (ACCO_…, o PagBank informa)
  GOOGLE_PAY_MERCHANT_ID                   = ID do Google Pay & Wallet Console (só produção; sem ele o Google Pay fica fora)
  APPLE_PAY_MERCHANT_ID[_SANDBOX]          = merchant.br.com.redesn.postossn[.sandbox] (certificado do CSR do PagBank)

CONTA DE TESTE NO SANDBOX com o servidor em produção (28/10/2026, Ronan: "o desvio só da conta de teste para o sandbox"):
  CARTAO_SANDBOX_CPFS    = CPFs (só dígitos, vírgula) cuja conta do app usa o PagBank SANDBOX no cartão e na carteira
  CARTAO_SANDBOX_TITULOS = ids das compras/faturas DE TESTE que essas contas podem pagar no sandbox. Qualquer outro título
                           é recusado: pagar uma nota de verdade no sandbox daria baixa sem dinheiro.
"""
import json
import os
import re
import time
import uuid

import requests as rq
from flask import Blueprint, g, has_request_context, jsonify, request

import cashback_portal as cp
from cliente_faturas import (SEM_CACHE, _cliente_ou_401, _contas, _fatura_do_cliente, _log,
                             _saldo_fatura)

bp_cartao = Blueprint("cliente_cartao", __name__)

UA = "OctanoSistemas/1.0 (+https://www.redesn.com.br)"     # sem User-Agent o Cloudflare do PagBank devolve 403 (erro 1010)
SDK_URL = "https://assets.pagseguro.com.br/checkout-sdk-js/rc/dist/browser/pagseguro.min.js"


# ------------------------------------------------------------------ configuração
def _lista_env(nome):
    return {x.strip().lower() for x in os.environ.get(nome, "").split(",") if x.strip()}


def _marcar_teste(cli):
    """Conta de teste (CARTAO_SANDBOX_CPFS): nesta requisição o cartão e a carteira vão para o sandbox."""
    cpf = _so_digitos((cli or {}).get("cpf"))
    g.cartao_teste = bool(cpf) and cpf in {_so_digitos(x) for x in _lista_env("CARTAO_SANDBOX_CPFS")}


def _ambiente():
    if has_request_context() and getattr(g, "cartao_teste", False):
        return "sandbox"
    return "producao" if os.environ.get("PAGBANK_AMBIENTE", "").strip().lower() in ("producao", "prod", "production") else "sandbox"


def _base():
    return "https://api.pagseguro.com" if _ambiente() == "producao" else "https://sandbox.api.pagseguro.com"


def _base_sdk():
    return "https://sdk.pagseguro.com" if _ambiente() == "producao" else "https://sandbox.sdk.pagseguro.com"


def _token():
    nome = "PAGBANK_TOKEN" if _ambiente() == "producao" else "PAGBANK_TOKEN_SANDBOX"
    return os.environ.get(nome, "").strip()


def _max_parcelas():
    try:
        return max(1, min(12, int(os.environ.get("CARTAO_MAX_PARCELAS", "6"))))
    except ValueError:
        return 6


def _parcela_min():
    try:
        return float(os.environ.get("CARTAO_PARCELA_MIN", "50").replace(",", "."))
    except ValueError:
        return 50.0


def _ligado(empresa_id):
    v = os.environ.get("CARTAO_APP_ATIVO", "").strip().lower()
    if not v or not _token():
        return False
    return v in ("all", "todos", "1", "true") or str(empresa_id or "").lower() in [x.strip() for x in v.split(",")]


def _carteiras():
    """Google Pay / Apple Pay ligados neste ambiente: {"google": {...}, "apple": {...}} (o app monta o pedido da carteira)."""
    prod = _ambiente() == "producao"
    suf = "" if prod else "_SANDBOX"
    out = {}
    gw = os.environ.get("GOOGLE_PAY_GATEWAY_MERCHANT_ID" + suf, "").strip()
    mid = os.environ.get("GOOGLE_PAY_MERCHANT_ID", "").strip()
    if gw and (mid or not prod):
        out["google"] = {"ambiente": "production" if prod else "test", "gateway": "pagbank", "gateway_merchant_id": gw,
                         "merchant_id": mid, "merchant_name": "Rede SN", "bandeiras": ["VISA", "MASTERCARD", "ELO"]}
    am = os.environ.get("APPLE_PAY_MERCHANT_ID" + suf, "").strip()
    if am:
        out["apple"] = {"merchant_id": am, "bandeiras": ["visa", "masterCard"]}   # PagBank: Apple Pay só Visa e Mastercard
    return out


_CARTEIRA_TIPOS = {"GOOGLE_PAY": "google", "APPLE_PAY": "apple"}


def _carteira_do_pedido(corpo):
    """(None, None) sem carteira; ({tipo, token}, None) válida; (None, resposta_de_erro) se veio errada ou desligada."""
    c = corpo.get("carteira")
    if not c:
        return None, None
    tipo = str((c or {}).get("tipo") or "").upper() if isinstance(c, dict) else ""
    token = (c or {}).get("token") if isinstance(c, dict) else None
    if tipo not in _CARTEIRA_TIPOS or not isinstance(token, str) or not token.strip() or len(token) > 20000:
        return None, (jsonify({"erro": "Pagamento pela carteira do celular inválido. Tente de novo."}), 400)
    if _CARTEIRA_TIPOS[tipo] not in _carteiras():
        return None, (jsonify({"erro": "Esta carteira ainda não está habilitada. Use o cartão ou o Pix."}), 503)
    return {"tipo": tipo, "token": token.strip()}, None


# ------------------------------------------------------------------ PagBank
def _pb(metodo, caminho, corpo=None, base=None, idem=None, timeout=40):
    h = {"Authorization": "Bearer " + _token(), "Content-Type": "application/json", "accept": "application/json", "User-Agent": UA}
    if idem:
        h["x-idempotency-key"] = idem
    r = rq.request(metodo, (base or _base()) + caminho, headers=h, json=corpo, timeout=timeout)
    try:
        j = r.json()
    except ValueError:
        j = {"_texto": (r.text or "")[:300]}
    return r.status_code, j


_PK = {}


def _public_key():
    """Chave pública da conta (o app criptografa o cartão com ela). Uma por ambiente, guardada na memória."""
    amb = _ambiente()
    if amb in _PK:
        return _PK[amb]
    pk = os.environ.get("PAGBANK_PUBLIC_KEY_SANDBOX" if amb == "sandbox" else "PAGBANK_PUBLIC_KEY", "").strip()
    if not pk:
        st, j = _pb("GET", "/public-keys/card")
        if st >= 400 or not j.get("public_key"):
            st, j = _pb("POST", "/public-keys", {"type": "card"})
        pk = (j or {}).get("public_key") or ""
    if pk:
        _PK[amb] = pk
    return pk


def _planos(valor, bin6):
    """Parcelas com os juros reais do PagBank (juros por conta do comprador). [] se não deu para calcular."""
    centavos = int(round(valor * 100))
    q = (f"/charges/fees/calculate?payment_methods=CREDIT_CARD&value={centavos}&max_installments={_max_parcelas()}"
         f"&max_installments_no_interest=0&credit_card_bin={bin6}")
    st, j = _pb("GET", q, timeout=20)
    if st >= 400:
        return None, (j.get("error_messages") or [{}])[0].get("error") if isinstance(j, dict) else "erro"
    cc = ((j.get("payment_methods") or {}).get("credit_card") or {})
    out = []
    for bandeira, d in cc.items():
        for p in d.get("installment_plans") or []:
            n = int(p["installments"])
            parc = p["installment_value"] / 100
            if n > 1 and parc + 0.004 < _parcela_min():
                continue
            juros = ((p.get("amount") or {}).get("fees") or {}).get("buyer", {}).get("interest", {})
            out.append({"parcelas": n, "valor_parcela": round(parc, 2), "total": round(p["amount"]["value"] / 100, 2),
                        "juros": round((juros.get("total") or 0) / 100, 2), "juros_parcelas": juros.get("installments") or 0,
                        "sem_juros": bool(p.get("interest_free")), "bandeira": bandeira})
        break                                  # uma bandeira por BIN
    return sorted(out, key=lambda x: x["parcelas"]), None


# ------------------------------------------------------------------ o que vai ser pago
def _alvo(corpo, cli):
    """(emp, cliente_id, valor, {nota_prazo_id|fatura_id}, filtro, descricao) ou (None, ..., erro_resposta)."""
    nid = cp._uuid_ok(corpo.get("nota_id"))
    fid = cp._uuid_ok(corpo.get("fatura_id"))
    if bool(nid) == bool(fid):
        return None, (jsonify({"erro": "Informe a compra ou a fatura a pagar."}), 400)
    if getattr(g, "cartao_teste", False) and str(nid or fid).lower() not in _lista_env("CARTAO_SANDBOX_TITULOS"):
        return None, (jsonify({"erro": "Conta de teste: no cartão só a compra de teste pode ser paga. Use o Pix."}), 409)
    if nid:
        rows = cp._sget(f"oct_pdv_notas_prazo?id=eq.{nid}&select=id,empresa_id,cliente_id,valor,status,fatura_id,numero_nfe&limit=1")
        n = rows[0] if rows else None
        ids = _contas(cli).get(n.get("empresa_id")) if n else None
        if not n or not ids or n.get("cliente_id") not in ids:
            return None, (jsonify({"erro": "Compra não encontrada."}), 404)
        if n.get("status") != "aberto" or n.get("fatura_id"):
            return None, (jsonify({"erro": "Esta compra já foi paga ou já entrou numa fatura."}), 409)
        return dict(emp=n["empresa_id"], cliente=n["cliente_id"], valor=round(float(n.get("valor") or 0), 2),
                    campo={"nota_prazo_id": nid}, filtro=f"nota_prazo_id=eq.{nid}",
                    desc=f"Compra a prazo {n.get('numero_nfe') or ''}".strip()), None
    f, _ids = _fatura_do_cliente(fid, cli)
    if not f:
        return None, (jsonify({"erro": "Fatura não encontrada."}), 404)
    if f.get("status") != "aberta":
        return None, (jsonify({"erro": "Esta fatura não está em aberto."}), 409)
    return dict(emp=f["empresa_id"], cliente=f["cliente_id"], valor=_saldo_fatura(f), campo={"fatura_id": fid},
                filtro=f"fatura_id=eq.{fid}", desc=f"Fatura {f.get('numero') or ''}".strip()), None


def _so_digitos(s):
    return re.sub(r"\D", "", str(s or ""))


def _telefone(cli):
    t = _so_digitos(cli.get("telefone"))
    if t.startswith("55") and len(t) > 11:
        t = t[2:]
    return {"country": "55", "area": t[:2], "number": t[2:], "type": "MOBILE"} if len(t) in (10, 11) else None


# ------------------------------------------------------------------ rotas
@bp_cartao.route("/cashback/api/cartao/config", methods=["GET"])
def api_config():
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    _marcar_teste(cli)
    emp = cp._uuid_ok(request.args.get("posto"))
    if not emp or not _ligado(emp):
        return jsonify({"ok": True, "ativo": False}), 200, SEM_CACHE
    salvos = cp._sget(f"oct_cartoes_salvos?cpf_app=eq.{cli['cpf']}&ambiente=eq.{_ambiente()}&ativo=eq.true"
                      "&select=id,bandeira,final,validade&order=usado_em.desc.nullslast,id.desc&limit=5")
    return jsonify({"ok": True, "ativo": True, "ambiente": _ambiente(), "env_sdk": "PROD" if _ambiente() == "producao" else "SANDBOX",
                    "sdk_url": SDK_URL, "public_key": _public_key(), "max_parcelas": _max_parcelas(),
                    "parcela_min": _parcela_min(), "salvos": salvos or [], "carteiras": _carteiras()}), 200, SEM_CACHE


@bp_cartao.route("/cashback/api/cartao/parcelas", methods=["POST"])
def api_parcelas():
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    _marcar_teste(cli)
    corpo = request.get_json(silent=True) or {}
    a, err = _alvo(corpo, cli)
    if err:
        return err
    if not _ligado(a["emp"]):
        return jsonify({"erro": "O cartão pelo app ainda não está habilitado neste posto."}), 503
    bin6 = _so_digitos(corpo.get("bin"))[:6]
    if not bin6 and corpo.get("cartao_salvo_id"):
        s = _cartao_salvo(corpo.get("cartao_salvo_id"), cli)
        bin6 = (s or {}).get("primeiros") or ""
    if len(bin6) != 6:
        return jsonify({"erro": "Informe os 6 primeiros números do cartão."}), 400
    planos, motivo = _planos(a["valor"], bin6)
    if not planos:
        msg = "Cartão não reconhecido. Confira o número." if motivo == "credit_card_bin_data_not_found" else "Não consegui calcular as parcelas agora."
        return jsonify({"erro": msg}), 422
    return jsonify({"ok": True, "valor": a["valor"], "planos": planos}), 200, SEM_CACHE


@bp_cartao.route("/cashback/api/cartao/sessao3ds", methods=["POST"])
def api_sessao3ds():
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    _marcar_teste(cli)
    if not cp._limite_ok("3ds:" + cli["cpf"], 30, 3600):
        return cp._muitas()
    st, j = _pb("POST", "/checkout-sdk/sessions", {}, base=_base_sdk(), timeout=20)
    if st >= 400 or not j.get("session"):
        return jsonify({"erro": "Não consegui iniciar a confirmação do banco. Tente de novo."}), 502
    return jsonify({"ok": True, "session": j["session"], "expires_at": j.get("expires_at"),
                    "env": "PROD" if _ambiente() == "producao" else "SANDBOX"}), 200, SEM_CACHE


def _cartao_salvo(sid, cli):
    try:
        n = int(sid)
    except (TypeError, ValueError):
        return None
    rows = cp._sget(f"oct_cartoes_salvos?id=eq.{n}&cpf_app=eq.{cli['cpf']}&ambiente=eq.{_ambiente()}&ativo=eq.true&limit=1")
    return rows[0] if rows else None


def _status_do_pagbank(charge):
    st = (charge or {}).get("status")
    return {"PAID": "paga", "AUTHORIZED": "paga", "DECLINED": "recusada", "CANCELED": "cancelada",
            "IN_ANALYSIS": "analise", "WAITING": "analise"}.get(st, "erro")


def _gravar_resultado(cid, charge, order_id, cli, salvar):
    """Atualiza a cobrança com a resposta do PagBank; paga -> baixa automática; salvar -> guarda o token."""
    pm = (charge or {}).get("payment_method") or {}
    card = pm.get("card") or {}
    resp = (charge or {}).get("payment_response") or {}
    auth = pm.get("authentication_method") or {}
    status = _status_do_pagbank(charge)
    upd = {"status": status, "pagbank_order_id": order_id, "pagbank_charge_id": (charge or {}).get("id"),
           "bandeira": card.get("brand"), "cartao_final": card.get("last_digits"),
           "autorizacao": resp.get("reference") or resp.get("code"), "mensagem": resp.get("message"),
           "tres_ds": auth.get("status") or ("sem 3DS" if not auth else None),
           "atualizado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if status == "paga":
        upd["pago_em"] = (charge or {}).get("paid_at") or upd["atualizado_em"]
    cp._spatch(f"oct_cartao_cobrancas?id=eq.{cid}", upd)
    if salvar and card.get("id") and status == "paga":
        try:
            cp._spost("oct_cartoes_salvos?on_conflict=ambiente,pagbank_card_id", {"cpf_app": cli["cpf"], "ambiente": _ambiente(), "pagbank_card_id": card["id"],
                                             "bandeira": card.get("brand"), "primeiros": card.get("first_digits"),
                                             "final": card.get("last_digits"),
                                             "validade": f"{card.get('exp_month', '')}/{card.get('exp_year', '')}"},
                      prefer="resolution=ignore-duplicates,return=minimal")
        except Exception as e:                       # cartão salvo é conveniência: falhar aqui não desfaz o pagamento
            print(f"[cartao] nao salvei o cartao da cobranca {cid}: {e}")
    baixa = None
    if status == "paga":
        baixa = cp._rpc("oct_cartao_lancar_baixa", {"p_id": cid})
    return status, upd, baixa


@bp_cartao.route("/cashback/api/cartao/pagar", methods=["POST"])
def api_pagar_cartao():
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    _marcar_teste(cli)
    corpo = request.get_json(silent=True) or {}
    a, err = _alvo(corpo, cli)
    if err:
        return err
    if not _ligado(a["emp"]):
        return jsonify({"erro": "O cartão pelo app ainda não está habilitado neste posto. Pague no caixa do posto."}), 503
    if a["valor"] <= 0:
        return jsonify({"erro": "Não há valor a pagar neste título."}), 409
    if not cp._limite_ok("cartao:" + cli["cpf"], 10, 3600):
        return cp._muitas("Muitas tentativas de pagamento seguidas. Espere um pouco.")
    # já existe uma cobrança paga/em análise para o mesmo título? não cobra de novo
    # ("pedido" só segura por 10 min: uma cobrança que travou no meio não bloqueia o cliente para sempre)
    dez_min = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    ja = cp._sget(f"oct_cartao_cobrancas?{a['filtro']}&or=(status.in.(paga,analise),and(status.eq.pedido,criado_em.gte.{dez_min}))"
                  "&order=id.desc&limit=1&select=id,status")
    if ja:
        return jsonify({"erro": "Este título já tem um pagamento em andamento.", "id": ja[0]["id"], "status": ja[0]["status"]}), 409

    carteira, err = _carteira_do_pedido(corpo)
    if err:
        return err
    if carteira:                                   # Google Pay / Apple Pay: à vista, sem juros
        # o cliente aprovou na carteira o valor que o app mostrava: se o título mudou, não cobra outro valor
        try:
            v_app = round(float(corpo.get("valor")), 2) if corpo.get("valor") is not None else a["valor"]
        except (TypeError, ValueError):
            v_app = None
        if v_app is None or abs(v_app - a["valor"]) > 0.005:
            v = f"{a['valor']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            return jsonify({"erro": f"O valor deste título agora é R$ {v}. Confira e pague de novo.", "valor": a["valor"]}), 409
        n_parc, salvo, encrypted = 1, None, ""
        plano = {"parcelas": 1, "valor_parcela": a["valor"], "total": a["valor"], "juros": 0, "juros_parcelas": 0}
    else:
        try:
            n_parc = int(corpo.get("parcelas") or 1)
        except (TypeError, ValueError):
            n_parc = 1
        salvo = _cartao_salvo(corpo.get("cartao_salvo_id"), cli) if corpo.get("cartao_salvo_id") else None
        encrypted = str(corpo.get("encrypted") or "").strip()
        if not salvo and not encrypted:
            return jsonify({"erro": "Informe o cartão."}), 400
        bin6 = (salvo or {}).get("primeiros") or _so_digitos(corpo.get("bin"))[:6]
        planos, _m = _planos(a["valor"], bin6) if len(bin6 or "") == 6 else (None, None)
        plano = next((p for p in planos or [] if p["parcelas"] == n_parc), None)
        if not plano:
            return jsonify({"erro": "Parcelamento indisponível para este valor/cartão. Escolha outra opção."}), 422

    # 1) registra ANTES de cobrar (se a resposta do PagBank se perder, a linha fica e dá para conferir)
    origem = "app-" + carteira["tipo"].lower().replace("_", "-") if carteira else "app"     # app-google-pay | app-apple-pay
    nova = cp._spost("oct_cartao_cobrancas", dict(a["campo"], empresa_id=a["emp"], cliente_id=a["cliente"], cpf_app=cli["cpf"],
                                                  valor=a["valor"], parcelas=n_parc, juros_cliente=plano["juros"],
                                                  valor_cobrado=plano["total"], status="pedido", ambiente=_ambiente(),
                                                  cartao_salvo_id=(salvo or {}).get("id"), origem=origem))
    cid = (nova or [{}])[0].get("id")

    # 2) pedido no PagBank
    centavos = int(round(a["valor"] * 100))
    amount = {"value": int(round(plano["total"] * 100)), "currency": "BRL"}
    if plano["juros"] > 0:
        amount["fees"] = {"buyer": {"interest": {"total": int(round(plano["juros"] * 100)), "installments": plano["juros_parcelas"] or n_parc}}}
    if carteira:                                   # o token da carteira vai inteiro; o PagBank abre (card.wallet)
        card = {"wallet": {"type": carteira["tipo"], "key": carteira["token"]}}
    elif salvo:
        card = {"id": salvo["pagbank_card_id"], "security_code": _so_digitos(corpo.get("cvv"))[:4]}
    else:
        card = {"encrypted": encrypted, "store": bool(corpo.get("salvar"))}
    titular = (str(corpo.get("titular") or cli.get("nome") or "").strip().upper()[:60]) or "TITULAR"
    pm = {"type": "CREDIT_CARD", "installments": n_parc, "capture": True, "soft_descriptor": "REDE SN", "card": card}
    if not carteira:                               # na carteira o titular vem do próprio cartão do celular
        pm["holder"] = {"name": titular, "tax_id": cli["cpf"]}
    if corpo.get("auth3ds_id"):
        pm["authentication_method"] = {"type": "THREEDS", "id": str(corpo["auth3ds_id"])[:80]}
    cliente = {"name": (cli.get("nome") or titular)[:60], "email": cli.get("email") or "cliente@redesn.com.br", "tax_id": cli["cpf"]}
    tel = _telefone(cli)
    if tel:
        cliente["phones"] = [tel]
    pedido = {"reference_id": f"oct-cartao-{cid}", "customer": cliente,
              "items": [{"reference_id": a["desc"][:60] or "titulo", "name": (a["desc"] or "Abastecimento")[:60],
                         "quantity": 1, "unit_amount": centavos}],
              "charges": [{"reference_id": f"oct-cartao-{cid}", "description": (a["desc"] or "Rede SN")[:60],
                           "amount": amount, "payment_method": pm}]}
    try:
        # chave ÚNICA por tentativa (20/10/2026): "oct-cartao-<id>" repetia entre os testes (banco simulado, id 1) e
        # a produção, e o PagBank recusava com 409 IDEMPOTENCY_CONFLICT ("request body must be the same")
        st, j = _pb("POST", "/orders", pedido, idem=f"oct-cartao-{cid}-{uuid.uuid4().hex[:16]}")
    except rq.RequestException as e:
        cp._spatch(f"oct_cartao_cobrancas?id=eq.{cid}", {"status": "erro", "erro": f"sem resposta do PagBank: {e}"[:300]})
        return jsonify({"erro": "Não consegui falar com o PagBank. Confira no app do seu banco antes de tentar de novo.", "id": cid}), 502
    if st >= 400 or not (j.get("charges") or []):
        msg = json.dumps(j.get("error_messages") or j, ensure_ascii=False)[:300]
        cp._spatch(f"oct_cartao_cobrancas?id=eq.{cid}", {"status": "erro", "erro": f"HTTP {st}: {msg}"})
        _log(cli, f"cartao {cid} ERRO {st} {msg[:120]}")
        return jsonify({"erro": "Pagamento não realizado. Confira os dados do cartão ou use outra forma.", "id": cid}), 422
    status, upd, baixa = _gravar_resultado(cid, j["charges"][0], j.get("id"), cli, bool(corpo.get("salvar")) and not salvo)
    if salvo:
        cp._spatch(f"oct_cartoes_salvos?id=eq.{salvo['id']}", {"usado_em": upd["atualizado_em"]})
    _log(cli, f"cartao {cid} {status} R$ {a['valor']:.2f} {n_parc}x{' ' + carteira['tipo'] if carteira else ''} "
              f"({a['emp'][:8]}) {upd.get('mensagem') or ''}")
    out = {"ok": status == "paga", "id": cid, "status": status, "valor": a["valor"], "parcelas": n_parc,
           "valor_parcela": plano["valor_parcela"], "total": plano["total"], "bandeira": upd.get("bandeira"),
           "final": upd.get("cartao_final"), "autorizacao": upd.get("autorizacao"), "baixa": baixa}
    if status == "recusada":
        out["erro"] = "Pagamento recusado pelo banco do cartão. Tente outro cartão ou outra forma."
    elif status == "analise":
        out["aviso"] = "Pagamento em análise. Avisamos aqui assim que o PagBank confirmar."
    return jsonify(out), 200, SEM_CACHE


@bp_cartao.route("/cashback/api/cartao/<int:cid>", methods=["GET"])
def api_cartao_status(cid):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    _marcar_teste(cli)
    rows = cp._sget(f"oct_cartao_cobrancas?id=eq.{cid}&cpf_app=eq.{cli['cpf']}&limit=1")
    if not rows:
        return jsonify({"erro": "Pagamento não encontrado."}), 404
    c = rows[0]
    if c.get("ambiente") == "sandbox":              # reconsulta no ambiente em que a cobrança foi feita
        g.cartao_teste = True
    if c["status"] == "analise" and c.get("pagbank_order_id"):
        st, j = _pb("GET", f"/orders/{c['pagbank_order_id']}", timeout=20)
        if st < 400 and j.get("charges"):
            status, _upd, baixa = _gravar_resultado(cid, j["charges"][0], c["pagbank_order_id"], cli, False)
            c.update(status=status, baixa=baixa or c.get("baixa"))
    return jsonify({"ok": True, "id": c["id"], "status": c["status"], "valor": float(c["valor"]), "parcelas": c["parcelas"],
                    "total": float(c.get("valor_cobrado") or c["valor"]), "bandeira": c.get("bandeira"),
                    "final": c.get("cartao_final"), "baixa": c.get("baixa")}), 200, SEM_CACHE


@bp_cartao.route("/cashback/api/cartao/salvos/<int:sid>", methods=["DELETE"])
def api_cartao_remover(sid):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    _marcar_teste(cli)
    cp._spatch(f"oct_cartoes_salvos?id=eq.{sid}&cpf_app=eq.{cli['cpf']}", {"ativo": False})
    return jsonify({"ok": True}), 200, SEM_CACHE
