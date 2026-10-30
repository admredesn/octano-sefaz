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
  POST   /cashback/api/cartao/parcelas {nota_id|fatura_id|acionamento_id, bin} -> planos com os juros reais do PagBank
  POST   /cashback/api/cartao/sessao3ds                  -> {session, env} para o SDK fazer o 3DS no celular
  POST   /cashback/api/cartao/pagar {nota_id|fatura_id|acionamento_id, parcelas, encrypted, bin, titular, salvar,
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

ABASTECIMENTO (29/10/2026, Ronan: "colocar a opção de pagamento via cartão nos abastecimentos também, igual funciona no
pagamento de fatura"; decidiu: paga DEPOIS de abastecer e a venda FECHA SOZINHA no PDV). No "Vou abastecer" o cliente
escolhe "Cartão pelo app" (acionamento com pagar_app='cartao'); quando a bomba termina, paga aqui com {acionamento_id}:
o valor é o do abastecimento que a bomba gravou (oct_pdv_abastecimentos), e a cobrança guarda acionamento_id e
abastecimento_id. Aprovado = oct_cartao_lancar_baixa marca o acionamento como pago; quem fecha a venda é o NÚCLEO do posto
(pdv2/app_pago.py), que lê esta tabela e casa o pagamento com o abastecimento. Sem Pix aqui (o Pix do app só baixa título).
Liga à parte do título, porque o cliente abastece ANTES de pagar:
  CARTAO_ABASTECIMENTO_ATIVO   = lista de empresa_id ou all (vazio = ninguém vê a opção)
  CARTAO_SANDBOX_ABASTECIMENTO = 1 deixa a conta de teste pagar abastecimento no sandbox (abastecimento DE VERDADE pago
                                 sem dinheiro: só num teste combinado com o posto)

MERCADO PAGO (30/10/2026, Ronan: o PagBank não liberou a produção; "o Mercado Pago ... será que ele não tem uma opção mais
imediata?" — o MP libera a produção no próprio painel, sem homologação). Mesmas rotas e telas; muda o provedor:
o cartão vira TOKEN no celular pelo SDK do MP (o número e o CVV nunca passam por aqui), o pedido vai na Orders API
(/v1/orders) e o MP soma sozinho os juros do parcelamento no cartão (o pedido leva o valor do título).
Cartão salvo = customer + card do MP, guardado em oct_cartoes_salvos.pagbank_card_id como "MP:<customer>:<card>"
(o celular gera o token do salvo com o card id + CVV). A cobrança grava origem "app-mercadopago", o id do pedido em
pagbank_order_id e o do pagamento em pagbank_charge_id. Sem carteira do celular no MP (por enquanto).
  CARTAO_MP_ATIVO          = lista de empresa_id ou all: esses postos cobram pelo Mercado Pago (o resto segue PagBank)
  CARTAO_SANDBOX_PROVEDOR  = mercadopago: a conta de teste (CARTAO_SANDBOX_CPFS) usa o sandbox do MP
  MP_ACCESS_TOKEN / MP_PUBLIC_KEY                  = credenciais de produção do MP (conta PJ que recebe)
  MP_ACCESS_TOKEN_SANDBOX / MP_PUBLIC_KEY_SANDBOX  = credenciais de teste
  O ambiente (sandbox/producao) é o mesmo do PagBank (PAGBANK_AMBIENTE + desvio da conta de teste).
  CARTAO_TESTE_REAL        = 1: a conta de teste usa a PRODUÇÃO (cartão e dinheiro de verdade), ainda só nos títulos de
                             CARTAO_SANDBOX_TITULOS (30/10/2026, Ronan: "nos conseguimos fazer um teste real?" — R$ 5,00
                             pela conta PF dele, credenciais de produção da PF em MP_ACCESS_TOKEN/MP_PUBLIC_KEY só no teste)
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
        # CARTAO_TESTE_REAL=1: a conta de teste paga DE VERDADE (produção), mas só os títulos de CARTAO_SANDBOX_TITULOS
        return "producao" if os.environ.get("CARTAO_TESTE_REAL", "").strip().lower() in ("1", "true", "sim") else "sandbox"
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
    if not v or not (_mp_token() if _provedor(empresa_id) == "mercadopago" else _token()):
        return False
    return v in ("all", "todos", "1", "true") or str(empresa_id or "").lower() in [x.strip() for x in v.split(",")]


def _abast_ligado(empresa_id):
    """Cartão pelo app no ABASTECIMENTO neste posto, para esta conta (a conta de teste só com CARTAO_SANDBOX_ABASTECIMENTO)."""
    if not _ligado(empresa_id):
        return False
    if has_request_context() and getattr(g, "cartao_teste", False):
        return os.environ.get("CARTAO_SANDBOX_ABASTECIMENTO", "").strip().lower() in ("1", "true", "sim")
    v = os.environ.get("CARTAO_ABASTECIMENTO_ATIVO", "").strip().lower()
    return v in ("all", "todos", "1", "true") or str(empresa_id or "").lower() in [x.strip() for x in v.split(",") if x.strip()]


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


def _planos(valor, bin6, prov="pagbank"):
    return _mp_planos(valor, bin6) if prov == "mercadopago" else _pb_planos(valor, bin6)


def _pb_planos(valor, bin6):
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


# ------------------------------------------------------------------ Mercado Pago
MP_BASE = "https://api.mercadopago.com"
MP_SDK_URL = "https://sdk.mercadopago.com/js/v2"
MP_ORIGEM = "app-mercadopago"


def _provedor(empresa_id=None):
    """pagbank | mercadopago. A conta de teste segue CARTAO_SANDBOX_PROVEDOR; os postos, CARTAO_MP_ATIVO."""
    if has_request_context() and getattr(g, "cartao_teste", False):
        return "mercadopago" if os.environ.get("CARTAO_SANDBOX_PROVEDOR", "").strip().lower() in ("mercadopago", "mp") else "pagbank"
    v = os.environ.get("CARTAO_MP_ATIVO", "").strip().lower()
    if v and (v in ("all", "todos", "1", "true") or str(empresa_id or "").lower() in [x.strip() for x in v.split(",")]):
        return "mercadopago"
    return "pagbank"


def _mp_token():
    return os.environ.get("MP_ACCESS_TOKEN" if _ambiente() == "producao" else "MP_ACCESS_TOKEN_SANDBOX", "").strip()


def _mp_public_key():
    return os.environ.get("MP_PUBLIC_KEY" if _ambiente() == "producao" else "MP_PUBLIC_KEY_SANDBOX", "").strip()


def _mp(metodo, caminho, corpo=None, params=None, idem=None, device=None, timeout=40):
    h = {"Authorization": "Bearer " + _mp_token(), "Content-Type": "application/json", "accept": "application/json", "User-Agent": UA}
    if idem:
        h["X-Idempotency-Key"] = idem
    if device:
        h["X-meli-session-id"] = device            # identificação do aparelho (SDK): o antifraude do MP recusa menos
    r = rq.request(metodo, MP_BASE + caminho, headers=h, json=corpo, params=params, timeout=timeout)
    try:
        j = r.json()
    except ValueError:
        j = {"_texto": (r.text or "")[:300]}
    return r.status_code, j


def _mp_planos(valor, bin6):
    """Parcelas com os juros do Mercado Pago (o MP soma os juros no cartão; quem paga é o comprador)."""
    st, j = _mp("GET", "/v1/payment_methods/installments", params={"amount": f"{valor:.2f}", "bin": bin6,
                                                                   "payment_type_id": "credit_card"}, timeout=20)
    if st == 404 or (st < 400 and not j):
        return None, "credit_card_bin_data_not_found"
    if st >= 400 or not isinstance(j, list):
        return None, "erro"
    m = j[0]
    out = []
    for p in m.get("payer_costs") or []:
        n = int(p["installments"])
        parc = round(float(p["installment_amount"]), 2)
        if n > _max_parcelas() or (n > 1 and parc + 0.004 < _parcela_min()):
            continue
        total = round(float(p["total_amount"]), 2)
        juros = round(max(0.0, total - valor), 2)
        out.append({"parcelas": n, "valor_parcela": parc, "total": total, "juros": juros, "juros_parcelas": n if juros else 0,
                    "sem_juros": not juros, "bandeira": m.get("payment_method_id")})
    return sorted(out, key=lambda x: x["parcelas"]), None


def _status_do_mp(order):
    return {"processed": "paga", "failed": "recusada", "canceled": "cancelada", "expired": "cancelada", "refunded": "cancelada",
            "charged_back": "cancelada", "processing": "analise", "action_required": "analise", "created": "analise"
            }.get((order or {}).get("status"), "erro")


def _mp_email(cli):
    """E-mail do cliente no MP. O sandbox só aceita e-mail de usuário de teste; na produção, um por CPF (sem misturar contas)."""
    cpf = _so_digitos(cli.get("cpf"))
    return f"test_user_{cpf}@testuser.com" if _ambiente() != "producao" else f"app{cpf}@clientes.redesn.com.br"


def _mp_cliente(cli):
    """Customer do MP desta conta do app: o de um cartão já salvo, o achado pelo e-mail ou um novo."""
    rows = cp._sget(f"oct_cartoes_salvos?cpf_app=eq.{cli['cpf']}&ambiente=eq.{_ambiente()}&pagbank_card_id=like.MP:*"
                    "&select=pagbank_card_id&limit=1")
    if rows:
        return rows[0]["pagbank_card_id"].split(":")[1]
    email = _mp_email(cli)
    st, j = _mp("GET", "/v1/customers/search", params={"email": email}, timeout=20)
    if st < 400 and (j.get("results") or []):
        return j["results"][0]["id"]
    nome = str(cli.get("nome") or "").split()
    st, j = _mp("POST", "/v1/customers", {"email": email, "first_name": (nome[0] if nome else "")[:40],
                                          "last_name": " ".join(nome[1:])[:60],
                                          "identification": {"type": "CPF", "number": _so_digitos(cli.get("cpf"))}}, timeout=20)
    return j.get("id") if st < 400 else None


def _mp_salvar_cartao(cli, token):
    """Guarda no MP o cartão do pagamento aprovado (o mesmo token serve) e anota aqui o id e o final."""
    cust = _mp_cliente(cli)
    if not cust:
        print("[cartao] MP: nao consegui o customer para salvar o cartao")
        return
    st, c = _mp("POST", f"/v1/customers/{cust}/cards", {"token": token}, timeout=30)
    if st >= 400 or not c.get("id"):
        print(f"[cartao] MP: cartao nao salvo HTTP {st} {json.dumps(c, ensure_ascii=False)[:200]}")
        return
    cp._spost("oct_cartoes_salvos?on_conflict=ambiente,pagbank_card_id",
              {"cpf_app": cli["cpf"], "ambiente": _ambiente(), "pagbank_card_id": f"MP:{cust}:{c['id']}",
               "bandeira": (c.get("payment_method") or {}).get("id"), "primeiros": c.get("first_six_digits"),
               "final": c.get("last_four_digits"), "validade": f"{c.get('expiration_month', '')}/{c.get('expiration_year', '')}"},
              prefer="resolution=ignore-duplicates,return=minimal")


def _mp_gravar(cid, order, cli, salvar_token=None, final=None):
    """Atualiza a cobrança com o pedido do MP; paga -> baixa automática; salvar_token -> guarda o cartão."""
    pay = (((order or {}).get("transactions") or {}).get("payments") or [{}])[0]
    status = _status_do_mp(order)
    upd = {"status": status, "pagbank_order_id": order.get("id"), "pagbank_charge_id": pay.get("id"),
           "bandeira": (pay.get("payment_method") or {}).get("id"), "autorizacao": pay.get("reference_id"),
           "mensagem": pay.get("status_detail") or order.get("status_detail"), "tres_ds": "sem 3DS",
           "atualizado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if final:
        upd["cartao_final"] = final
    if status == "paga":
        upd["pago_em"] = order.get("last_updated_date") or upd["atualizado_em"]
    cp._spatch(f"oct_cartao_cobrancas?id=eq.{cid}", upd)
    if salvar_token and status == "paga":
        try:
            _mp_salvar_cartao(cli, salvar_token)
        except Exception as e:                       # cartão salvo é conveniência: falhar aqui não desfaz o pagamento
            print(f"[cartao] nao salvei o cartao da cobranca {cid}: {e}")
    baixa = cp._rpc("oct_cartao_lancar_baixa", {"p_id": cid}) if status == "paga" else None
    return status, upd, baixa


def _mp_3ds():
    """Confirmação do banco (3DS) no MP: on_fraud_risk (padrão) = em vez de RECUSAR o que achar arriscado, o MP pede ao
    cliente para confirmar no banco (30/10/2026: o teste real de R$ 5,00 voltou "high_risk"). never = desliga."""
    v = os.environ.get("MP_3DS", "on_fraud_risk").strip().lower()
    return v if v in ("on_fraud_risk", "always") else None


def _mp_pagador(cli, corpo):
    """Tudo o que o MP recomenda mandar do comprador (o antifraude recusa menos com dados completos)."""
    nome = str(cli.get("nome") or corpo.get("titular") or "").split()
    email = str(cli.get("email") or "").strip().lower()
    p = {"email": _mp_email(cli) if _ambiente() != "producao" or "@" not in email else email,
         "first_name": (nome[0] if nome else "")[:40].title(), "last_name": " ".join(nome[1:])[:60].title(),
         "identification": {"type": "CPF", "number": _so_digitos(cli.get("cpf"))}}
    tel = _telefone(cli)
    if tel:
        p["phone"] = {"area_code": tel["area"], "number": tel["number"]}
    cep = _so_digitos(cli.get("cep"))
    if len(cep) == 8 and cli.get("endereco"):
        p["address"] = {k: v for k, v in {"zip_code": cep, "street_name": str(cli.get("endereco"))[:80],
                                          "street_number": str(cli.get("numero") or "S/N")[:10],
                                          "neighborhood": str(cli.get("bairro") or "")[:60], "city": str(cli.get("cidade") or "")[:60],
                                          "state": str(cli.get("uf") or "")[:2].upper()}.items() if v}
    return p


def _mp_info_extra(cli, corpo):
    """additional_info: canal, data de cadastro no app e histórico de compras pagas com cartão no app."""
    info = {"payer.authentication_type": "MOBILE" if corpo.get("nativo") else "WEB", "payer.is_prime_user": False}
    if cli.get("criado_em"):
        info["payer.registration_date"] = str(cli["criado_em"])
    try:
        ult = cp._sget(f"oct_cartao_cobrancas?cpf_app=eq.{cli['cpf']}&status=eq.paga&ambiente=eq.{_ambiente()}"
                       "&select=pago_em&order=pago_em.desc.nullslast&limit=1")
    except Exception:
        ult = None
    info["payer.is_first_purchase_online"] = not ult
    if ult and ult[0].get("pago_em"):
        info["payer.last_purchase"] = str(ult[0]["pago_em"])
    return info


def _mp_pedido(cid, a, cli, corpo, plano, n_parc, salvo, token):
    """Pedido no Mercado Pago (Orders API). Recusa vem com HTTP 402 e o pedido em "data"."""
    v = f"{a['valor']:.2f}"
    desc = (a["desc"] or "Rede SN")[:60]
    payer = _mp_pagador(cli, corpo)
    if salvo:                                      # cartão salvo: o customer dono do cartão (o e-mail é o dele)
        payer.pop("email", None)
        payer["customer_id"] = salvo["pagbank_card_id"].split(":")[1]
    pedido = {"type": "online", "processing_mode": "automatic", "total_amount": v, "external_reference": f"oct-cartao-{cid}",
              "description": desc, "payer": payer,
              "items": [{"title": desc, "description": "Posto de combustível - Rede SN", "unit_price": v, "quantity": 1,
                         "category_id": "others", "external_code": str(cid)}],
              "additional_info": _mp_info_extra(cli, corpo),
              "transactions": {"payments": [{"amount": v, "payment_method": {
                  "id": plano.get("bandeira"), "type": "credit_card", "token": token, "installments": n_parc,
                  "statement_descriptor": "REDE SN"}}]}}
    tres = _mp_3ds()
    if tres:
        pedido["config"] = {"online": {"transaction_security": {"validation": tres, "liability_shift": "required"}}}
    device = str(corpo.get("device_id") or "").strip()[:300] or None
    st, j = _mp("POST", "/v1/orders", pedido, idem=f"oct-cartao-{cid}-{uuid.uuid4().hex[:16]}", device=device)
    if st == 402 and isinstance(j.get("data"), dict):
        st, j = st, j["data"]
    j["_device"] = bool(device)
    return st, j


def _mp_atualizar(c, cli):
    """Reconsulta no MP uma cobrança "analise" (atualiza c) e devolve a URL do desafio se ainda estiver pendente."""
    st, j = _mp("GET", f"/v1/orders/{c['pagbank_order_id']}", timeout=20)
    if st >= 400 or not j.get("id"):
        return None
    status, _upd, baixa = _mp_gravar(c["id"], j, cli)
    c.update(status=status, baixa=baixa or c.get("baixa"))
    return _mp_desafio(j)


def _mp_desafio(order):
    """URL da confirmação do banco (3DS) quando o MP pediu o desafio; None se não pediu."""
    pay = (((order or {}).get("transactions") or {}).get("payments") or [{}])[0]
    if (order or {}).get("status") != "action_required":
        return None
    return ((pay.get("payment_method") or {}).get("transaction_security") or {}).get("url")


# ------------------------------------------------------------------ o que vai ser pago
def _alvo(corpo, cli):
    """(emp, cliente_id, valor, {nota_prazo_id|fatura_id|acionamento_id+abastecimento_id}, filtro, descricao) ou
    (None, erro_resposta)."""
    nid = cp._uuid_ok(corpo.get("nota_id"))
    fid = cp._uuid_ok(corpo.get("fatura_id"))
    aid = cp._uuid_ok(corpo.get("acionamento_id"))
    if aid and not nid and not fid:
        return _alvo_abastecimento(aid, cli)
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


def abastecimento_do_acionamento(ac):
    """O abastecimento do bico acionado, gravado DEPOIS do acionamento (a mesma busca do espelho ao vivo do app)."""
    if not ac.get("bico"):
        return None
    rows = cp._sget(f"oct_pdv_abastecimentos?empresa_id=eq.{ac['empresa_id']}&bico=eq.{ac['bico']}"
                    f"&data_abast=gte.{rq.utils.quote(cp._hora_do_posto(ac['criado_em']), safe='')}"
                    "&select=id,litros,valor,status,data_abast&order=data_abast.desc&limit=1")
    return rows[0] if rows else None


def _alvo_abastecimento(aid, cli):
    """Abastecimento acionado no app com "Cartão pelo app": só o dono, só uma vez e só enquanto o caixa não fechou."""
    rows = cp._sget(f"oct_cashback_acionamentos?id=eq.{aid}&cliente_cpf=eq.{cli['cpf']}&limit=1"
                    "&select=id,empresa_id,status,bico,criado_em,pagar_app,pago_app,pessoa_id")
    ac = rows[0] if rows else None
    if not ac:
        return None, (jsonify({"erro": "Abastecimento não encontrado."}), 404)
    if ac.get("pagar_app") != "cartao":
        return None, (jsonify({"erro": "Este abastecimento não foi marcado para pagar no app. Pague no caixa."}), 409)
    if ac.get("pago_app"):
        return None, (jsonify({"erro": "Este abastecimento já foi pago."}), 409)
    if ac.get("status") != "aguardando":
        return None, (jsonify({"erro": "Este abastecimento já foi fechado no caixa."}), 409)
    if not _abast_ligado(ac["empresa_id"]):
        return None, (jsonify({"erro": "O cartão pelo app ainda não está habilitado neste posto. Pague no caixa."}), 503)
    ab = abastecimento_do_acionamento(ac)
    if not ab:
        return None, (jsonify({"erro": "O abastecimento ainda não chegou ao sistema. Espere uns segundos e tente de novo."}), 409)
    if (ab.get("status") or "pendente") != "pendente":
        return None, (jsonify({"erro": "Este abastecimento já foi fechado no caixa."}), 409)
    return dict(emp=ac["empresa_id"], cliente=ac.get("pessoa_id"), valor=round(float(ab.get("valor") or 0), 2),
                campo={"acionamento_id": aid, "abastecimento_id": ab["id"]}, filtro=f"acionamento_id=eq.{aid}",
                desc=f"Abastecimento bico {ac.get('bico')}"), None


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
    prov = _provedor(emp)
    salvos = cp._sget(f"oct_cartoes_salvos?cpf_app=eq.{cli['cpf']}&ambiente=eq.{_ambiente()}&ativo=eq.true"
                      f"&pagbank_card_id={'like' if prov == 'mercadopago' else 'not.like'}.MP:*"
                      "&select=id,bandeira,final,validade,pagbank_card_id&order=usado_em.desc.nullslast,id.desc&limit=5") or []
    for s in salvos:              # MP: o celular gera o token do salvo com o id do cartão (o customer fica aqui)
        cid_cartao = s.pop("pagbank_card_id", "") or ""
        if prov == "mercadopago":
            s["ref"] = cid_cartao.split(":")[-1]
    mp = prov == "mercadopago"
    return jsonify({"ok": True, "ativo": True, "provedor": prov, "ambiente": _ambiente(),
                    "env_sdk": "PROD" if _ambiente() == "producao" else "SANDBOX",
                    "sdk_url": MP_SDK_URL if mp else SDK_URL, "public_key": _mp_public_key() if mp else _public_key(),
                    "max_parcelas": _max_parcelas(), "parcela_min": _parcela_min(), "salvos": salvos,
                    "carteiras": {} if mp else _carteiras(), "abastecimento": _abast_ligado(emp)}), 200, SEM_CACHE


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
    prov = _provedor(a["emp"])
    bin6 = _so_digitos(corpo.get("bin"))[:6]
    if not bin6 and corpo.get("cartao_salvo_id"):
        s = _cartao_salvo(corpo.get("cartao_salvo_id"), cli, prov)
        bin6 = (s or {}).get("primeiros") or ""
    if len(bin6) != 6:
        return jsonify({"erro": "Informe os 6 primeiros números do cartão."}), 400
    planos, motivo = _planos(a["valor"], bin6, prov)
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


def _cartao_salvo(sid, cli, prov="pagbank"):
    try:
        n = int(sid)
    except (TypeError, ValueError):
        return None
    rows = cp._sget(f"oct_cartoes_salvos?id=eq.{n}&cpf_app=eq.{cli['cpf']}&ambiente=eq.{_ambiente()}&ativo=eq.true&limit=1")
    s = rows[0] if rows else None
    if s and str(s.get("pagbank_card_id") or "").startswith("MP:") != (prov == "mercadopago"):
        return None                                  # cartão salvo no outro provedor não serve aqui
    return s


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
                  "&order=id.desc&limit=1&select=id,status,origem,pagbank_order_id")
    desafio = None
    if ja and ja[0]["status"] == "analise" and ja[0].get("origem") == MP_ORIGEM and ja[0].get("pagbank_order_id"):
        desafio = _mp_atualizar(ja[0], cli)        # MP: desafio abandonado expira em 40 min e não pode travar o título
    if ja and ja[0]["status"] in ("paga", "analise", "pedido"):
        out = {"erro": "Este título já tem um pagamento em andamento.", "id": ja[0]["id"], "status": ja[0]["status"]}
        if desafio:
            out["desafio"] = desafio
        return jsonify(out), 409

    prov = _provedor(a["emp"])
    if prov == "mercadopago" and corpo.get("carteira"):
        return jsonify({"erro": "Esta carteira ainda não está habilitada. Use o cartão."}), 503
    carteira, err = _carteira_do_pedido(corpo)
    if err:
        return err
    mp_token = ""
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
        salvo = _cartao_salvo(corpo.get("cartao_salvo_id"), cli, prov) if corpo.get("cartao_salvo_id") else None
        if corpo.get("cartao_salvo_id") and not salvo:
            return jsonify({"erro": "Cartão salvo não encontrado. Escolha outro cartão."}), 404
        encrypted = str(corpo.get("encrypted") or "").strip()
        mp_token = str(corpo.get("token") or "").strip()[:200]       # MP: token do SDK (cartão novo ou salvo + CVV)
        if (prov == "mercadopago" and not mp_token) or (prov != "mercadopago" and not salvo and not encrypted):
            return jsonify({"erro": "Informe o cartão."}), 400
        bin6 = (salvo or {}).get("primeiros") or _so_digitos(corpo.get("bin"))[:6]
        planos, _m = _planos(a["valor"], bin6, prov) if len(bin6 or "") == 6 else (None, None)
        plano = next((p for p in planos or [] if p["parcelas"] == n_parc), None)
        if not plano:
            return jsonify({"erro": "Parcelamento indisponível para este valor/cartão. Escolha outra opção."}), 422

    # 1) registra ANTES de cobrar (se a resposta do PagBank se perder, a linha fica e dá para conferir)
    origem = ("app-" + carteira["tipo"].lower().replace("_", "-") if carteira     # app-google-pay | app-apple-pay
              else MP_ORIGEM if prov == "mercadopago" else "app")
    nova = cp._spost("oct_cartao_cobrancas", dict(a["campo"], empresa_id=a["emp"], cliente_id=a["cliente"], cpf_app=cli["cpf"],
                                                  valor=a["valor"], parcelas=n_parc, juros_cliente=plano["juros"],
                                                  valor_cobrado=plano["total"], status="pedido", ambiente=_ambiente(),
                                                  cartao_salvo_id=(salvo or {}).get("id"), origem=origem))
    cid = (nova or [{}])[0].get("id")
    if prov == "mercadopago":
        return _mp_pagar(cid, a, cli, corpo, plano, n_parc, salvo, mp_token)

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


def _mp_pagar(cid, a, cli, corpo, plano, n_parc, salvo, token):
    """Passo 2 do /pagar no Mercado Pago (a cobrança já está registrada como "pedido")."""
    try:
        st, j = _mp_pedido(cid, a, cli, corpo, plano, n_parc, salvo, token)
    except rq.RequestException as e:
        cp._spatch(f"oct_cartao_cobrancas?id=eq.{cid}", {"status": "erro", "erro": f"sem resposta do Mercado Pago: {e}"[:300]})
        return jsonify({"erro": "Não consegui falar com o Mercado Pago. Confira no app do seu banco antes de tentar de novo.", "id": cid}), 502
    if not j.get("id") or not (j.get("transactions") or {}).get("payments"):
        msg = json.dumps(j.get("errors") or j, ensure_ascii=False)[:300]
        cp._spatch(f"oct_cartao_cobrancas?id=eq.{cid}", {"status": "erro", "erro": f"HTTP {st}: {msg}"})
        _log(cli, f"cartao {cid} MP ERRO {st} {msg[:120]}")
        return jsonify({"erro": "Pagamento não realizado. Confira os dados do cartão ou use outra forma.", "id": cid}), 422
    final = (salvo or {}).get("final") or _so_digitos(corpo.get("final"))[-4:] or None
    salvar = token if (bool(corpo.get("salvar")) and not salvo) else None
    status, upd, baixa = _mp_gravar(cid, j, cli, salvar, final)
    if salvo:
        cp._spatch(f"oct_cartoes_salvos?id=eq.{salvo['id']}", {"usado_em": upd["atualizado_em"]})
    desafio = _mp_desafio(j)
    _log(cli, f"cartao {cid} MP {status} R$ {a['valor']:.2f} {n_parc}x ({a['emp'][:8]}) {upd.get('mensagem') or ''}"
              f"{' 3DS' if desafio else ''} aparelho={'sim' if j.get('_device') else 'nao'}")
    out = {"ok": status == "paga", "id": cid, "status": status, "valor": a["valor"], "parcelas": n_parc,
           "valor_parcela": plano["valor_parcela"], "total": plano["total"], "bandeira": upd.get("bandeira"),
           "final": final, "autorizacao": upd.get("autorizacao"), "baixa": baixa}
    if status == "recusada":
        out["erro"] = ("Pagamento recusado pelo antifraude. Tente outro cartão ou outra forma." if "high_risk" in str(upd.get("mensagem"))
                       else "Pagamento não autorizado na confirmação do banco." if "3ds" in str(upd.get("mensagem"))
                       else "Pagamento recusado pelo banco do cartão. Tente outro cartão ou outra forma.")
    elif desafio:                                  # 3DS: o cliente confirma no banco (tela do MP) e o app acompanha
        out["desafio"] = desafio
        out["aviso"] = "Seu banco pediu uma confirmação."
    elif status == "analise":
        out["aviso"] = "Pagamento em análise. Avisamos aqui assim que o Mercado Pago confirmar."
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
    desafio = None
    if c["status"] == "analise" and c.get("pagbank_order_id") and c.get("origem") == MP_ORIGEM:
        desafio = _mp_atualizar(c, cli)
    elif c["status"] == "analise" and c.get("pagbank_order_id"):
        st, j = _pb("GET", f"/orders/{c['pagbank_order_id']}", timeout=20)
        if st < 400 and j.get("charges"):
            status, _upd, baixa = _gravar_resultado(cid, j["charges"][0], c["pagbank_order_id"], cli, False)
            c.update(status=status, baixa=baixa or c.get("baixa"))
    out = {"ok": True, "id": c["id"], "status": c["status"], "valor": float(c["valor"]), "parcelas": c["parcelas"],
           "total": float(c.get("valor_cobrado") or c["valor"]), "bandeira": c.get("bandeira"),
           "final": c.get("cartao_final"), "baixa": c.get("baixa")}
    if desafio:
        out["desafio"] = desafio
    if c["status"] == "recusada":
        out["erro"] = "O pagamento não foi autorizado. Tente outro cartão ou outra forma."
    return jsonify(out), 200, SEM_CACHE


@bp_cartao.route("/cashback/api/cartao/salvos/<int:sid>", methods=["DELETE"])
def api_cartao_remover(sid):
    cli, erro = _cliente_ou_401()
    if erro:
        return erro
    _marcar_teste(cli)
    cp._spatch(f"oct_cartoes_salvos?id=eq.{sid}&cpf_app=eq.{cli['cpf']}", {"ativo": False})
    return jsonify({"ok": True}), 200, SEM_CACHE
