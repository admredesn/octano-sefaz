"""
operadores_admin.py  -  Definir/trocar a senha de um operador do PDV.

POR QUE ISTO EXISTE NO SERVIDOR, E NAO NO NAVEGADOR
    Criar uma conta funciona com a chave publica (anon): o proprio Supabase
    permite signUp. Mas TROCAR a senha de OUTRA pessoa exige a chave de
    administracao (service_role), que da acesso irrestrito ao banco inteiro.
    Essa chave nao pode chegar ao navegador de forma alguma -- qualquer um com
    o F12 aberto a copiaria. Entao o retaguarda pede aqui, e o servidor (que ja
    tem a SUPABASE_SERVICE_KEY no ambiente do Railway) executa.

QUEM PODE CHAMAR
    So um GERENTE (oct_perfis.master = true) e so sobre operadores da PROPRIA
    empresa. O retaguarda envia o access_token da sessao dele; nos validamos
    esse token no proprio Supabase e conferimos o perfil. Sem token valido de
    gerente, a rota recusa -- ela nunca aceita "confie em mim, sou o gerente"
    vindo do corpo da requisicao.
"""

import os
import json
import urllib.request
import urllib.error

from flask import request, jsonify


def _cfg():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    return url, key


def _req(url, *, metodo="GET", corpo=None, headers=None, timeout=20):
    dados = json.dumps(corpo).encode() if corpo is not None else None
    r = urllib.request.Request(url, data=dados, method=metodo)
    for k, v in (headers or {}).items():
        r.add_header(k, v)
    if dados is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(txt) if txt.strip() else None)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"erro": txt[:300]}
    except Exception as e:
        return 0, {"erro": str(e)[:300]}


def _quem_e(url, key, token):
    """Valida o access_token no Supabase e devolve o uid de quem chamou."""
    status, corpo = _req(f"{url}/auth/v1/user", headers={
        "apikey": key, "Authorization": f"Bearer {token}"})
    if status != 200 or not isinstance(corpo, dict):
        return None
    return corpo.get("id")


def _perfil(url, key, uid):
    status, corpo = _req(
        f"{url}/rest/v1/oct_perfis?id=eq.{uid}&select=id,empresa_id,master,ativo",
        headers={"apikey": key, "Authorization": f"Bearer {key}"})
    if status != 200 or not corpo:
        return None
    return corpo[0]


def registrar_rotas_operadores(app):
    @app.route("/operador/senha", methods=["POST"])
    def operador_senha():
        """Define a senha de um operador.  body: {token, alvo_uid, senha}"""
        try:
            url, key = _cfg()
            if not url or not key:
                return jsonify({"erro": "SUPABASE_URL / SUPABASE_SERVICE_KEY nao configurados"}), 500

            d = request.get_json(silent=True) or {}
            token = (d.get("token") or "").strip()
            alvo = (d.get("alvo_uid") or "").strip()
            senha = d.get("senha") or ""
            if not token or not alvo or not senha:
                return jsonify({"erro": "token, alvo_uid e senha sao obrigatorios"}), 400
            if len(senha) < 6:
                return jsonify({"erro": "a senha deve ter ao menos 6 caracteres"}), 400

            # 1) quem esta pedindo?
            uid = _quem_e(url, key, token)
            if not uid:
                return jsonify({"erro": "sessao invalida ou expirada"}), 401

            # 2) e gerente ativo?
            eu = _perfil(url, key, uid)
            if not eu or not eu.get("master") or eu.get("ativo") is False:
                return jsonify({"erro": "apenas um gerente pode redefinir senhas"}), 403

            # 3) o alvo e da MESMA empresa? (um gerente nao mexe em outro posto)
            dele = _perfil(url, key, alvo)
            if not dele:
                return jsonify({"erro": "operador nao encontrado"}), 404
            if str(dele.get("empresa_id")) != str(eu.get("empresa_id")):
                return jsonify({"erro": "operador de outra empresa"}), 403

            # 4) troca a senha pela Admin API
            status, corpo = _req(
                f"{url}/auth/v1/admin/users/{alvo}", metodo="PUT",
                corpo={"password": senha},
                headers={"apikey": key, "Authorization": f"Bearer {key}"})
            if status not in (200, 204):
                return jsonify({"erro": "falha ao trocar a senha",
                                "detalhe": corpo}), 422
            return jsonify({"ok": True}), 200
        except Exception as e:
            return jsonify({"erro": str(e)[:300]}), 500
