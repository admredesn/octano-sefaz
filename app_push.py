# -*- coding: utf-8 -*-
"""
app_push.py — notificações do app Postos SN pelo Firebase Cloud Messaging (05/10/2026).

Uma campanha criada no retaguarda (oct_app_campanhas) com push_em preenchido entra na
fila; quando chega a hora, este worker manda a notificação para cada celular que
aceitou publicidade (oct_app_dispositivos.aceita_marketing; campanha tipo 'aviso' usa
aceita_avisos), filtrando pelos postos da campanha, e grava uma linha por envio em
oct_app_envios. O Firebase entrega no Android e no iPhone (a chave da Apple fica
cadastrada no próprio Firebase).

Credencial: variável de ambiente FIREBASE_SA = o JSON da conta de serviço do projeto
Firebase (inteiro ou em base64). Sem ela o worker fica parado e a campanha espera na
fila — nada é marcado como enviado.

Dois workers do gunicorn rodam este laço: cada campanha é "reservada" com um PATCH
condicionado (push_enviado_em=is.null, return=representation). Só quem recebe a linha
de volta envia — sem isso a campanha sairia em dobro (auditoria #188/#190).

Sem biblioteca nova: o acesso ao Google é um JWT RS256 assinado com `cryptography`
(já usada nos certificados) e trocado por um token OAuth de 1 h.
"""

import base64
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests as rq

CICLO_SEG = 30
PARALELO = 8                 # envios simultâneos ao Firebase
PRAZO_ATRASO_H = 2           # notificação mais atrasada que isso não sai (ver rodada)
_on = False
_avisou_sem_credencial = [False]
_tok = {"v": None, "exp": 0}


def _log(msg):
    print(f"[app-push] {msg}", flush=True)


# ------------------------------------------------------------------
# Supabase (service key do servidor)
# ------------------------------------------------------------------
def _supa():
    return (os.environ.get("SUPABASE_URL", "").strip().rstrip("/"),
            os.environ.get("SUPABASE_SERVICE_KEY", "").strip())


def _h(extra=None):
    _, key = _supa()
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def _sget(q):
    url, _ = _supa()
    r = rq.get(f"{url}/rest/v1/{q}", headers=_h(), timeout=30)
    r.raise_for_status()
    return r.json()


def _sget_tudo(q, passo=1000):
    """Lista inteira, de 1000 em 1000 (o PostgREST corta em 1000 calado). q precisa de order."""
    out, ini = [], 0
    while True:
        url, _ = _supa()
        r = rq.get(f"{url}/rest/v1/{q}", headers=_h({"Range-Unit": "items", "Range": f"{ini}-{ini + passo - 1}"}),
                   timeout=30)
        r.raise_for_status()
        lote = r.json()
        out.extend(lote)
        if len(lote) < passo:
            return out
        ini += passo


def _spatch(q, corpo, representar=False):
    url, _ = _supa()
    r = rq.patch(f"{url}/rest/v1/{q}", headers=_h({"Prefer": "return=representation" if representar
                                                   else "return=minimal"}), json=corpo, timeout=30)
    r.raise_for_status()
    return r.json() if representar and (r.text or "").strip() else None


def _spost(tab, linhas):
    url, _ = _supa()
    r = rq.post(f"{url}/rest/v1/{tab}", headers=_h({"Prefer": "return=minimal"}), json=linhas, timeout=30)
    r.raise_for_status()


# ------------------------------------------------------------------
# Firebase Cloud Messaging (API v1)
# ------------------------------------------------------------------
def _sa():
    bruto = (os.environ.get("FIREBASE_SA") or "").strip()
    if not bruto:
        return None
    try:
        if not bruto.startswith("{"):
            bruto = base64.b64decode(bruto).decode()
        sa = json.loads(bruto)
        return sa if sa.get("client_email") and sa.get("private_key") and sa.get("project_id") else None
    except Exception:
        return None


def configurado():
    return _sa() is not None


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _access_token(sa):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    agora = int(time.time())
    if _tok["v"] and _tok["exp"] - 120 > agora:
        return _tok["v"]
    aud = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
    cab = {"alg": "RS256", "typ": "JWT"}
    corpo = {"iss": sa["client_email"], "scope": "https://www.googleapis.com/auth/firebase.messaging",
             "aud": aud, "iat": agora, "exp": agora + 3600}
    msg = _b64u(json.dumps(cab).encode()) + "." + _b64u(json.dumps(corpo).encode())
    chave = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    assinatura = chave.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
    r = rq.post(aud, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                           "assertion": msg + "." + _b64u(assinatura)}, timeout=20)
    r.raise_for_status()
    j = r.json()
    _tok.update(v=j["access_token"], exp=agora + int(j.get("expires_in") or 3600))
    return _tok["v"]


def enviar_um(token_aparelho, titulo, texto, imagem=None, dados=None, canal="campanhas"):
    """Uma notificação para um celular. Devolve (ok, erro, token_morto).
    token_morto = o app foi desinstalado ou o token trocou: o aparelho deve sair da lista."""
    sa = _sa()
    if not sa:
        return False, "Firebase não configurado (FIREBASE_SA)", False
    msg = {
        "token": token_aparelho,
        "notification": {"title": str(titulo or "")[:120], "body": str(texto or "")[:400]},
        "data": {k: str(v) for k, v in (dados or {}).items() if v is not None},
        "android": {"priority": "HIGH", "notification": {"channel_id": canal}},
        "apns": {"payload": {"aps": {"sound": "default", "mutable-content": 1}}},
    }
    if imagem:
        msg["notification"]["image"] = imagem
        msg["apns"]["fcm_options"] = {"image": imagem}
    try:
        r = rq.post(f"https://fcm.googleapis.com/v1/projects/{sa['project_id']}/messages:send",
                    headers={"Authorization": "Bearer " + _access_token(sa), "Content-Type": "application/json"},
                    json={"message": msg}, timeout=20)
    except Exception as e:
        return False, f"sem conexão com o Firebase: {str(e)[:120]}", False
    if r.status_code == 200:
        return True, None, False
    det = (r.text or "")[:300]
    morto = r.status_code == 404 or "UNREGISTERED" in det
    return False, f"HTTP {r.status_code}: {det}", morto


# ------------------------------------------------------------------
# Público da campanha
# ------------------------------------------------------------------
def publico(campanha):
    """Aparelhos que recebem: ativos, com o consentimento certo e ligados a um dos postos
    da campanha (posto de origem do cadastro, posto favorito ou acionou o app ali nos
    últimos 90 dias). Campanha sem postos = rede toda."""
    campo = "aceita_avisos" if campanha.get("tipo") == "aviso" else "aceita_marketing"
    devs = _sget_tudo(f"oct_app_dispositivos?ativo=eq.true&{campo}=eq.true"
                      "&select=id,cliente_cpf,token,posto_favorito&order=id")
    emp = [e for e in (campanha.get("empresa_ids") or []) if e]
    if not emp or not devs:
        return devs
    lista = ",".join(emp)
    cpfs = {c["cpf"] for c in _sget_tudo(
        f"oct_cashback_clientes?or=(empresa_origem.in.({lista}),posto_favorito.in.({lista}))&select=cpf&order=cpf")}
    corte = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cpfs |= {a["cliente_cpf"] for a in _sget_tudo(
        f"oct_cashback_acionamentos?empresa_id=in.({lista})&criado_em=gte.{corte}&select=cliente_cpf&order=id")}
    return [d for d in devs if d["cliente_cpf"] in cpfs or (d.get("posto_favorito") in emp)]


# ------------------------------------------------------------------
# Envio de uma campanha
# ------------------------------------------------------------------
def enviar_campanha(c):
    titulo = (c.get("push_titulo") or c.get("titulo") or "").strip()
    texto = (c.get("push_texto") or c.get("texto") or "").strip()
    dados = {"tipo": "campanha", "campanha_id": c["id"], "link": c.get("link") or ""}
    canal = "avisos" if c.get("tipo") == "aviso" else "campanhas"
    devs = publico(c)
    _log(f"campanha {c['id']} '{titulo[:40]}': {len(devs)} aparelho(s)")

    def um(d):
        ok, erro, morto = enviar_um(d["token"], titulo, texto, c.get("imagem_url"), dados, canal)
        return d, ok, erro, morto

    linhas, total, erros, mortos = [], 0, 0, []
    with ThreadPoolExecutor(max_workers=PARALELO) as ex:
        for d, ok, erro, morto in ex.map(um, devs):
            total += 1 if ok else 0
            erros += 0 if ok else 1
            if morto:
                mortos.append(d["id"])
            linhas.append({"campanha_id": c["id"], "tipo": "campanha", "cliente_cpf": d["cliente_cpf"],
                           "dispositivo_id": d["id"], "titulo": titulo[:120], "texto": texto[:400],
                           "status": "ok" if ok else "erro", "erro": (erro or None) and erro[:300]})
    for i in range(0, len(linhas), 200):
        try:
            _spost("oct_app_envios", linhas[i:i + 200])
        except Exception as e:
            _log(f"não gravei o registro dos envios: {e}")
    for i in range(0, len(mortos), 100):
        try:
            _spatch(f"oct_app_dispositivos?id=in.({','.join(mortos[i:i + 100])})", {"ativo": False})
        except Exception:
            pass
    _spatch(f"oct_app_campanhas?id=eq.{c['id']}", {"push_total": total, "push_erros": erros})
    _log(f"campanha {c['id']}: {total} enviada(s), {erros} erro(s), {len(mortos)} aparelho(s) desativado(s)")
    return total, erros


def avisar_cliente(cpf, titulo, texto, dados=None):
    """Aviso de serviço para UM cliente (cashback pago, abastecimento concluído, fatura
    vencendo) — vai para quem aceitou avisos, não depende do consentimento de publicidade."""
    if not configurado():
        return 0
    devs = _sget(f"oct_app_dispositivos?cliente_cpf=eq.{cpf}&ativo=eq.true&aceita_avisos=eq.true"
                 "&select=id,token&limit=10")
    n = 0
    for d in devs:
        ok, erro, morto = enviar_um(d["token"], titulo, texto, None, {**(dados or {}), "tipo": "aviso"}, "avisos")
        n += 1 if ok else 0
        try:
            _spost("oct_app_envios", [{"tipo": "aviso", "cliente_cpf": cpf, "dispositivo_id": d["id"],
                                        "titulo": titulo[:120], "texto": (texto or "")[:400],
                                        "status": "ok" if ok else "erro", "erro": (erro or None) and erro[:300]}])
            if morto:
                _spatch(f"oct_app_dispositivos?id=eq.{d['id']}", {"ativo": False})
        except Exception:
            pass
    return n


# ------------------------------------------------------------------
# Fila
# ------------------------------------------------------------------
def rodada():
    if not configurado():
        if not _avisou_sem_credencial[0]:
            _avisou_sem_credencial[0] = True
            _log("FIREBASE_SA não configurada: campanhas com notificação ficam na fila até configurar")
        return 0
    agora_dt = datetime.now(timezone.utc)
    agora = agora_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    fila = _sget(f"oct_app_campanhas?push_em=lte.{agora}&push_enviado_em=is.null&ativo=eq.true"
                 "&select=*&order=push_em&limit=5")
    feitas = 0
    for c in fila:
        # PASSOU DA HORA (05/10/2026): campanha que esperou na fila (Firebase ainda não
        # configurado, servidor fora) NÃO sai atrasada — "promoção até as 22h" chegando no
        # dia seguinte é pior que não chegar. Marca push_erros=-2 e o retaguarda mostra
        # "não enviada: passou da hora".
        try:
            push_em = datetime.fromisoformat(str(c["push_em"]).replace("Z", "+00:00"))
            fim = datetime.fromisoformat(str(c["fim"]).replace("Z", "+00:00")) if c.get("fim") else None
        except Exception:
            push_em, fim = agora_dt, None
        if agora_dt - push_em > timedelta(hours=PRAZO_ATRASO_H) or (fim and fim <= agora_dt):
            _spatch(f"oct_app_campanhas?id=eq.{c['id']}&push_enviado_em=is.null",
                    {"push_enviado_em": agora, "push_erros": -2})
            _log(f"campanha {c['id']}: notificação NÃO enviada (passou da hora)")
            continue
        # reserva: só um dos workers recebe a linha de volta
        pega = _spatch(f"oct_app_campanhas?id=eq.{c['id']}&push_enviado_em=is.null",
                       {"push_enviado_em": agora}, representar=True)
        if not pega:
            continue
        try:
            enviar_campanha(pega[0])
            feitas += 1
        except Exception as e:
            _log(f"campanha {c['id']} falhou no meio: {e}")
            try:
                _spatch(f"oct_app_campanhas?id=eq.{c['id']}", {"push_erros": -1})
            except Exception:
                pass
    return feitas


def iniciar():
    global _on
    if _on or os.environ.get("APP_PUSH", "1").strip().lower() in ("0", "false", "nao", "off"):
        return
    _on = True

    def loop():
        time.sleep(45)
        while True:
            try:
                rodada()
            except Exception as e:
                _log(f"erro na rodada: {e}")
            time.sleep(CICLO_SEG)

    threading.Thread(target=loop, daemon=True).start()
    _log(f"fila de notificações ativa (a cada {CICLO_SEG} s)")
