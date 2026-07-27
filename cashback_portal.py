# -*- coding: utf-8 -*-
"""
cashback_portal.py — Portal do CLIENTE do cashback (QR code no posto).

Fluxo: cliente lê o QR -> 1º acesso CADASTRA (nome, endereço, telefone,
nascimento, CPF, sexo, e-mail, chave Pix + senha) -> depois LOGA (CPF+senha).
Dashboard: cashbacks recebidos + quando pode receber o próximo (janela 2h).
Acionar benefício: seleciona COMBUSTÍVEL > FORMA DE PAGAMENTO -> o PDV do
posto vê o acionamento e baixa o abastecimento com aquela forma, gerando o
cashback automaticamente.

Roda DENTRO do servidor SEFAZ (Flask/Railway): a service_key do Supabase fica
NO SERVIDOR — a página pública não carrega credencial nenhuma.

Tabelas (Supabase):
  oct_cashback_clientes(id, cpf unique, nome, endereco, telefone, nascimento,
    sexo, email, chave_pix, senha_hash, empresa_origem, criado_em)
  oct_cashback_acionamentos(id, empresa_id, cliente_cpf, cliente_nome,
    pessoa_id, combustivel, forma, status[aguardando|usado|expirado|cancelado],
    criado_em, usado_em, venda_numero)

Token de sessão: HMAC-SHA256("cpf|exp") com CHAVE_MESTRA (env) — sem dependências.
"""

import os
import re
import json
import hmac
import base64
import hashlib
import time
from datetime import datetime, timezone, timedelta

import requests as rq
from flask import Blueprint, request, jsonify, Response

bp_cashback = Blueprint("cashback", __name__)

ACIONAMENTO_VALIDADE_MIN = 60      # acionamento vale 1h
JANELA_2H_SEG = 2 * 3600

COMBUSTIVEIS = ["GASOLINA COMUM", "GASOLINA ADITIVADA", "ETANOL", "DIESEL S10", "DIESEL S500"]
FORMAS = [("01", "Dinheiro"), ("17", "PIX")]


# ------------------------------------------------------------------
# Supabase REST (service key do servidor)
# ------------------------------------------------------------------
def _supa():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    return url, key


def _sh(extra=None):
    _, key = _supa()
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _checa(r):
    """raise_for_status mostrando o ERRO REAL do PostgREST (não só o código)."""
    if r.status_code >= 400:
        det = ""
        try:
            det = (r.json() or {}).get("message") or r.text[:180]
        except Exception:
            det = (r.text or "")[:180]
        raise RuntimeError(f"banco {r.status_code}: {det}")
    return r


def _sget(q):
    url, _ = _supa()
    r = _checa(rq.get(f"{url}/rest/v1/{q}", headers=_sh(), timeout=20))
    return r.json()


def _spost(tab, body, prefer="return=representation"):
    url, _ = _supa()
    r = _checa(rq.post(f"{url}/rest/v1/{tab}", headers=_sh({"Prefer": prefer}), json=body, timeout=20))
    return r.json() if r.text.strip() else None


def _spatch(q, body, prefer="return=minimal"):
    url, _ = _supa()
    r = _checa(rq.patch(f"{url}/rest/v1/{q}", headers=_sh({"Prefer": prefer}), json=body, timeout=20))
    return r.json() if (r.text or "").strip() and "representation" in prefer else None


_RE_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _uuid_ok(s):
    """Só aceita UUID de verdade (o QR errado pode mandar '<empresa_id>' literal)."""
    s = str(s or "").strip()
    return s if _RE_UUID.match(s) else None


# ------------------------------------------------------------------
# senha + token
# ------------------------------------------------------------------
def _hash_senha(senha, sal=None):
    sal = sal or base64.b16encode(os.urandom(12)).decode()
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), sal.encode(), 120000)
    return f"pbkdf2${sal}${base64.b16encode(h).decode()}"


def _confere_senha(senha, guardado):
    try:
        _, sal, _ = guardado.split("$", 2)
        return hmac.compare_digest(_hash_senha(senha, sal), guardado)
    except Exception:
        return False


def _segredo():
    return (os.environ.get("CHAVE_MESTRA") or "octano-cashback-dev").encode()


def _token_gerar(cpf):
    exp = int(time.time()) + 30 * 86400   # 30 dias
    corpo = f"{cpf}|{exp}"
    ass = hmac.new(_segredo(), corpo.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{corpo}|{ass}".encode()).decode()


def _token_validar(tok):
    try:
        corpo = base64.urlsafe_b64decode(tok.encode()).decode()
        cpf, exp, ass = corpo.rsplit("|", 2)
        if int(exp) < time.time():
            return None
        esperado = hmac.new(_segredo(), f"{cpf}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        return cpf if hmac.compare_digest(ass, esperado) else None
    except Exception:
        return None


def _cliente_do_token():
    tok = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    cpf = _token_validar(tok) if tok else None
    if not cpf:
        return None
    rows = _sget(f"oct_cashback_clientes?cpf=eq.{cpf}&limit=1")
    return rows[0] if rows else None


# ------------------------------------------------------------------
# validações
# ------------------------------------------------------------------
def _so_digitos(s):
    return re.sub(r"\D", "", str(s or ""))


def _cpf_valido(cpf):
    cpf = _so_digitos(cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for n in (9, 10):
        soma = sum(int(cpf[i]) * ((n + 1) - i) for i in range(n))
        dig = (soma * 10 % 11) % 10
        if dig != int(cpf[n]):
            return False
    return True


# ------------------------------------------------------------------
# APIs
# ------------------------------------------------------------------
@bp_cashback.route("/cashback/api/postos", methods=["GET"])
def api_postos():
    try:
        rows = _sget("oct_empresas?ativo=eq.true&select=id,nome,nome_fantasia&order=nome")
        return jsonify([{"id": r["id"], "nome": r.get("nome_fantasia") or r.get("nome") or "Posto"} for r in rows])
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@bp_cashback.route("/cashback/api/cadastro", methods=["POST"])
def api_cadastro():
    d = request.get_json(silent=True) or {}
    cpf = _so_digitos(d.get("cpf"))
    nome = str(d.get("nome") or "").strip()
    senha = str(d.get("senha") or "")
    chave_pix = str(d.get("chave_pix") or "").strip()
    if not _cpf_valido(cpf):
        return jsonify({"erro": "CPF inválido"}), 400
    if len(nome.split()) < 2:
        return jsonify({"erro": "Informe o nome completo"}), 400
    if len(senha) < 6:
        return jsonify({"erro": "Senha deve ter pelo menos 6 caracteres"}), 400
    if not chave_pix:
        return jsonify({"erro": "Informe sua chave Pix (é onde o cashback cai)"}), 400
    tel = _so_digitos(d.get("telefone"))
    if len(tel) < 10:
        return jsonify({"erro": "Telefone/WhatsApp inválido"}), 400
    posto = _uuid_ok(d.get("posto"))
    try:
        if _sget(f"oct_cashback_clientes?cpf=eq.{cpf}&select=id&limit=1"):
            return jsonify({"erro": "CPF já cadastrado — use 'Entrar' com sua senha"}), 409
        reg = {
            "cpf": cpf, "nome": nome[:120],
            "endereco": str(d.get("endereco") or "").strip()[:160] or None,
            "numero": str(d.get("numero") or "").strip()[:20] or None,
            "bairro": str(d.get("bairro") or "").strip()[:80] or None,
            "cidade": str(d.get("cidade") or "").strip()[:80] or None,
            "uf": str(d.get("uf") or "").strip()[:2].upper() or None,
            "cep": _so_digitos(d.get("cep"))[:8] or None,
            "telefone": tel, "nascimento": (d.get("nascimento") or None),
            "sexo": str(d.get("sexo") or "").strip()[:20] or None,
            "email": str(d.get("email") or "").strip()[:120] or None,
            "chave_pix": chave_pix[:120], "senha_hash": _hash_senha(senha),
            "empresa_origem": posto,
        }
        try:
            _spost("oct_cashback_clientes", reg, prefer="return=minimal")
        except RuntimeError as e:
            # tabela ainda sem alguma coluna opcional (ex.: bairro/cidade) ->
            # grava sem os opcionais em vez de falhar o cadastro
            if "Could not find the" not in str(e):
                raise
            minimo = {k: reg[k] for k in ("cpf", "nome", "endereco", "telefone", "nascimento",
                                          "sexo", "email", "chave_pix", "senha_hash", "empresa_origem")
                      if k in reg}
            _spost("oct_cashback_clientes", minimo, prefer="return=minimal")
        # garante a PESSOA do PDV no posto de origem (elegível ao cashback)
        if posto:
            _garantir_pessoa(posto, reg)
        return jsonify({"ok": True, "token": _token_gerar(cpf), "nome": nome})
    except Exception as e:
        return jsonify({"erro": "falha no cadastro: " + str(e)[:200]}), 500


def _garantir_pessoa(empresa_id, cad):
    """Cria/atualiza o cliente em oct_pessoas do posto (o PDV usa essa tabela
    p/ elegibilidade do cashback: cashback_ativo + chave_pix)."""
    try:
        ex = _sget(f"oct_pessoas?empresa_id=eq.{empresa_id}&documento=eq.{cad['cpf']}&select=id&limit=1")
        corpo = {
            "nome": cad["nome"], "documento": cad["cpf"], "telefone": cad.get("telefone"),
            "whatsapp": cad.get("telefone"), "email": cad.get("email"),
            "chave_pix": cad["chave_pix"], "cashback_ativo": True, "ativo": True,
            "endereco": cad.get("endereco"), "num_endereco": cad.get("numero"),
            "bairro": cad.get("bairro"), "cidade": cad.get("cidade"),
            "cep": cad.get("cep"), "uf": cad.get("uf"),
        }
        if ex:
            _spatch(f"oct_pessoas?id=eq.{ex[0]['id']}", corpo)
            return ex[0]["id"]
        corpo.update({"empresa_id": empresa_id, "tipo": "cliente", "tipo_pessoa": "fisica"})
        novo = _spost("oct_pessoas", corpo)
        return novo[0]["id"] if novo else None
    except Exception:
        return None


@bp_cashback.route("/cashback/api/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or {}
    cpf = _so_digitos(d.get("cpf"))
    try:
        rows = _sget(f"oct_cashback_clientes?cpf=eq.{cpf}&limit=1") if cpf else []
    except Exception as e:
        return jsonify({"erro": "serviço indisponível: " + str(e)[:120]}), 500
    if not rows or not _confere_senha(str(d.get("senha") or ""), rows[0].get("senha_hash") or ""):
        return jsonify({"erro": "CPF ou senha incorretos"}), 401
    return jsonify({"ok": True, "token": _token_gerar(cpf), "nome": rows[0]["nome"]})


@bp_cashback.route("/cashback/api/me", methods=["GET"])
def api_me():
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    try:
        chave = cli.get("chave_pix") or ""
        cbs = _sget("oct_cashback?or=(chave_pix.eq." + rq.utils.quote(chave, safe="")
                    + ",cliente_nome.ilike." + rq.utils.quote("*" + cli["nome"][:25] + "*", safe="") + ")"
                    + "&select=valor_cashback,litros,status,criado_em,pago_em,numero_nfe,empresa_id"
                    + "&order=criado_em.desc&limit=60")
    except Exception:
        cbs = []
    # nomes dos postos
    nomes = {}
    try:
        for e in _sget("oct_empresas?select=id,nome,nome_fantasia"):
            nomes[e["id"]] = e.get("nome_fantasia") or e.get("nome") or "Posto"
    except Exception:
        pass
    # próxima liberação: último cashback VIVO + 2h
    prox = None
    vivos = [c for c in cbs if c.get("status") in ("pendente", "processando", "pago")]
    if vivos:
        try:
            ult = max(datetime.fromisoformat(str(c["criado_em"]).replace("Z", "+00:00")) for c in vivos)
            lib = ult + timedelta(seconds=JANELA_2H_SEG)
            if lib > datetime.now(timezone.utc):
                prox = lib.isoformat()
        except Exception:
            pass
    # acionamento ativo
    corte = (datetime.now(timezone.utc) - timedelta(minutes=ACIONAMENTO_VALIDADE_MIN)).isoformat()
    try:
        ac = _sget(f"oct_cashback_acionamentos?cliente_cpf=eq.{cli['cpf']}&status=eq.aguardando"
                   f"&criado_em=gte.{rq.utils.quote(corte, safe='')}&order=criado_em.desc&limit=1")
    except Exception:
        ac = []
    total_pago = sum(float(c.get("valor_cashback") or 0) for c in cbs if c.get("status") == "pago")
    return jsonify({
        "ok": True, "nome": cli["nome"], "chave_pix": chave, "total_pago": round(total_pago, 2),
        "proxima_liberacao": prox,
        "acionamento": (ac[0] if ac else None),
        "cashbacks": [{
            "valor": c.get("valor_cashback"), "litros": c.get("litros"), "status": c.get("status"),
            "quando": c.get("pago_em") or c.get("criado_em"), "cupom": c.get("numero_nfe"),
            "posto": nomes.get(c.get("empresa_id"), ""),
        } for c in cbs],
    })


@bp_cashback.route("/cashback/api/acionar", methods=["POST"])
def api_acionar():
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    d = request.get_json(silent=True) or {}
    empresa = _uuid_ok(d.get("posto"))
    comb = str(d.get("combustivel") or "").strip().upper()
    forma = str(d.get("forma") or "").strip()
    bico = None
    try:
        bico = int(str(d.get("bico") or "").strip() or 0) or None
    except ValueError:
        bico = None
    if not empresa:
        return jsonify({"erro": "posto não identificado — abra o portal lendo o QR code do posto"}), 400
    if forma not in [f[0] for f in FORMAS]:
        return jsonify({"erro": "forma de pagamento inválida"}), 400
    # janela 2h: se recebeu (ou tem pendente) há menos de 2h, não deixa acionar
    corte2h = (datetime.now(timezone.utc) - timedelta(seconds=JANELA_2H_SEG)).isoformat()
    try:
        rec = _sget("oct_cashback?chave_pix=eq." + rq.utils.quote(cli.get("chave_pix") or "", safe="")
                    + "&status=in.(pendente,processando,pago)"
                    + f"&criado_em=gte.{rq.utils.quote(corte2h, safe='')}&select=criado_em&limit=1")
        if rec:
            lib = datetime.fromisoformat(str(rec[0]["criado_em"]).replace("Z", "+00:00")) + timedelta(seconds=JANELA_2H_SEG)
            return jsonify({"erro": "Você já recebeu cashback nas últimas 2 horas.",
                            "proxima_liberacao": lib.isoformat()}), 429
    except Exception:
        pass
    try:
        # expira acionamentos antigos ainda aguardando
        _spatch(f"oct_cashback_acionamentos?cliente_cpf=eq.{cli['cpf']}&status=eq.aguardando",
                {"status": "expirado"})
        pessoa_id = _garantir_pessoa(empresa, {
            "cpf": cli["cpf"], "nome": cli["nome"], "telefone": cli.get("telefone"),
            "email": cli.get("email"), "chave_pix": cli.get("chave_pix"),
        })
        reg = {
            "empresa_id": empresa, "cliente_cpf": cli["cpf"], "cliente_nome": cli["nome"],
            "pessoa_id": pessoa_id, "combustivel": comb or None, "forma": forma,
            "status": "aguardando",
        }
        if bico:
            reg["bico"] = bico
        try:
            novo = _spost("oct_cashback_acionamentos", reg)
        except RuntimeError as e:
            if "Could not find the" not in str(e):
                raise
            reg.pop("bico", None)   # tabela ainda sem a coluna bico
            novo = _spost("oct_cashback_acionamentos", reg)
        return jsonify({"ok": True, "acionamento": (novo[0] if novo else None),
                        "validade_min": ACIONAMENTO_VALIDADE_MIN})
    except Exception as e:
        return jsonify({"erro": "falha ao acionar: " + str(e)[:200]}), 500


@bp_cashback.route("/cashback/api/bico", methods=["GET"])
def api_bico():
    """Ficha do bico p/ o portal: combustível (e preço, se houver um recente).
    Fonte: últimos abastecimentos do bico na nuvem."""
    posto = _uuid_ok(request.args.get("posto"))
    try:
        bico = int(request.args.get("bico") or 0)
    except ValueError:
        bico = 0
    if not posto or not bico:
        return jsonify({"ok": False, "erro": "posto/bico inválidos"}), 400
    try:
        rows = _sget(f"oct_pdv_abastecimentos?empresa_id=eq.{posto}&bico=eq.{bico}"
                     f"&select=combustivel,preco_litro,data_abast&order=data_abast.desc&limit=30")
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)[:120]}), 500
    comb_bruto = next((r["combustivel"] for r in rows if r.get("combustivel")), None)
    preco = None
    corte_preco = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    for r in rows:
        if (r.get("preco_litro") or 0) > 0 and str(r.get("data_abast") or "") >= corte_preco:
            preco = float(r["preco_litro"])
            break
    # normaliza pro nome padrão da lista do portal
    comb = None
    if comb_bruto:
        s = comb_bruto.upper()
        if "ADT" in s or "ADIT" in s:
            comb = "GASOLINA ADITIVADA"
        elif "GASOLINA" in s:
            comb = "GASOLINA COMUM"
        elif "ETANOL" in s or "ALCOOL" in s or "ÁLCOOL" in s:
            comb = "ETANOL"
        elif "S10" in s or "S-10" in s:
            comb = "DIESEL S10"
        elif "DIESEL" in s:
            comb = "DIESEL S500"
    return jsonify({"ok": True, "bico": bico, "combustivel": comb,
                    "combustivel_bruto": comb_bruto, "preco_litro": preco})


@bp_cashback.route("/cashback/api/acionamento/live", methods=["GET"])
def api_acionamento_live():
    """Espelho do abastecimento p/ o acionamento mais recente do cliente.
    Fases: aguardando_inicio -> abastecendo (volume ao vivo, oct_bico_live
    publicado pelo núcleo) -> concluido (litros/valor do abastecimento) ->
    usado (venda emitida) + cashback (pendente/pago)."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    corte = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    try:
        acs = _sget(f"oct_cashback_acionamentos?cliente_cpf=eq.{cli['cpf']}"
                    f"&criado_em=gte.{rq.utils.quote(corte, safe='')}"
                    f"&order=criado_em.desc&limit=1")
    except Exception:
        acs = []
    if not acs:
        return jsonify({"ok": True, "fase": "sem_acionamento"})
    ac = acs[0]
    resp = {"ok": True, "acionamento": {k: ac.get(k) for k in
            ("id", "status", "bico", "combustivel", "forma", "criado_em")}}
    bico = ac.get("bico")

    # cashback gerado depois do acionamento? (fase final)
    try:
        cbs = _sget("oct_cashback?chave_pix=eq." + rq.utils.quote(cli.get("chave_pix") or "", safe="")
                    + f"&criado_em=gte.{rq.utils.quote(ac['criado_em'], safe='')}"
                    + "&select=valor_cashback,litros,status,pago_em&order=criado_em.desc&limit=1")
    except Exception:
        cbs = []
    if cbs:
        resp["fase"] = "cashback"
        resp["cashback"] = cbs[0]
        return jsonify(resp)

    # abastecimento concluído no bico após o acionamento?
    if bico:
        try:
            abs_ = _sget(f"oct_pdv_abastecimentos?empresa_id=eq.{ac['empresa_id']}&bico=eq.{bico}"
                         f"&data_abast=gte.{rq.utils.quote(ac['criado_em'], safe='')}"
                         f"&select=litros,valor,preco_litro,produto_nome,data_abast,status"
                         f"&order=data_abast.desc&limit=1")
        except Exception:
            abs_ = []
        if abs_:
            resp["fase"] = "concluido" if ac.get("status") == "aguardando" else "usado"
            resp["abastecimento"] = abs_[0]
            return jsonify(resp)
        # ao vivo: bico publicado pelo núcleo há menos de 20s?
        try:
            live = _sget(f"oct_bico_live?empresa_id=eq.{ac['empresa_id']}&bico=eq.{bico}&limit=1")
        except Exception:
            live = []
        if live:
            lv = live[0]
            try:
                idade = (datetime.now(timezone.utc)
                         - datetime.fromisoformat(str(lv["atualizado_em"]).replace("Z", "+00:00"))).total_seconds()
            except Exception:
                idade = 999
            if idade < 20 and lv.get("estado") in ("abastecendo", "aguardando"):
                resp["fase"] = "abastecendo"
                resp["live"] = {"estado": lv.get("estado"), "volume": lv.get("volume"),
                                "valor": lv.get("valor"), "combustivel": lv.get("combustivel")}
                return jsonify(resp)
    resp["fase"] = "aguardando_inicio" if ac.get("status") == "aguardando" else ac.get("status")
    return jsonify(resp)


@bp_cashback.route("/cashback/api/acionar/cancelar", methods=["POST"])
def api_acionar_cancelar():
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    _spatch(f"oct_cashback_acionamentos?cliente_cpf=eq.{cli['cpf']}&status=eq.aguardando",
            {"status": "cancelado"})
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# QR code do posto (imprimir e colar na bomba/loja)
# ------------------------------------------------------------------
@bp_cashback.route("/cashback/qr", methods=["GET"])
def qr_posto():
    import io
    import qrcode
    p = (request.args.get("p") or "").strip()
    bico = (request.args.get("bico") or "").strip()
    # atrás do proxy do Railway o host_url vem http:// — força https (só local fica http)
    base_url = request.host_url.rstrip("/")
    if "localhost" not in base_url and "127.0.0.1" not in base_url:
        base_url = base_url.replace("http://", "https://")
    alvo = base_url + "/cashback"
    if p:
        alvo += f"?p={p}" + (f"&bico={bico}" if bico else "")
    img = qrcode.make(alvo, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png")


# ------------------------------------------------------------------
# PWA: manifest + service worker + ícone (vira "app" na tela inicial)
# ------------------------------------------------------------------
@bp_cashback.route("/cashback/manifest.json", methods=["GET"])
def manifest():
    return jsonify({
        "name": "Cashback do Posto",
        "short_name": "Cashback",
        "description": "Abasteça e receba dinheiro de volta no seu Pix",
        "start_url": "/cashback",
        "scope": "/cashback",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0b0d14",
        "theme_color": "#f97316",
        "icons": [
            {"src": "/cashback/icone.png?t=192", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/cashback/icone.png?t=512", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })


@bp_cashback.route("/cashback/icone.png", methods=["GET"])
def icone():
    """Ícone do app gerado na hora (gota de combustível + cifrão), sem arquivo."""
    import io
    from PIL import Image, ImageDraw
    try:
        tam = int(request.args.get("t") or 192)
    except ValueError:
        tam = 192
    tam = 512 if tam > 256 else 192
    img = Image.new("RGB", (tam, tam), "#f97316")
    dr = ImageDraw.Draw(img)
    m = tam / 192.0   # escala
    # gota (triângulo + círculo) branca
    cx, topo, raio = tam / 2, 34 * m, 52 * m
    cy = tam - 62 * m - raio / 2
    dr.polygon([(cx, topo), (cx - raio, cy), (cx + raio, cy)], fill="white")
    dr.ellipse([cx - raio, cy - raio * 0.9, cx + raio, cy + raio * 1.1], fill="white")
    # cifrão laranja dentro da gota (traços simples)
    e = 10 * m
    dr.line([(cx, cy - raio * 0.55), (cx, cy + raio * 0.75)], fill="#f97316", width=int(e * 0.8))
    dr.arc([cx - raio * 0.45, cy - raio * 0.5, cx + raio * 0.45, cy + raio * 0.1],
           start=90, end=340, fill="#f97316", width=int(e * 0.7))
    dr.arc([cx - raio * 0.45, cy - raio * 0.05, cx + raio * 0.45, cy + raio * 0.55],
           start=270, end=160, fill="#f97316", width=int(e * 0.7))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@bp_cashback.route("/cashback/jsqr.js", methods=["GET"])
def jsqr_local():
    """jsQR servido do PRÓPRIO servidor (CDN externo pode ser bloqueado
    por operadora/DNS — era uma das causas de 'não lê a foto')."""
    import os as _os
    caminho = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "jsqr.min.js")
    try:
        with open(caminho, "rb") as f:
            return Response(f.read(), mimetype="application/javascript",
                            headers={"Cache-Control": "public, max-age=86400"})
    except Exception:
        return Response("// jsqr indisponivel", mimetype="application/javascript", status=404)


@bp_cashback.route("/cashback/sw.js", methods=["GET"])
def service_worker():
    # network-first (dados sempre frescos); casca offline básica
    sw = """
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(clients.claim()));
self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    fetch(e.request).then(r => {
      if (e.request.url.includes('/cashback') && r.ok) {
        const cp = r.clone();
        caches.open('cb-v1').then(c => c.put(e.request, cp));
      }
      return r;
    }).catch(() => caches.match(e.request))
  );
});
"""
    return Response(sw, mimetype="application/javascript",
                    headers={"Service-Worker-Allowed": "/cashback"})


# ------------------------------------------------------------------
# Página do cliente (mobile-first, um arquivo, sem credencial)
# ------------------------------------------------------------------
@bp_cashback.route("/cashback", methods=["GET"])
def pagina():
    return Response(PAGINA_HTML, mimetype="text/html; charset=utf-8")


PAGINA_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cashback do Posto</title>
<link rel="manifest" href="/cashback/manifest.json">
<meta name="theme-color" content="#f97316">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Cashback">
<link rel="apple-touch-icon" href="/cashback/icone.png?t=192">
<link rel="icon" type="image/png" href="/cashback/icone.png?t=192">
<style>
  :root{--lar:#f97316;--ok:#16a34a;--fundo:#0b0d14;--card:#131722;--borda:#232838;--txt:#e5e9f0;--mut:#8b93a5}
  *{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
  body{background:var(--fundo);color:var(--txt);min-height:100vh;display:flex;justify-content:center}
  .app{width:100%;max-width:440px;padding:18px 16px 40px}
  h1{font-size:1.25rem;color:var(--lar);display:flex;align-items:center;gap:8px;margin-bottom:2px}
  .sub{color:var(--mut);font-size:.8rem;margin-bottom:18px}
  .card{background:var(--card);border:1px solid var(--borda);border-radius:12px;padding:16px;margin-bottom:14px}
  label{display:block;color:var(--mut);font-size:.74rem;margin:10px 0 4px;text-transform:uppercase;letter-spacing:.4px}
  input,select{width:100%;padding:11px 12px;border-radius:8px;border:1px solid var(--borda);background:#0d1017;color:var(--txt);font-size:1rem}
  button{width:100%;padding:13px;border-radius:9px;border:none;background:var(--lar);color:#fff;font-weight:700;font-size:1rem;cursor:pointer;margin-top:14px}
  button.sec{background:transparent;border:1px solid var(--borda);color:var(--mut);font-weight:400}
  .msg{margin-top:10px;font-size:.85rem;text-align:center;min-height:1.2em}
  .ok{color:#4ade80}.erro{color:#f87171}
  .grande{font-size:1.9rem;font-weight:800;color:#4ade80}
  .lista{margin-top:8px}
  .item{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid #1a1f2c;font-size:.86rem}
  .tag{font-size:.66rem;padding:2px 7px;border-radius:99px;font-weight:700}
  .t-pago{background:#052e16;color:#4ade80}.t-pendente{background:#2a2007;color:#fbbf24}
  .t-outros{background:#1f2433;color:#8b93a5}
  .esc{display:none}
  .aviso{background:#101a2c;border:1px solid #1e3a5f;border-radius:9px;padding:10px 12px;font-size:.8rem;color:#93c5fd;margin-top:10px}
  .cta{background:#052e16;border:1px solid #14532d;border-radius:9px;padding:12px;margin-top:10px;font-size:.86rem;color:#86efac}
  a{color:var(--lar);text-decoration:none}
</style></head><body><div class="app">

<h1>⛽ Cashback do Posto</h1>
<div class="sub" id="nome-posto">Abasteça e receba dinheiro de volta no seu Pix</div>

<!-- LOGIN -->
<div class="card" id="tela-login">
  <label>CPF</label><input id="lg-cpf" inputmode="numeric" placeholder="000.000.000-00" maxlength="14">
  <label>Senha</label><input id="lg-senha" type="password" placeholder="Sua senha">
  <button onclick="fazerLogin()">Entrar</button>
  <button class="sec" onclick="mostrar('tela-cad')">Primeiro acesso? Cadastre-se</button>
  <div class="msg" id="lg-msg"></div>
</div>

<!-- CADASTRO -->
<div class="card esc" id="tela-cad">
  <div style="font-weight:700;margin-bottom:4px">Criar minha conta</div>
  <div class="sub">O cashback cai direto na sua chave Pix.</div>
  <label>Nome completo *</label><input id="cd-nome" placeholder="Como no documento">
  <label>CPF *</label><input id="cd-cpf" inputmode="numeric" placeholder="000.000.000-00" maxlength="14">
  <label>Data de nascimento</label><input id="cd-nasc" type="date">
  <label>Sexo</label><select id="cd-sexo"><option value="">Prefiro não informar</option><option>Feminino</option><option>Masculino</option><option>Outro</option></select>
  <label>Celular / WhatsApp *</label><input id="cd-tel" inputmode="numeric" placeholder="(31) 9 9999-9999">
  <label>E-mail</label><input id="cd-email" type="email" placeholder="voce@email.com">
  <div style="display:flex;gap:8px">
    <div style="flex:1"><label>CEP</label><input id="cd-cep" inputmode="numeric" placeholder="00000-000" maxlength="9"></div>
    <div style="flex:1.6"><label>Cidade</label><input id="cd-cidade" placeholder="Cidade"></div>
    <div style="width:64px"><label>UF</label><input id="cd-uf" maxlength="2" placeholder="MG"></div>
  </div>
  <div class="sub" id="cd-cep-msg" style="margin:4px 0 0"></div>
  <div style="display:flex;gap:8px">
    <div style="flex:2.4"><label>Endereço (rua/avenida)</label><input id="cd-end" placeholder="Rua / Avenida"></div>
    <div style="flex:1"><label>Número</label><input id="cd-num" inputmode="numeric" placeholder="nº"></div>
  </div>
  <label>Bairro</label><input id="cd-bairro" placeholder="Bairro">
  <label>Chave Pix (onde o dinheiro cai) *</label><input id="cd-pix" placeholder="CPF, celular, e-mail ou aleatória">
  <label>Senha (mín. 6) *</label><input id="cd-senha" type="password">
  <label>Repita a senha *</label><input id="cd-senha2" type="password">
  <button onclick="fazerCadastro()">Cadastrar e entrar</button>
  <button class="sec" onclick="mostrar('tela-login')">Já tenho conta</button>
  <div class="msg" id="cd-msg"></div>
</div>

<!-- DASHBOARD -->
<div class="esc" id="tela-dash">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div><div class="sub" style="margin:0">Olá,</div><div style="font-weight:700" id="dh-nome">—</div></div>
      <button class="sec" style="width:auto;padding:7px 12px;margin:0" onclick="sair()">Sair</button>
    </div>
    <div style="margin-top:14px" class="sub">Total já recebido</div>
    <div class="grande" id="dh-total">R$ 0,00</div>
    <div id="dh-prox" class="aviso esc"></div>
  </div>

  <div class="card">
    <div style="font-weight:700">🎁 Usar meu cashback agora</div>
    <div class="sub">Escolha antes de abastecer — o caixa já vai saber.</div>
    <div id="ac-form">
      <div id="ac-posto-box" class="esc">
        <label>Posto</label>
        <select id="ac-posto"><option value="">Carregando postos…</option></select>
      </div>
      <label>Bico (número na bomba)</label>
      <div style="display:flex;gap:8px">
        <input id="ac-bico" inputmode="numeric" maxlength="3" placeholder="digite o nº do bico" style="flex:1">
        <button onclick="abrirScanner()" style="width:auto;margin:0;padding:0 16px;white-space:nowrap">📷 Escanear</button>
      </div>
      <div id="ac-bico-info" class="sub" style="margin:6px 0 0;min-height:1.1em"></div>
      <label>Combustível</label>
      <select id="ac-comb"></select>
      <label>Forma de pagamento</label>
      <select id="ac-forma"><option value="01">Dinheiro</option><option value="17">PIX</option></select>
      <button onclick="acionar()">Acionar benefício</button>
    </div>
    <div id="ac-ativo" class="cta esc"></div>
    <div id="ac-live" class="esc" style="margin-top:10px;background:#0d1017;border:1px solid #232838;border-radius:10px;padding:14px;text-align:center">
      <div class="sub" id="lv-fase">—</div>
      <div style="font-size:2.4rem;font-weight:800;color:#4ade80;font-variant-numeric:tabular-nums" id="lv-num">—</div>
      <div class="sub" id="lv-det"></div>
    </div>
    <div class="msg" id="ac-msg"></div>
  </div>

  <div class="card">
    <div style="font-weight:700;margin-bottom:6px">📜 Meus cashbacks</div>
    <div class="lista" id="dh-lista"><div class="sub">Carregando…</div></div>
  </div>

  <!-- SCANNER de QR (câmera) -->
  <!-- display controlado SÓ por style.display (inline display:flex vencia a
       classe .esc e a tela nascia ABERTA por cima de tudo — era ESSE o travamento) -->
  <div id="scan-overlay" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.92);z-index:9999;flex-direction:column;align-items:center;justify-content:center;padding:16px">
    <div style="color:#fff;font-weight:700;margin-bottom:10px">Aponte para o QR do bico</div>
    <video id="scan-video" playsinline muted style="width:100%;max-width:400px;border-radius:12px;border:2px solid #f97316"></video>
    <div id="scan-msg" class="sub" style="margin-top:10px;text-align:center">Abrindo a câmera…</div>
    <button type="button" onclick="scanPorFoto()" style="max-width:400px;background:#16a34a">📸 Tirar FOTO do QR (câmera do celular)</button>
    <button type="button" onclick="fecharScanner()" style="max-width:400px;background:#2a2d3e">Cancelar e digitar o número</button>
    <input id="scan-foto" type="file" accept="image/*" capture="environment" style="display:none">
  </div>
  <div id="js-erro" style="display:none;position:fixed;bottom:0;left:0;right:0;background:#7f1d1d;color:#fecaca;font-size:.72rem;padding:8px 12px;z-index:99999;word-break:break-all"></div>

  <div class="card esc" id="pwa-card">
    <div style="font-weight:700">📲 Vire um app no seu celular</div>
    <div class="sub" id="pwa-txt">Instale para abrir direto da tela inicial, como um aplicativo.</div>
    <button id="pwa-btn" class="esc" onclick="pwaInstalar()">Instalar o app</button>
  </div>
</div>

<div class="sub" style="text-align:center;margin-top:14px;opacity:.45">versão bico-info-7</div>

<script>
// qualquer erro de JS aparece na tela (diagnóstico remoto: o cliente manda o texto)
window.onerror = function (m, src, lin, col) {
  try {
    var d = document.getElementById("js-erro");
    d.style.display = "block";
    d.textContent = "⚠ erro: " + m + " @" + (src || "").split("/").pop() + ":" + lin + ":" + col;
  } catch (e) {}
};
window.addEventListener("unhandledrejection", function (ev) {
  try {
    var d = document.getElementById("js-erro");
    d.style.display = "block";
    d.textContent = "⚠ promessa: " + (ev.reason && (ev.reason.name + " " + ev.reason.message) || ev.reason);
  } catch (e) {}
});
const API = "";
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
let POSTO = new URLSearchParams(location.search).get("p") || localStorage.getItem("cb_posto") || "";
if (!UUID_RE.test(POSTO)) { POSTO = ""; localStorage.removeItem("cb_posto"); }
if (POSTO) localStorage.setItem("cb_posto", POSTO);
const COMBS = ["GASOLINA COMUM","GASOLINA ADITIVADA","ETANOL","DIESEL S10","DIESEL S500"];
document.getElementById("ac-comb").innerHTML = COMBS.map(c=>`<option>${c}</option>`).join("");
const BICO_URL = (new URLSearchParams(location.search).get("bico")||"").replace(/\D/g,"");
if (BICO_URL) {
  document.getElementById("ac-bico").value = BICO_URL;
  setTimeout(()=>{ try{ carregarInfoBico(); }catch(e){} }, 400);   // ficha do bico do QR
}

function mostrar(id){["tela-login","tela-cad","tela-dash"].forEach(t=>document.getElementById(t).classList.toggle("esc",t!==id));}
function tok(){return localStorage.getItem("cb_token")||"";}
function brl(v){return "R$ "+Number(v||0).toLocaleString("pt-BR",{minimumFractionDigits:2});}
function mascaraCpf(el){el.addEventListener("input",()=>{let v=el.value.replace(/\D/g,"").slice(0,11);el.value=v.replace(/(\d{3})(\d)/,"$1.$2").replace(/(\d{3})(\d)/,"$1.$2").replace(/(\d{3})(\d{1,2})$/,"$1-$2");});}
mascaraCpf(document.getElementById("lg-cpf"));mascaraCpf(document.getElementById("cd-cpf"));

// ---- CEP: máscara + autopreenchimento (ViaCEP) ----
(function(){
  const cep=document.getElementById("cd-cep"),msg=document.getElementById("cd-cep-msg");
  cep.addEventListener("input",()=>{let v=cep.value.replace(/\D/g,"").slice(0,8);cep.value=v.replace(/(\d{5})(\d)/,"$1-$2");
    if(v.length===8)buscarCep(v);});
  async function buscarCep(v){
    msg.textContent="Buscando CEP…";
    try{
      const r=await fetch("https://viacep.com.br/ws/"+v+"/json/").then(x=>x.json());
      if(r.erro){msg.textContent="CEP não encontrado — preencha o endereço manualmente.";return;}
      document.getElementById("cd-end").value=r.logradouro||"";
      document.getElementById("cd-bairro").value=r.bairro||"";
      document.getElementById("cd-cidade").value=r.localidade||"";
      document.getElementById("cd-uf").value=r.uf||"";
      msg.textContent="✓ Endereço preenchido — confira e informe o número.";
      document.getElementById("cd-num").focus();
    }catch(e){msg.textContent="Não consegui consultar o CEP — preencha manualmente.";}
  }
})();

async function req(caminho,corpo,metodo){
  const r = await fetch(API+caminho,{method:metodo||(corpo?"POST":"GET"),
    headers:{"Content-Type":"application/json","Authorization":"Bearer "+tok()},
    body:corpo?JSON.stringify(corpo):undefined});
  return r.json().then(j=>({http:r.status,...j}));
}

async function fazerLogin(){
  const m=document.getElementById("lg-msg");m.className="msg";m.textContent="Entrando…";
  const r=await req("/cashback/api/login",{cpf:document.getElementById("lg-cpf").value,senha:document.getElementById("lg-senha").value});
  if(r.token){localStorage.setItem("cb_token",r.token);carregarDash();}
  else{m.className="msg erro";m.textContent=r.erro||"Falha no login";}
}

async function fazerCadastro(){
  const m=document.getElementById("cd-msg");m.className="msg";
  const s1=document.getElementById("cd-senha").value,s2=document.getElementById("cd-senha2").value;
  if(s1!==s2){m.className="msg erro";m.textContent="As senhas não conferem";return;}
  m.textContent="Cadastrando…";
  const r=await req("/cashback/api/cadastro",{
    nome:document.getElementById("cd-nome").value,cpf:document.getElementById("cd-cpf").value,
    nascimento:document.getElementById("cd-nasc").value||null,sexo:document.getElementById("cd-sexo").value,
    telefone:document.getElementById("cd-tel").value,email:document.getElementById("cd-email").value,
    cep:document.getElementById("cd-cep").value,endereco:document.getElementById("cd-end").value,
    numero:document.getElementById("cd-num").value,bairro:document.getElementById("cd-bairro").value,
    cidade:document.getElementById("cd-cidade").value,uf:document.getElementById("cd-uf").value,
    chave_pix:document.getElementById("cd-pix").value,
    senha:s1,posto:POSTO||null});
  if(r.token){localStorage.setItem("cb_token",r.token);carregarDash();}
  else{m.className="msg erro";m.textContent=r.erro||"Falha no cadastro";}
}

function sair(){localStorage.removeItem("cb_token");mostrar("tela-login");}

async function carregarDash(){
  const r=await req("/cashback/api/me");
  if(!r.ok){sair();return;}
  mostrar("tela-dash");
  document.getElementById("dh-nome").textContent=r.nome.split(" ")[0];
  document.getElementById("dh-total").textContent=brl(r.total_pago);
  const prox=document.getElementById("dh-prox");
  if(r.proxima_liberacao){
    const d=new Date(r.proxima_liberacao);
    prox.classList.remove("esc");
    prox.textContent="⏳ Próximo cashback liberado às "+d.toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit"})+" ("+d.toLocaleDateString("pt-BR")+")";
  } else prox.classList.add("esc");
  const at=document.getElementById("ac-ativo"),fm=document.getElementById("ac-form");
  if(r.acionamento){
    fm.classList.add("esc");at.classList.remove("esc");
    at.innerHTML="✅ <b>Benefício acionado!</b><br>"+
      (r.acionamento.bico?("Bico "+r.acionamento.bico+" · "):"")+
      (r.acionamento.combustivel||"Combustível") + " · " +
      (r.acionamento.forma==="17"?"PIX":"Dinheiro") +
      "<br>Vá até a bomba e abasteça — acompanhe abaixo.<br><br><a href='#' onclick='cancelarAcionamento();return false'>cancelar</a>";
    ligarEspelho();
  } else {fm.classList.remove("esc");at.classList.add("esc");garantirPosto();}
  const lst=document.getElementById("dh-lista");
  if(!(r.cashbacks||[]).length){lst.innerHTML='<div class="sub">Nenhum cashback ainda — abasteça para começar! 🚗</div>';}
  else lst.innerHTML=r.cashbacks.map(c=>{
    const cls=c.status==="pago"?"t-pago":(c.status==="pendente"||c.status==="processando")?"t-pendente":"t-outros";
    const rot=c.status==="pago"?"PAGO":(c.status==="pendente"||c.status==="processando")?"A RECEBER":String(c.status||"").toUpperCase();
    const q=c.quando?new Date(c.quando).toLocaleDateString("pt-BR")+" "+new Date(c.quando).toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit"}):"";
    return `<div class="item"><div><b>${brl(c.valor)}</b> <span class="sub">· ${Number(c.litros||0).toFixed(1)} L${c.posto?" · "+c.posto:""}</span><br><span class="sub">${q}</span></div><span class="tag ${cls}">${rot}</span></div>`;
  }).join("");
}

// sem posto identificado (entrou sem QR): mostra o seletor de postos
async function garantirPosto(){
  const box=document.getElementById("ac-posto-box"),sel=document.getElementById("ac-posto");
  if(POSTO){box.classList.add("esc");return;}
  box.classList.remove("esc");
  if(sel.options.length<=1){
    try{
      const ps=await fetch(API+"/cashback/api/postos").then(r=>r.json());
      sel.innerHTML='<option value="">Escolha o posto…</option>'+
        (Array.isArray(ps)?ps:[]).map(p=>`<option value="${p.id}">${p.nome}</option>`).join("");
    }catch(e){sel.innerHTML='<option value="">Falha ao listar postos</option>';}
  }
  sel.onchange=()=>{if(sel.value){POSTO=sel.value;localStorage.setItem("cb_posto",POSTO);}};
}

async function acionar(){
  const m=document.getElementById("ac-msg");m.className="msg";
  const postoSel=document.getElementById("ac-posto");
  const posto=POSTO||(postoSel&&postoSel.value)||"";
  if(!posto){m.className="msg erro";m.textContent="Escolha o POSTO acima (ou leia o QR do bico).";garantirPosto();return;}
  m.textContent="Acionando…";
  const r=await req("/cashback/api/acionar",{posto:posto,bico:document.getElementById("ac-bico").value,
    combustivel:document.getElementById("ac-comb").value,forma:document.getElementById("ac-forma").value});
  if(r.ok){m.textContent="";carregarDash();ligarEspelho();}
  else{m.className="msg erro";m.textContent=r.erro||"Falha ao acionar";
    if(r.proxima_liberacao){const d=new Date(r.proxima_liberacao);m.textContent+=" (libera às "+d.toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit"})+")";}}
}

async function cancelarAcionamento(){await req("/cashback/api/acionar/cancelar",{});desligarEspelho();carregarDash();}

// ---- ESPELHO AO VIVO do abastecimento (fase a fase) ----
let _lvTimer=null;
function ligarEspelho(){ if(_lvTimer)return; _lvTimer=setInterval(_lvTick,2500); _lvTick(); }
function desligarEspelho(){ if(_lvTimer){clearInterval(_lvTimer);_lvTimer=null;}
  document.getElementById("ac-live").classList.add("esc"); }
async function _lvTick(){
  const box=document.getElementById("ac-live"),fase=document.getElementById("lv-fase"),
        num=document.getElementById("lv-num"),det=document.getElementById("lv-det");
  const r=await req("/cashback/api/acionamento/live");
  if(!r.ok||r.fase==="sem_acionamento"){desligarEspelho();return;}
  box.classList.remove("esc");
  const ac=r.acionamento||{};
  if(r.fase==="aguardando_inicio"){
    fase.textContent="⏳ Aguardando o abastecimento no bico "+(ac.bico||"?");
    num.textContent="—";det.textContent="Vá até a bomba e abasteça normalmente.";
  } else if(r.fase==="abastecendo"){
    fase.textContent="⛽ Abastecendo no bico "+(ac.bico||"?");
    const lv=r.live||{};
    if(lv.volume!=null){
      num.textContent=Number(lv.volume).toLocaleString("pt-BR",{minimumFractionDigits:2})+" L";
      det.textContent=(lv.valor!=null?brl(lv.valor)+" · ":"")+(lv.combustivel||"")+" · ao vivo da bomba";
    } else {
      num.textContent=brl(lv.valor!=null?lv.valor:0);
      det.textContent=(lv.combustivel||"")+" · ao vivo da bomba";
    }
  } else if(r.fase==="concluido"){
    const a=r.abastecimento||{};
    fase.textContent="✅ Abastecimento concluído — bico "+(ac.bico||"?");
    num.textContent=brl(a.valor||a.valor_total||0);
    det.textContent=Number(a.litros||0).toFixed(2)+" L de "+(a.produto_nome||"combustível")+
      ". Agora pague no caixa em "+(ac.forma==="17"?"PIX":"Dinheiro")+" 💳";
  } else if(r.fase==="cashback"){
    const c=r.cashback||{};
    fase.textContent=c.status==="pago"?"🎉 Cashback PAGO no seu Pix!":"🕐 Cashback a caminho…";
    num.textContent=brl(c.valor_cashback||0);
    det.textContent=c.status==="pago"?"Confira seu extrato — e obrigado pela preferência!":"Pagamento em processamento (cai em instantes).";
    if(c.status==="pago"){clearInterval(_lvTimer);_lvTimer=null;setTimeout(carregarDash,4000);}
  } else { // usado / expirado / cancelado
    fase.textContent="Acionamento "+r.fase;num.textContent="—";det.textContent="";
    if(r.fase==="expirado"||r.fase==="cancelado")desligarEspelho();
  }
}

// nome do posto no topo
(async()=>{ if(!POSTO) return;
  try{const ps=await req("/cashback/api/postos");const p=(ps||[]).find?null:null;}catch(e){}
  try{const ps=await fetch(API+"/cashback/api/postos").then(r=>r.json());
    const p=(Array.isArray(ps)?ps:[]).find(x=>x.id===POSTO);
    if(p)document.getElementById("nome-posto").textContent=p.nome+" · abasteça e receba de volta no Pix";
  }catch(e){}
})();

if(tok()) carregarDash(); else mostrar("tela-login");

// ---- SCANNER de QR do bico (câmera no próprio portal) ----
// Nativo (BarcodeDetector, Android/Chrome) com fallback jsQR (iPhone/Safari).
// Sempre dá pra cancelar e digitar o número na mão.
let _scanStream=null,_scanTimer=null,_jsqrCarregando=null;
function _scanExtrair(texto){
  // aceita a URL do QR (…/cashback?p=..&bico=N) ou um número puro
  try{ const u=new URL(texto); const b=u.searchParams.get("bico"); const p=u.searchParams.get("p");
    if(p&&UUID_RE.test(p)){POSTO=p;localStorage.setItem("cb_posto",p);}
    if(b)return b.replace(/\D/g,""); }catch(e){}
  const so=String(texto||"").replace(/\D/g,"");
  return (so.length>=1&&so.length<=3)?so:null;
}
function _scanAchou(texto){
  const b=_scanExtrair(texto);
  if(!b)return false;
  document.getElementById("ac-bico").value=b;
  fecharScanner();
  if(navigator.vibrate)navigator.vibrate(80);
  carregarInfoBico();
  return true;
}

// ---- FICHA do bico: ao identificar o bico (QR/foto/digitação), puxa o
// combustível (e preço recente, se houver) e já seleciona no formulário ----
let _infoBicoTimer=null;
async function carregarInfoBico(){
  const bico=(document.getElementById("ac-bico").value||"").replace(/\D/g,"");
  const info=document.getElementById("ac-bico-info");
  if(!bico||!POSTO){info.textContent="";return;}
  info.textContent="Consultando o bico "+bico+"…";
  try{
    const r=await fetch(API+"/cashback/api/bico?posto="+POSTO+"&bico="+bico).then(x=>x.json());
    if(!r.ok||!r.combustivel){info.textContent="Bico "+bico+" sem histórico — confira o combustível abaixo.";return;}
    const sel=document.getElementById("ac-comb");
    if(![...sel.options].some(o=>o.value===r.combustivel||o.text===r.combustivel))
      sel.insertAdjacentHTML("beforeend",`<option>${r.combustivel}</option>`);
    sel.value=r.combustivel;
    info.innerHTML="⛽ <b style='color:#4ade80'>"+r.combustivel+"</b>"+
      (r.preco_litro?(" · ≈ R$ "+Number(r.preco_litro).toLocaleString("pt-BR",{minimumFractionDigits:2})+"/L"):"");
  }catch(e){info.textContent="";}
}
document.getElementById("ac-bico").addEventListener("input",()=>{
  clearTimeout(_infoBicoTimer);
  _infoBicoTimer=setTimeout(carregarInfoBico,500);
});
const EH_IOS=/iphone|ipad|ipod/i.test(navigator.userAgent);
async function abrirScanner(){
  const ov=document.getElementById("scan-overlay"),vid=document.getElementById("scan-video"),msg=document.getElementById("scan-msg");
  ov.style.display="flex";
  if(EH_IOS){
    // iPhone/iPad: a câmera ao vivo do WebKit é problemática (trava em app
    // instalado) — vai DIRETO pra câmera nativa de foto, que o iOS faz bem
    vid.style.display="none";
    msg.textContent="Toque no botão verde: a câmera do iPhone abre, fotografe o QR do bico.";
    return;
  }
  msg.textContent="Abrindo a câmera… (se pedir permissão, toque em PERMITIR)";
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
    msg.textContent="Este navegador não dá acesso à câmera. Use o botão verde (foto) ou digite o número.";return;
  }
  // timeout: alguns navegadores seguram o prompt de permissão indefinidamente
  const comTimeout=(p,ms)=>Promise.race([p,new Promise((_,rej)=>setTimeout(()=>rej(new DOMException("demorou demais","TimeoutError")),ms))]);
  try{
    try{
      _scanStream=await comTimeout(navigator.mediaDevices.getUserMedia({video:{facingMode:{ideal:"environment"}},audio:false}),9000);
    }catch(e1){
      // fallback: qualquer câmera disponível
      _scanStream=await comTimeout(navigator.mediaDevices.getUserMedia({video:true,audio:false}),9000);
    }
    vid.setAttribute("playsinline","");vid.muted=true;
    vid.srcObject=_scanStream;
    try{await vid.play();}catch(e){/* alguns navegadores tocam sozinhos */}
  }catch(e){
    let dica="Digite o número do bico.";
    if(e&&e.name==="NotAllowedError")dica="Permissão negada — toque no cadeado 🔒 da barra do navegador > Permissões > Câmera > Permitir, e tente de novo. Ou digite o número.";
    else if(e&&e.name==="TimeoutError")dica="O pedido de permissão não apareceu — confira se a câmera não está bloqueada pro site (cadeado 🔒 na barra). Ou digite o número.";
    else if(e&&e.name==="NotFoundError")dica="Nenhuma câmera encontrada neste aparelho. Digite o número do bico.";
    msg.textContent="Câmera não abriu ("+(e&&e.name||"erro")+"). "+dica;
    return;
  }
  msg.textContent="Procurando o QR…";
  if("BarcodeDetector" in window){
    const det=new BarcodeDetector({formats:["qr_code"]});
    _scanTimer=setInterval(async()=>{
      try{const codes=await det.detect(vid);
        if(codes.length&&_scanAchou(codes[0].rawValue))return;
      }catch(e){}
    },300);
  } else {
    // fallback: jsQR via canvas (carrega a lib só quando precisa)
    try{
      if(!window.jsQR){
        _jsqrCarregando=_jsqrCarregando||new Promise((ok,err)=>{
          const s=document.createElement("script");
          s.src="/cashback/jsqr.js";
          s.onload=ok;s.onerror=err;document.head.appendChild(s);
        });
        await _jsqrCarregando;
      }
      const cv=document.createElement("canvas"),cx=cv.getContext("2d",{willReadFrequently:true});
      _scanTimer=setInterval(()=>{
        try{
          if(!vid.videoWidth)return;
          cv.width=vid.videoWidth;cv.height=vid.videoHeight;
          cx.drawImage(vid,0,0);
          const img=cx.getImageData(0,0,cv.width,cv.height);
          const q=window.jsQR(img.data,img.width,img.height);
          if(q&&q.data)_scanAchou(q.data);
        }catch(e){}
      },350);
    }catch(e){msg.textContent="Leitor indisponível neste navegador — digite o número do bico.";}
  }
}
function fecharScanner(){
  if(_scanTimer){clearInterval(_scanTimer);_scanTimer=null;}
  if(_scanStream){_scanStream.getTracks().forEach(t=>t.stop());_scanStream=null;}
  document.getElementById("scan-overlay").style.display="none";
}

// ---- PLANO C: FOTO do QR pelo app de câmera nativo (funciona em qualquer
// navegador, sem permissão de vídeo — inclusive navegador embutido) ----
async function _decodificarImagem(bmp){
  if("BarcodeDetector" in window){
    try{const det=new BarcodeDetector({formats:["qr_code"]});
      const codes=await det.detect(bmp);
      if(codes.length)return codes[0].rawValue;}catch(e){}
  }
  if(!window.jsQR){
    _jsqrCarregando=_jsqrCarregando||new Promise((ok,err)=>{
      const s=document.createElement("script");
      s.src="/cashback/jsqr.js";
      s.onload=ok;s.onerror=err;document.head.appendChild(s);
    });
    await _jsqrCarregando;
  }
  // reduz a foto (12MP trava o decoder) e tenta em VÁRIOS tamanhos, com e sem
  // inversão de cor (attemptBoth) — foto real tem blur/ângulo/iluminação
  for(const alvo of [520, 800, 1200, 1800]){
    const esc=Math.min(1, alvo/Math.max(bmp.width,bmp.height));
    const cv=document.createElement("canvas");
    cv.width=Math.round(bmp.width*esc);cv.height=Math.round(bmp.height*esc);
    const cx=cv.getContext("2d");cx.drawImage(bmp,0,0,cv.width,cv.height);
    const img=cx.getImageData(0,0,cv.width,cv.height);
    const q=window.jsQR(img.data,img.width,img.height,{inversionAttempts:"attemptBoth"});
    if(q&&q.data)return q.data;
  }
  return null;
}
// carrega a foto respeitando a rotação EXIF; se createImageBitmap não aceitar
// o formato (ex.: HEIC do iPhone), cai pro <img> que o navegador sabe renderizar
async function _fotoParaBitmap(f){
  try{return await createImageBitmap(f,{imageOrientation:"from-image"});}catch(e1){}
  try{return await createImageBitmap(f);}catch(e2){}
  return await new Promise((ok,err)=>{
    const url=URL.createObjectURL(f), im=new Image();
    im.onload=()=>{URL.revokeObjectURL(url);ok(im);};
    im.onerror=()=>{URL.revokeObjectURL(url);err(new DOMException("formato de foto não suportado","NotSupportedError"));};
    im.src=url;
  });
}
function scanPorFoto(){document.getElementById("scan-foto").click();}
document.getElementById("scan-foto").addEventListener("change",async function(){
  const f=this.files&&this.files[0];this.value="";
  if(!f)return;
  const msg=document.getElementById("scan-msg");
  msg.textContent="Lendo a foto…";
  try{
    const bmp=await _fotoParaBitmap(f);
    const texto=await _decodificarImagem(bmp);
    if(texto&&_scanAchou(texto))return;
    msg.textContent="Não achei o QR na foto — enche a tela com o QR (sem cortar as bordas) e evita reflexo. Ou digite o número.";
  }catch(e){msg.textContent="Falha ao ler a foto ("+(e.name||"erro")+"). Digite o número do bico.";}
});

// ---- PWA: service worker + botão de instalação ----
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/cashback/sw.js", { scope: "/cashback" }).catch(()=>{});
}
let _pwaEvt=null;
const _standalone = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
window.addEventListener("beforeinstallprompt", e => {
  e.preventDefault(); _pwaEvt = e;
  if(!_standalone){
    document.getElementById("pwa-card").classList.remove("esc");
    document.getElementById("pwa-btn").classList.remove("esc");
  }
});
async function pwaInstalar(){
  if(!_pwaEvt) return;
  _pwaEvt.prompt();
  const r = await _pwaEvt.userChoice;
  if(r && r.outcome === "accepted") document.getElementById("pwa-card").classList.add("esc");
  _pwaEvt = null;
}
// iPhone/iPad (Safari não tem prompt): mostra a instrução manual
(function(){
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  if(ios && !_standalone){
    document.getElementById("pwa-card").classList.remove("esc");
    document.getElementById("pwa-txt").innerHTML =
      "No iPhone: toque em <b>Compartilhar</b> (⬆️) e depois em <b>Adicionar à Tela de Início</b>.";
  }
})();
</script>
</div></body></html>
"""
