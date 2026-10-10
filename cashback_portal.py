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

VENDA A PRAZO COM FACIAL (01/10/2026, decisão do Ronan): a "assinatura" da compra a
prazo é uma SELFIE tirada ANTES de abastecer (foto como prova — sem reconhecimento
automático). No cadastro o cliente aceita o termo (LGPD) e tira a foto de referência;
no acionamento forma 05 a selfie é OBRIGATÓRIA. O caixa vê as duas fotos lado a lado
na contagem do PDV e o cupom sai com "autorizada por NOME — código". As fotos ficam
num bucket PRIVADO (octano-faces): só este servidor lê/grava; o PDV recebe link
assinado de 10 min, e só com operador logado. Selfie de compra: apagada após 60 dias.
"""

import os
import re
import json
import hmac
import base64
import hashlib
import secrets
import time
import uuid
from datetime import datetime, timezone, timedelta

import requests as rq
from flask import Blueprint, request, jsonify, Response

bp_cashback = Blueprint("cashback", __name__)

ACIONAMENTO_VALIDADE_MIN = 60      # acionamento vale 1h
JANELA_2H_SEG = 2 * 3600

COMBUSTIVEIS = ["GASOLINA COMUM", "GASOLINA ADITIVADA", "ETANOL", "DIESEL S10", "DIESEL S500"]
FORMAS = [("01", "Dinheiro"), ("17", "PIX"), ("05", "A Prazo")]


def _prazo_conta_confirmada(empresa_id, cli):
    """A prazo pelo app (05/10/2026, auditoria #17/#48): só para conta confirmada pelo
    contato que o POSTO tem da pessoa, ou cujo telefone do app é o mesmo do cadastro do
    posto (quem liberou o crédito conferiu esse cadastro)."""
    if str(cli.get("verificado_via") or "").endswith("posto"):
        return True
    try:
        p = _sget(f"oct_pessoas?empresa_id=eq.{empresa_id}&documento=eq.{cli['cpf']}"
                  f"&select=telefone,whatsapp&limit=1")
    except Exception:
        return False
    meu = _so_digitos(cli.get("telefone"))[-10:]
    return bool(p and len(meu) == 10 and meu in {_so_digitos(p[0].get(k))[-10:] for k in ("telefone", "whatsapp")})


def _prazo_liberado(empresa_id, cpf):
    """Cliente pode comprar A PRAZO neste posto? A liberação é do POSTO.
    Se a pessoa está VINCULADA a uma EMPRESA (frota_empresa_id), o crédito é
    o da EMPRESA e a venda sai no NOME DELA.
    Retorna (ok, motivo, conta) — conta = {"pessoa_id", "nome", "empresa": bool}."""
    try:
        p = _sget(f"oct_pessoas?empresa_id=eq.{empresa_id}&documento=eq.{cpf}"
                  f"&select=id,nome,aceita_nota_prazo,credito_bloqueado,frota_empresa_id&limit=1")
        if not p:
            return False, "Seu cadastro ainda não existe neste posto — fale com o caixa.", None
        pes = p[0]
        # COLABORADOR de empresa: usa o crédito da EMPRESA vinculada
        if pes.get("frota_empresa_id"):
            e = _sget(f"oct_pessoas?id=eq.{pes['frota_empresa_id']}"
                      f"&select=id,nome,aceita_nota_prazo,credito_bloqueado&limit=1")
            if not e:
                return False, "Empresa vinculada não encontrada — fale com o posto.", None
            emp = e[0]
            if emp.get("credito_bloqueado"):
                return False, f"Crédito da empresa {emp.get('nome')} está bloqueado — fale com o posto.", None
            if not emp.get("aceita_nota_prazo"):
                return False, f"A empresa {emp.get('nome')} não está liberada a prazo — fale com o posto.", None
            return True, None, {"pessoa_id": emp["id"], "nome": emp.get("nome"), "empresa": True}
        if pes.get("credito_bloqueado"):
            return False, "Crédito bloqueado neste posto — fale com o caixa.", None
        if not pes.get("aceita_nota_prazo"):
            return False, "Compra a prazo ainda não liberada pro seu cadastro — fale com o posto.", None
        return True, None, {"pessoa_id": pes["id"], "nome": pes.get("nome"), "empresa": False}
    except Exception as e:
        return False, "não deu pra verificar o crédito: " + str(e)[:80], None


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


# ------------------------------------------------------------------
# FACIAL — fotos de rosto (bucket PRIVADO) + termo + código de autenticação
# ------------------------------------------------------------------
BUCKET_FACES = "octano-faces"
FACE_GUARDA_DIAS = 60            # selfie de COMPRA; a foto do cadastro fica enquanto a conta existir
TERMO_VERSAO = "2026-10-01"
TERMO_TEXTO = (
    "TERMO DE CONSENTIMENTO — USO DA IMAGEM DO ROSTO (LGPD, Lei 13.709/2018)\n\n"
    "1. O que coletamos: uma foto do seu rosto no cadastro e uma foto a cada compra a prazo "
    "feita pelo aplicativo, com data, hora, aparelho e endereço de rede usados.\n\n"
    "2. Para que serve: confirmar que é você quem está autorizando a compra a prazo no posto e "
    "servir de comprovante dessa autorização, no lugar da assinatura em papel. A foto NÃO é "
    "usada para reconhecimento automático nem para outra finalidade.\n\n"
    "3. Quem vê: o posto onde você compra (caixa e gerência) e a empresa que opera o sistema. "
    "As fotos não são vendidas nem repassadas a terceiros.\n\n"
    "4. Por quanto tempo: a foto de cada compra é apagada depois de 60 dias. A foto do cadastro "
    "fica guardada enquanto a sua conta existir.\n\n"
    "5. Seus direitos: você pode pedir a qualquer momento para ver, corrigir ou apagar as suas "
    "fotos e retirar este consentimento, no próprio posto. Sem a foto, a compra a prazo continua "
    "possível no caixa, com a via em papel.\n\n"
    "Ao marcar o aceite e tirar a foto, você concorda com este termo."
)


def _st_headers(extra=None):
    _, key = _supa()
    h = {"apikey": key, "Authorization": "Bearer " + key}
    if extra:
        h.update(extra)
    return h


def _st_upload(caminho, dados):
    url, _ = _supa()
    _checa(rq.post(f"{url}/storage/v1/object/{BUCKET_FACES}/{caminho}", data=dados,
                   headers=_st_headers({"Content-Type": "image/jpeg", "x-upsert": "true"}), timeout=30))


def _st_assinar(caminho, seg=600):
    """Link temporário para UMA foto (o bucket é privado). None se a foto não existe mais."""
    if not caminho:
        return None
    url, _ = _supa()
    r = rq.post(f"{url}/storage/v1/object/sign/{BUCKET_FACES}/{caminho}", json={"expiresIn": seg},
                headers=_st_headers({"Content-Type": "application/json"}), timeout=15)
    if r.status_code >= 400:
        return None
    u = (r.json() or {}).get("signedURL") or (r.json() or {}).get("signedUrl") or ""
    return (f"{url}/storage/v1{u}" if u.startswith("/") else u) or None


def _foto_bytes(dataurl):
    """'data:image/jpeg;base64,...' -> bytes. Só JPEG, de 4 KB a 700 KB (a tela reduz para 640 px)."""
    txt = str(dataurl or "")
    if not txt.startswith("data:image/jpeg;base64,"):
        raise ValueError("Tire a foto do rosto para autorizar a compra a prazo.")
    try:
        dados = base64.b64decode(txt.split(",", 1)[1], validate=True)
    except Exception:
        raise ValueError("Foto inválida — tire de novo.")
    if dados[:2] != b"\xff\xd8" or not (4000 <= len(dados) <= 700000):
        raise ValueError("Foto inválida — tire de novo.")
    return dados


def _cpf_mascara(cpf):
    c = _so_digitos(cpf)
    return f"***.{c[3:6]}.{c[6:9]}-**" if len(c) == 11 else "***"


def _auth_codigo(ac_id, cpf, foto_hash):
    """Código curto impresso no cupom: prova que ESTA selfie autorizou ESTE acionamento."""
    return hmac.new(_chave("auth"), f"{ac_id}|{cpf}|{foto_hash}".encode(), hashlib.sha256).hexdigest()[:8].upper()


_FACES_LIMPEZA = {"ts": 0.0}


def _faces_limpar():
    """Apaga as selfies de COMPRA com mais de 60 dias (pastas compras/AAAA-MM-DD). No máximo
    1 vez por dia, de carona num acionamento — sem agendador."""
    if time.time() - _FACES_LIMPEZA["ts"] < 86400:
        return
    _FACES_LIMPEZA["ts"] = time.time()
    try:
        url, _ = _supa()
        hj = {"Content-Type": "application/json"}
        corte = (datetime.now(timezone.utc) - timedelta(days=FACE_GUARDA_DIAS)).strftime("%Y-%m-%d")
        r = rq.post(f"{url}/storage/v1/object/list/{BUCKET_FACES}", headers=_st_headers(hj), timeout=20,
                    json={"prefix": "compras", "limit": 1000, "sortBy": {"column": "name", "order": "asc"}})
        for pasta in (r.json() if r.status_code < 400 else []):
            dia = pasta.get("name") or ""
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", dia) or dia >= corte:
                continue
            r2 = rq.post(f"{url}/storage/v1/object/list/{BUCKET_FACES}", headers=_st_headers(hj), timeout=20,
                         json={"prefix": f"compras/{dia}", "limit": 1000})
            nomes = [f"compras/{dia}/{o['name']}" for o in (r2.json() if r2.status_code < 400 else []) if o.get("name")]
            if nomes:
                rq.delete(f"{url}/storage/v1/object/{BUCKET_FACES}", headers=_st_headers(hj),
                          json={"prefixes": nomes}, timeout=30)
    except Exception:
        pass


_OPERADOR_CACHE = {}


def _operador_logado():
    """A chamada vem de um operador LOGADO no PDV/retaguarda? Confere o token do Supabase
    (o mesmo da sessão dele). Guarda o resultado por 5 min."""
    tok = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    if len(tok) < 40:
        return False
    ate = _OPERADOR_CACHE.get(tok)
    if ate and ate > time.time():
        return True
    try:
        url, key = _supa()
        r = rq.get(f"{url}/auth/v1/user", headers={"apikey": key, "Authorization": "Bearer " + tok}, timeout=10)
        if r.status_code == 200 and (r.json() or {}).get("id"):
            if len(_OPERADOR_CACHE) > 500:
                _OPERADOR_CACHE.clear()
            _OPERADOR_CACHE[tok] = time.time() + 300
            return True
    except Exception:
        pass
    return False


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
    """CHAVE_MESTRA do ambiente. SEM valor padrão (05/10/2026, auditoria #226): com o
    padrão escrito no código, qualquer um forjava sessão de qualquer CPF se a variável
    faltasse no servidor. Faltou = o portal para, em vez de aceitar token forjável."""
    m = (os.environ.get("CHAVE_MESTRA") or "").strip()
    if not m:
        raise RuntimeError("CHAVE_MESTRA não configurada no servidor")
    return m.encode()


def _chave(uso):
    """Uma chave por uso, derivada da CHAVE_MESTRA (que também cifra os certificados):
    vazar o segredo de um uso não entrega o outro."""
    return hmac.new(_segredo(), ("octano-cashback|" + uso).encode(), hashlib.sha256).digest()


# Sessão v2 (05/10/2026): "v2|cpf|exp|versão|assinatura". A versão é
# oct_cashback_clientes.token_versao: trocar a senha (ou "sair de todos os aparelhos")
# soma 1 e derruba as sessões antigas. Token v1 (sem versão) não vale mais.
TOKEN_DIAS = 30


def _token_gerar(cpf, versao=0):
    exp = int(time.time()) + TOKEN_DIAS * 86400
    corpo = f"v2|{cpf}|{exp}|{int(versao or 0)}"
    ass = hmac.new(_chave("token"), corpo.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{corpo}|{ass}".encode()).decode()


def _token_validar(tok):
    """(cpf, versão) ou None."""
    try:
        corpo = base64.urlsafe_b64decode(tok.encode()).decode()
        partes = corpo.split("|")
        if len(partes) != 5 or partes[0] != "v2":
            return None
        _, cpf, exp, ver, ass = partes
        if int(exp) < time.time():
            return None
        esperado = hmac.new(_chave("token"), f"v2|{cpf}|{exp}|{ver}".encode(), hashlib.sha256).hexdigest()[:32]
        return (cpf, int(ver)) if hmac.compare_digest(ass, esperado) else None
    except Exception:
        return None


def _cliente_do_token():
    tok = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    val = _token_validar(tok) if tok else None
    if not val:
        return None
    cpf, ver = val
    rows = _sget(f"oct_cashback_clientes?cpf=eq.{cpf}&limit=1")
    if not rows:
        return None
    cli = rows[0]
    if int(cli.get("token_versao") or 0) != ver or not cli.get("verificado_em"):
        return None                     # senha trocada / saiu de todos / conta não confirmada
    return cli


def _token_do_cliente(cli):
    return _token_gerar(cli["cpf"], cli.get("token_versao") or 0)


# ------------------------------------------------------------------
# LIMITES DE TENTATIVA (05/10/2026, auditoria #224). Por IP: na memória do
# processo (barato; cada worker conta o seu). Por CPF: na linha do cliente
# (vale entre os workers e sobrevive a reinício).
# ------------------------------------------------------------------
_LIMITES = {}


def _ip():
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()


def _limite_ok(chave, maximo, janela_seg):
    agora = time.time()
    if len(_LIMITES) > 5000:
        _LIMITES.clear()
    lst = [t for t in _LIMITES.get(chave, []) if agora - t < janela_seg]
    if len(lst) >= maximo:
        _LIMITES[chave] = lst
        return False
    lst.append(agora)
    _LIMITES[chave] = lst
    return True


def _muitas(msg="Muitas tentativas seguidas. Espere alguns minutos e tente de novo."):
    return jsonify({"erro": msg}), 429


LOGIN_MAX_FALHAS = 5          # erros de senha seguidos antes de bloquear o CPF
LOGIN_BLOQUEIO_SEG = 15 * 60
CODIGO_MAX_TENTATIVAS = 5     # erros no código antes de ele deixar de valer
CODIGO_VALIDADE_SEG = 15 * 60
CODIGO_INTERVALO_SEG = 60     # entre dois pedidos de código do mesmo CPF
CODIGO_MAX_HORA = 5


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
# CÓDIGOS DE 6 DÍGITOS: confirmar o cadastro e recuperar a senha (05/10/2026).
#
# Auditoria #17/#48: o cadastro aceitava qualquer CPF válido sem prova de nada;
# quem cadastrasse primeiro o CPF de um cliente a prazo comprava na conta dele. Agora
# a conta só vale depois de confirmada por código, e o código vai para o CONTATO
# OFICIAL: o WhatsApp/telefone/e-mail que o POSTO tem da pessoa (oct_pessoas). Só
# quando o posto não tem contato nenhum vale o que a pessoa digitou no app — e aí a
# conta fica "confirmada pelo app", que não compra a prazo (ver api_acionar).
#
# Envio: WhatsApp pelo gateway da rede (WPP_URL/WPP_TOKEN). E-mail pelo SMTP do
# servidor (SMTP_*), que no Railway costuma estar bloqueado — o WhatsApp é o canal.
# ------------------------------------------------------------------
def _mascara_fone(d):
    return "WhatsApp •••" + _so_digitos(d)[-4:]


def _mascara_email(e):
    u, _, dom = str(e or "").partition("@")
    return (u[:1] + "•••@" + dom) if dom else "e-mail"


def _contatos_oficiais(cpf, cli=None):
    """(fones, emails, origem). origem 'posto' = contato que o POSTO cadastrou;
    'app' = o posto não tem nenhum contato desta pessoa e vale o digitado no app."""
    fones, emails = [], []
    try:
        for p in _sget(f"oct_pessoas?documento=eq.{cpf}&select=whatsapp,telefone,email&limit=10"):
            for t in (p.get("whatsapp"), p.get("telefone")):
                d = _so_digitos(t)
                if len(d) >= 10 and d[-10:] not in [x[-10:] for x in fones]:
                    fones.append(d)
            e = str(p.get("email") or "").strip()
            if "@" in e and e.lower() not in [x.lower() for x in emails]:
                emails.append(e)
    except Exception:
        pass
    if fones or emails:
        return fones[:3], emails[:2], "posto"
    d = _so_digitos((cli or {}).get("telefone"))
    e = str((cli or {}).get("email") or "").strip()
    return ([d] if len(d) >= 10 else []), ([e] if "@" in e else []), "app"


def _wpp_texto(tel, msg):
    import urllib.request as _rq
    base = (os.environ.get("WPP_URL") or "").rstrip("/")
    tok = os.environ.get("WPP_TOKEN") or ""
    if not base or not tok:
        raise RuntimeError("WhatsApp indisponível")
    tel = _so_digitos(tel)
    if len(tel) <= 11:
        tel = "55" + tel
    req = _rq.Request(base + "/send-text",
                      data=json.dumps({"phone": tel, "message": msg}).encode(),
                      headers={"Content-Type": "application/json", "x-wpp-token": tok})
    with _rq.urlopen(req, timeout=30):
        pass


def _email_texto(email, assunto, texto):
    import smtplib
    from email.mime.text import MIMEText
    host = os.environ.get("SMTP_HOST") or ""
    user = os.environ.get("SMTP_USER") or ""
    senha = os.environ.get("SMTP_SENHA") or ""
    porta = int(os.environ.get("SMTP_PORT") or 587)
    if not host or not user:
        raise RuntimeError("E-mail indisponível")
    m = MIMEText(texto)
    m["Subject"] = assunto
    m["From"] = user
    m["To"] = email
    with smtplib.SMTP(host, porta, timeout=30) as s:
        s.starttls()
        s.login(user, senha)
        s.send_message(m)


_CODIGO_CAMPOS = {   # motivo -> (coluna do código, coluna da validade, coluna das tentativas)
    "verificar": ("verif_codigo", "verif_expira", "verif_tentativas"),
    "reset": ("reset_codigo", "reset_expira", "reset_tentativas"),
}


def _enviar_codigo(cli, motivo, canal="whatsapp"):
    """Gera, grava (hash) e envia o código ao contato oficial.
    Devolve (ok, destino_mascarado, origem, erro)."""
    agora = int(time.time())
    if agora - int(cli.get("envio_ultimo") or 0) < CODIGO_INTERVALO_SEG:
        return False, None, None, "Já enviamos um código agora há pouco. Espere 1 minuto para pedir outro."
    ini, qtd = int(cli.get("envio_janela_ini") or 0), int(cli.get("envio_qtd") or 0)
    if agora - ini > 3600:
        ini, qtd = agora, 0
    if qtd >= CODIGO_MAX_HORA:
        return False, None, None, "Muitos códigos pedidos para este CPF. Tente de novo em 1 hora."
    fones, emails, origem = _contatos_oficiais(cli["cpf"], cli)
    codigo = str(secrets.randbelow(900000) + 100000)
    col_cod, col_exp, col_tent = _CODIGO_CAMPOS[motivo]
    _spatch(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}",
            {col_cod: _hash_senha(codigo), col_exp: agora + CODIGO_VALIDADE_SEG, col_tent: 0,
             "envio_ultimo": agora, "envio_janela_ini": ini, "envio_qtd": qtd + 1})
    if motivo == "verificar":
        titulo, assunto = "Confirmação do cadastro", "confirmação do cadastro"
    else:
        titulo, assunto = "Recuperação de senha", "recuperação de senha"
    msg = (f"🔐 *{titulo} — Cashback do Posto (Rede SN)*\n\nSeu código é: *{codigo}*\n\n"
           "Ele vale por 15 minutos. Não passe este código para ninguém. Se você não pediu, ignore.")
    enviados, erros = [], []
    if canal == "email":
        for e in emails:
            try:
                _email_texto(e, f"Cashback do Posto — código de {assunto}",
                             f"Seu código de {assunto} é: {codigo}\n\nEle vale por 15 minutos. "
                             "Não passe este código para ninguém. Se você não pediu, ignore este e-mail.")
                enviados.append(_mascara_email(e))
            except Exception as ex:
                erros.append(str(ex)[:80])
        if not emails:
            erros.append("sem e-mail cadastrado")
    else:
        for t in fones:
            try:
                _wpp_texto(t, msg)
                enviados.append(_mascara_fone(t))
            except Exception as ex:
                erros.append(str(ex)[:80])
        if not fones:
            erros.append("sem WhatsApp cadastrado")
    if not enviados:
        dica = (" O posto não tem um contato seu atualizado: fale com o caixa para atualizar o seu WhatsApp."
                if origem == "posto" else "")
        return False, None, origem, "Não consegui enviar o código (" + "; ".join(erros) + ")." + dica
    return True, " e ".join(enviados), origem, None


def _conferir_codigo(cli, motivo, codigo):
    """None se o código confere; senão a mensagem de erro. Erros contam: no 5º o código morre."""
    col_cod, col_exp, col_tent = _CODIGO_CAMPOS[motivo]
    if not cli.get(col_cod) or int(cli.get(col_exp) or 0) < time.time():
        return "Código expirado — peça um novo."
    tent = int(cli.get(col_tent) or 0)
    if tent >= CODIGO_MAX_TENTATIVAS:
        return "Código bloqueado depois de várias tentativas erradas — peça um novo."
    if not _confere_senha(_so_digitos(codigo), cli[col_cod]):
        corpo = {col_tent: tent + 1}
        if tent + 1 >= CODIGO_MAX_TENTATIVAS:
            corpo[col_cod] = None
        _spatch(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}", corpo)
        resta = CODIGO_MAX_TENTATIVAS - tent - 1
        return ("Código incorreto." + (f" Restam {resta} tentativa(s)." if resta > 0
                                       else " O código deixou de valer — peça um novo."))
    return None


def _cliente_por_cpf(cpf):
    rows = _sget(f"oct_cashback_clientes?cpf=eq.{cpf}&limit=1") if cpf else []
    return rows[0] if rows else None


@bp_cashback.route("/cashback/api/senha/pedir", methods=["POST"])
def api_senha_pedir():
    d = request.get_json(silent=True) or {}
    cpf = _so_digitos(d.get("cpf"))
    canal = "email" if str(d.get("canal") or "") == "email" else "whatsapp"
    if not _limite_ok("pedir:" + _ip(), 10, 3600):
        return _muitas()
    generico = {"ok": True, "destino": None,
                "aviso": "Se este CPF já tiver conta no app, o código chega em instantes no WhatsApp que o posto "
                         "tem de você. Se você nunca criou a conta, volte e toque em 'Primeiro acesso? Cadastre-se'."}
    cli = _cliente_por_cpf(cpf) if _cpf_valido(cpf) else None
    if not cli:
        return jsonify(generico)          # não diz se o CPF existe (auditoria #224)
    ok, destino, _origem, erro = _enviar_codigo(cli, "reset", canal)
    if not ok:
        return jsonify({"erro": erro}), 429 if "Espere" in (erro or "") or "Muitos" in (erro or "") else 502
    return jsonify({"ok": True, "destino": destino})


@bp_cashback.route("/cashback/api/senha/trocar", methods=["POST"])
def api_senha_trocar():
    d = request.get_json(silent=True) or {}
    cpf = _so_digitos(d.get("cpf"))
    nova = str(d.get("senha") or "")
    if len(nova) < 6:
        return jsonify({"erro": "Senha deve ter pelo menos 6 caracteres"}), 400
    if not _limite_ok("trocar:" + _ip(), 30, 3600):
        return _muitas()
    cli = _cliente_por_cpf(cpf)
    if not cli:
        return jsonify({"erro": "Código expirado — peça um novo."}), 400
    erro = _conferir_codigo(cli, "reset", d.get("codigo"))
    if erro:
        return jsonify({"erro": erro}), 401
    # o código chegou no contato oficial: isso também CONFIRMA a conta (mesma prova do cadastro)
    _, _, origem = _contatos_oficiais(cpf, cli)
    versao = int(cli.get("token_versao") or 0) + 1        # derruba as sessões antigas
    corpo = {"senha_hash": _hash_senha(nova), "reset_codigo": None, "reset_expira": None, "reset_tentativas": 0,
             "token_versao": versao, "login_falhas": 0, "login_bloqueado_ate": None}
    if not cli.get("verificado_em") or origem == "posto":
        corpo.update({"verificado_em": datetime.now(timezone.utc).isoformat(), "verificado_via": "codigo-" + origem})
    _spatch(f"oct_cashback_clientes?cpf=eq.{cpf}", corpo)
    return jsonify({"ok": True, "token": _token_gerar(cpf, versao), "nome": cli.get("nome")})


@bp_cashback.route("/cashback/api/cadastro/confirmar", methods=["POST"])
def api_cadastro_confirmar():
    """Último passo do cadastro (e do 1º login de conta antiga): o código enviado ao
    contato oficial. Confirmou = a conta passa a valer e recebe a sessão."""
    d = request.get_json(silent=True) or {}
    cpf = _so_digitos(d.get("cpf"))
    if not _limite_ok("confirmar:" + _ip(), 30, 3600):
        return _muitas()
    cli = _cliente_por_cpf(cpf)
    if not cli:
        return jsonify({"erro": "Código expirado — peça um novo."}), 400
    erro = _conferir_codigo(cli, "verificar", d.get("codigo"))
    if erro:
        return jsonify({"erro": erro}), 401
    _, _, origem = _contatos_oficiais(cpf, cli)
    agora = datetime.now(timezone.utc).isoformat()
    _spatch(f"oct_cashback_clientes?cpf=eq.{cpf}",
            {"verificado_em": agora, "verificado_via": "codigo-" + origem, "verif_codigo": None,
             "verif_expira": None, "verif_tentativas": 0, "login_falhas": 0, "login_bloqueado_ate": None})
    cli["verificado_via"] = "codigo-" + origem
    # só agora o cliente entra no cadastro do posto (elegível ao cashback) — se o posto está ligado
    posto = cli.get("empresa_origem")
    if posto and _cashback_ligado(posto):
        _garantir_pessoa(posto, cli, cli)
    return jsonify({"ok": True, "token": _token_do_cliente(cli), "nome": cli.get("nome")})


@bp_cashback.route("/cashback/api/cadastro/reenviar", methods=["POST"])
def api_cadastro_reenviar():
    d = request.get_json(silent=True) or {}
    cpf = _so_digitos(d.get("cpf"))
    if not _limite_ok("reenviar:" + _ip(), 10, 3600):
        return _muitas()
    cli = _cliente_por_cpf(cpf)
    if not cli or cli.get("verificado_em"):
        return jsonify({"ok": True, "destino": None})
    ok, destino, _origem, erro = _enviar_codigo(cli, "verificar")
    if not ok:
        return jsonify({"erro": erro}), 429 if "Espere" in (erro or "") or "Muitos" in (erro or "") else 502
    return jsonify({"ok": True, "destino": destino})


@bp_cashback.route("/cashback/api/conta/sair-todos", methods=["POST"])
def api_conta_sair_todos():
    """Encerra a sessão em TODOS os aparelhos (celular perdido, senha vazada)."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    _spatch(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}", {"token_versao": int(cli.get("token_versao") or 0) + 1})
    return jsonify({"ok": True})


@bp_cashback.route("/cashback/api/conta/excluir", methods=["POST"])
def api_conta_excluir():
    """Excluir a conta pelo próprio app (exigência da App Store e do Google Play; LGPD).
    Apaga a conta do app e a foto do rosto e desliga o cashback no cadastro dos postos.
    Fica o que é registro de venda e de pagamento (cashbacks pagos, acionamentos, títulos):
    obrigação fiscal e financeira do posto. Pede a senha de novo."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    d = request.get_json(silent=True) or {}
    if not _confere_senha(str(d.get("senha") or ""), cli.get("senha_hash") or ""):
        return jsonify({"erro": "Senha incorreta"}), 401
    cpf = cli["cpf"]
    try:
        url, _ = _supa()
        if cli.get("foto_path"):
            rq.delete(f"{url}/storage/v1/object/{BUCKET_FACES}", headers=_st_headers({"Content-Type": "application/json"}),
                      json={"prefixes": [cli["foto_path"]]}, timeout=20)
        _spatch(f"oct_pessoas?documento=eq.{cpf}&cashback_ativo=eq.true", {"cashback_ativo": False})
        rq.delete(f"{url}/rest/v1/oct_app_dispositivos?cliente_cpf=eq.{cpf}", headers=_sh(), timeout=20)
        _checa(rq.delete(f"{url}/rest/v1/oct_cashback_clientes?cpf=eq.{cpf}", headers=_sh(), timeout=20))
    except Exception as e:
        return jsonify({"erro": "não consegui excluir agora: " + str(e)[:120]}), 500
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# CHAVE GERAL do cashback por posto (pedido Ronan 20/08):
# oct_empresas.cashback_ativo. Sem TRUE explícito o posto está DESLIGADO —
# não lista no seletor, não cadastra elegibilidade, não aceita acionamento.
# Fail-safe: erro na consulta = ninguém ligado.
# ------------------------------------------------------------------
_CB_LIGADO_CACHE = {"ts": 0.0, "ids": set()}


def _cashback_ligados():
    agora = time.time()
    if agora - _CB_LIGADO_CACHE["ts"] > 60:
        try:
            rows = _sget("oct_empresas?cashback_ativo=eq.true&ativo=eq.true&select=id")
            _CB_LIGADO_CACHE.update(ts=agora, ids={r["id"] for r in rows})
        except Exception:
            _CB_LIGADO_CACHE.update(ts=agora, ids=set())
    return _CB_LIGADO_CACHE["ids"]


def _cashback_ligado(empresa_id):
    return empresa_id in _cashback_ligados()


# ------------------------------------------------------------------
# APIs
# ------------------------------------------------------------------
@bp_cashback.route("/cashback/api/postos", methods=["GET"])
def api_postos():
    try:
        rows = _sget("oct_empresas?ativo=eq.true&select=id,nome,nome_fantasia&order=nome")
        ligados = _cashback_ligados()
        rows = [r for r in rows if r["id"] in ligados]
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
    if not _limite_ok("cadastro:" + _ip(), 10, 3600):
        return _muitas()
    try:
        existe = _cliente_por_cpf(cpf)
        if existe and existe.get("verificado_em"):
            return jsonify({"erro": "CPF já cadastrado — use 'Entrar' com sua senha "
                                    "(ou 'Esqueci minha senha')."}), 409
        if existe:
            # cadastro iniciado e não confirmado: só pode ser refeito depois de 30 min
            # (quem digitou o CPF de outra pessoa não trava o dono por mais que isso)
            try:
                criado = datetime.fromisoformat(str(existe.get("criado_em")).replace("Z", "+00:00"))
            except Exception:
                criado = datetime.now(timezone.utc) - timedelta(days=1)
            if datetime.now(timezone.utc) - criado < timedelta(minutes=30):
                return jsonify({"erro": "Este CPF já tem um cadastro esperando confirmação. Use o código que "
                                        "enviamos, ou tente de novo em 30 minutos."}), 409
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
        reg["verificado_em"] = None
        if existe:
            # refaz o cadastro que ninguém confirmou (dados novos, senha nova)
            _spatch(f"oct_cashback_clientes?cpf=eq.{cpf}", {**reg, "token_versao": int(existe.get("token_versao") or 0) + 1})
        else:
            _spost("oct_cashback_clientes", reg, prefer="return=minimal")
        cli = _cliente_por_cpf(cpf)
        # a conta só passa a valer com o código enviado ao contato OFICIAL (auditoria #17/#48);
        # a entrada no cadastro do posto acontece na confirmação (api_cadastro_confirmar)
        ok, destino, origem, erro = _enviar_codigo(cli, "verificar")
        return jsonify({"ok": True, "verificar": True, "nome": nome, "destino": destino,
                        "contato_do_posto": origem == "posto", "erro_envio": None if ok else erro})
    except Exception as e:
        return jsonify({"erro": "falha no cadastro: " + str(e)[:200]}), 500


def _garantir_pessoa(empresa_id, cad, cli=None):
    """Garante o cliente em oct_pessoas do posto (o PDV usa essa tabela p/ elegibilidade
    do cashback: cashback_ativo + chave_pix).

    05/10/2026 (auditoria #48): o app NÃO sobrescreve mais o cadastro do posto. Antes,
    todo acionamento trocava nome, telefone, e-mail, endereço e chave Pix da pessoa pelo
    que veio do app — quem cadastrasse o CPF de outra pessoa passava a receber a cobrança
    e o cashback dela. Agora: pessoa que não existe nasce com os dados do app; pessoa que
    já existe só ganha o que está VAZIO, e a chave Pix só muda se a conta foi confirmada
    pelo contato que o próprio posto tem (verificado_via 'codigo-posto')."""
    try:
        ex = _sget(f"oct_pessoas?empresa_id=eq.{empresa_id}&documento=eq.{cad['cpf']}"
                   f"&select=id,telefone,whatsapp,email,chave_pix,endereco,num_endereco,bairro,cidade,cep,uf,cashback_ativo"
                   f"&limit=1")
        dados = {
            "telefone": cad.get("telefone"), "whatsapp": cad.get("telefone"), "email": cad.get("email"),
            "endereco": cad.get("endereco"), "num_endereco": cad.get("numero"),
            "bairro": cad.get("bairro"), "cidade": cad.get("cidade"),
            "cep": cad.get("cep"), "uf": cad.get("uf"),
        }
        if ex:
            p = ex[0]
            corpo = {k: v for k, v in dados.items() if v and not p.get(k)}
            if not p.get("cashback_ativo"):
                corpo["cashback_ativo"] = True
            confirmado_pelo_posto = str((cli or {}).get("verificado_via") or "").endswith("posto")
            if cad.get("chave_pix") and (not p.get("chave_pix") or confirmado_pelo_posto) \
                    and p.get("chave_pix") != cad.get("chave_pix"):
                corpo["chave_pix"] = cad["chave_pix"]
            if corpo:
                _spatch(f"oct_pessoas?id=eq.{p['id']}", corpo)
            return p["id"]
        corpo = {**dados, "nome": cad["nome"], "documento": cad["cpf"], "chave_pix": cad.get("chave_pix"),
                 "cashback_ativo": True, "ativo": True,
                 "empresa_id": empresa_id, "tipo": "cliente", "tipo_pessoa": "fisica"}
        novo = _spost("oct_pessoas", corpo)
        return novo[0]["id"] if novo else None
    except Exception:
        return None


@bp_cashback.route("/cashback/api/login", methods=["POST"])
def api_login():
    d = request.get_json(silent=True) or {}
    cpf = _so_digitos(d.get("cpf"))
    if not _limite_ok("login:" + _ip(), 20, 600):
        return _muitas()
    try:
        cli = _cliente_por_cpf(cpf) if _cpf_valido(cpf) else None
    except Exception as e:
        return jsonify({"erro": "serviço indisponível: " + str(e)[:120]}), 500
    agora = int(time.time())
    if cli and int(cli.get("login_bloqueado_ate") or 0) > agora:
        minutos = max(1, (int(cli["login_bloqueado_ate"]) - agora + 59) // 60)
        return jsonify({"erro": f"Muitas senhas erradas seguidas. Tente de novo em {minutos} min "
                                "ou use 'Esqueci minha senha'."}), 429
    if not cli or not _confere_senha(str(d.get("senha") or ""), cli.get("senha_hash") or ""):
        if cli:
            falhas = int(cli.get("login_falhas") or 0) + 1
            corpo = {"login_falhas": falhas}
            if falhas >= LOGIN_MAX_FALHAS:
                corpo = {"login_falhas": 0, "login_bloqueado_ate": agora + LOGIN_BLOQUEIO_SEG}
            try:
                _spatch(f"oct_cashback_clientes?cpf=eq.{cpf}", corpo)
            except Exception:
                pass
        return jsonify({"erro": "CPF ou senha incorretos"}), 401
    if cli.get("login_falhas") or cli.get("login_bloqueado_ate"):
        _spatch(f"oct_cashback_clientes?cpf=eq.{cpf}", {"login_falhas": 0, "login_bloqueado_ate": None})
    if not cli.get("verificado_em"):
        # conta anterior a 05/10/2026 (ou cadastro não confirmado): confirma uma vez pelo código
        ok, destino, origem, erro = _enviar_codigo(cli, "verificar")
        return jsonify({"ok": True, "verificar": True, "nome": cli["nome"], "destino": destino,
                        "contato_do_posto": origem == "posto", "erro_envio": None if ok else erro})
    return jsonify({"ok": True, "token": _token_do_cliente(cli), "nome": cli["nome"]})


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
        # app Postos SN: dados do próprio cliente para a tela Perfil
        "cpf_mascarado": _cpf_mascara(cli["cpf"]), "email": cli.get("email"), "telefone": cli.get("telefone"),
        "posto_favorito": cli.get("posto_favorito"), "aceita_marketing": bool(cli.get("aceita_marketing")),
        "codigo": cli.get("id") and str(cli["id"])[:6].upper(),
        "proxima_liberacao": prox,
        "tem_facial": bool(cli.get("foto_path") and cli.get("lgpd_aceito_em")),
        "acionamento": (ac[0] if ac else None),
        "cashbacks": [{
            "valor": c.get("valor_cashback"), "litros": c.get("litros"), "status": c.get("status"),
            "quando": c.get("pago_em") or c.get("criado_em"), "cupom": c.get("numero_nfe"),
            "posto": nomes.get(c.get("empresa_id"), ""),
        } for c in cbs],
    })


@bp_cashback.route("/cashback/api/termo", methods=["GET"])
def api_termo():
    return jsonify({"ok": True, "versao": TERMO_VERSAO, "texto": TERMO_TEXTO})


@bp_cashback.route("/cashback/api/facial", methods=["POST"])
def api_facial():
    """Foto de REFERÊNCIA do rosto + aceite do termo (LGPD). Habilita a compra a prazo pelo app
    (o prazo em si continua dependendo da liberação do posto)."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    d = request.get_json(silent=True) or {}
    if d.get("aceite") is not True:
        return jsonify({"erro": "É preciso aceitar o termo para cadastrar a foto."}), 400
    try:
        foto = _foto_bytes(d.get("foto"))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    try:
        caminho = f"cadastro/{cli['cpf']}.jpg"
        _st_upload(caminho, foto)
        agora = datetime.now(timezone.utc).isoformat()
        _spatch(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}",
                {"foto_path": caminho, "foto_em": agora, "lgpd_aceito_em": agora, "lgpd_versao": TERMO_VERSAO})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"erro": "não consegui guardar a foto: " + str(e)[:160]}), 500


@bp_cashback.route("/cashback/api/pdv/assinatura", methods=["GET"])
def api_pdv_assinatura():
    """Para o CAIXA conferir quem autorizou a compra a prazo: foto do cadastro x selfie da
    autorização (links de 10 min) + nome, CPF mascarado e código. Só com operador logado."""
    if not _operador_logado():
        return jsonify({"erro": "entre no sistema para ver a foto"}), 401
    ac_id = _uuid_ok(request.args.get("acionamento"))
    if not ac_id:
        return jsonify({"erro": "acionamento inválido"}), 400
    try:
        acs = _sget(f"oct_cashback_acionamentos?id=eq.{ac_id}"
                    f"&select=id,empresa_id,cliente_cpf,cliente_nome,assinante_nome,selfie_path,selfie_em,"
                    f"auth_codigo,forma,placa,km&limit=1")
    except Exception as e:
        return jsonify({"erro": "banco: " + str(e)[:120]}), 500
    if not acs:
        return jsonify({"erro": "acionamento não encontrado"}), 404
    ac = acs[0]
    if not ac.get("auth_codigo") or not ac.get("selfie_path"):
        return jsonify({"ok": True, "assinado": False})
    cad = None
    try:
        cl = _sget(f"oct_cashback_clientes?cpf=eq.{ac['cliente_cpf']}&select=foto_path,foto_em&limit=1")
        cad = cl[0] if cl else None
    except Exception:
        cad = None
    return jsonify({
        "ok": True, "assinado": True, "acionamento": ac["id"],
        "assinante": ac.get("assinante_nome") or ac.get("cliente_nome"),
        # só o CPF mascarado (auditoria #136): o completo ia junto e ninguém usava
        "cpf_mascarado": _cpf_mascara(ac.get("cliente_cpf")),
        "auth_codigo": ac["auth_codigo"], "selfie_em": ac.get("selfie_em"), "selfie_path": ac["selfie_path"],
        "selfie_url": _st_assinar(ac["selfie_path"]),
        "cadastro_url": _st_assinar((cad or {}).get("foto_path")),
        "cadastro_em": (cad or {}).get("foto_em"),
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
    # CHAVE GERAL do cashback. 05/10/2026 (app Postos SN): o acionamento também IDENTIFICA
    # o cliente na venda, e é isso que dá os PONTOS (1 por R$ 1). Posto com cashback
    # desligado agora aceita o acionamento — só não gera cashback (o PDV já não gera:
    # a trava dele é oct_parametros 'cashback' / oct_empresas.cashback_ativo no gateway).
    com_cashback = forma != "05" and _cashback_ligado(empresa)
    aviso = None
    if forma != "05" and not com_cashback:
        aviso = "Este posto não está com cashback agora — mas você ganha os pontos da compra."
    # A PRAZO: só com liberação do POSTO (revalida no servidor); se o cliente é
    # colaborador de EMPRESA, a conta (e o cupom) é da empresa
    conta_prazo = None
    selfie = None
    if forma == "05":
        ok, motivo, conta_prazo = _prazo_liberado(empresa, cli["cpf"])
        if not ok:
            return jsonify({"erro": motivo}), 403
        if not _prazo_conta_confirmada(empresa, cli):
            return jsonify({"erro": "Para comprar a prazo pelo app, o seu WhatsApp precisa ser o mesmo do "
                                    "cadastro do posto — fale com o caixa para conferir."}), 403
        # A PRAZO pelo app = assinatura por SELFIE (antes de abastecer). Sem foto de referência
        # + termo aceito, ou sem a selfie desta compra, não aciona: a compra é feita no caixa.
        if not (cli.get("foto_path") and cli.get("lgpd_aceito_em")):
            return jsonify({"erro": "Para comprar a prazo pelo app, aceite o termo e cadastre a foto do seu rosto.",
                            "precisa_facial": True}), 428
        try:
            selfie = _foto_bytes(d.get("selfie"))
        except ValueError as e:
            return jsonify({"erro": str(e), "precisa_selfie": True}), 400
    # janela 2h (regra do CASHBACK — não vale pro A PRAZO, que pode repetir no dia)
    if forma != "05":
        corte2h = (datetime.now(timezone.utc) - timedelta(seconds=JANELA_2H_SEG)).isoformat()
        try:
            rec = _sget("oct_cashback?chave_pix=eq." + rq.utils.quote(cli.get("chave_pix") or "", safe="")
                        + "&status=in.(pendente,processando,pago)"
                        + f"&criado_em=gte.{rq.utils.quote(corte2h, safe='')}&select=criado_em&limit=1")
            if rec and com_cashback:
                # janela de 2h é do CASHBACK; o acionamento segue valendo para os pontos
                lib = datetime.fromisoformat(str(rec[0]["criado_em"]).replace("Z", "+00:00")) + timedelta(seconds=JANELA_2H_SEG)
                aviso = ("Você já recebeu cashback nas últimas 2 horas: o próximo libera às "
                         + lib.astimezone(timezone(timedelta(hours=-3))).strftime("%H:%M")
                         + ". Esta compra vale os pontos.")
        except Exception:
            pass
    try:
        # expira acionamentos antigos ainda aguardando
        _spatch(f"oct_cashback_acionamentos?cliente_cpf=eq.{cli['cpf']}&status=eq.aguardando",
                {"status": "expirado"})
        pessoa_id = _garantir_pessoa(empresa, {
            "cpf": cli["cpf"], "nome": cli["nome"], "telefone": cli.get("telefone"),
            "email": cli.get("email"), "chave_pix": cli.get("chave_pix"),
        }, cli)
        reg = {
            "empresa_id": empresa, "cliente_cpf": cli["cpf"], "cliente_nome": cli["nome"],
            "pessoa_id": pessoa_id, "combustivel": comb or None, "forma": forma,
            "status": "aguardando",
        }
        # frota: a CONTA (cliente da venda/cupom) é a EMPRESA vinculada
        if conta_prazo and conta_prazo.get("empresa"):
            reg["pessoa_id"] = conta_prazo["pessoa_id"]
            reg["cliente_nome"] = f"{conta_prazo['nome']} (por {cli['nome'].split(' ')[0]})"
        if bico:
            reg["bico"] = bico
        itens = _sanear_itens(d.get("itens"))
        if itens:
            reg["itens"] = itens
        # regras do A PRAZO: combustível vazio + sem bico = SÓ PRODUTOS (exige itens);
        # combustível escolhido = vai abastecer (exige o bico)
        if forma == "05" and not bico:
            if comb:
                return jsonify({"erro": "Escolheu o combustível: informe o número do BICO "
                                        "(ou deixe o combustível vazio para só produtos)."}), 400
            if not any(i.get("tipo") == "produto" for i in itens):
                return jsonify({"erro": "Adicione produtos ao carrinho — ou informe o bico se for abastecer."}), 400
        placa = re.sub(r"[^A-Za-z0-9]", "", str(d.get("placa") or "")).upper()[:8]
        if placa:
            reg["placa"] = placa
        try:
            km = int(float(str(d.get("km") or "").replace(",", ".") or 0)) or None
        except (TypeError, ValueError):
            km = None
        if km:
            reg["km"] = km
        if selfie:
            # a selfie sobe ANTES do acionamento existir: o PDV nunca vê um a prazo sem a foto
            agora_s = datetime.now(timezone.utc)
            ac_id = str(uuid.uuid4())
            caminho_s = f"compras/{agora_s.strftime('%Y-%m-%d')}/{ac_id}.jpg"
            _st_upload(caminho_s, selfie)
            hash_s = hashlib.sha256(selfie).hexdigest()
            ip = (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
            reg.update({
                "id": ac_id, "selfie_path": caminho_s, "selfie_em": agora_s.isoformat(), "selfie_hash": hash_s,
                "auth_codigo": _auth_codigo(ac_id, cli["cpf"], hash_s), "assinante_nome": cli["nome"][:120],
                "auth_ip": ip[:60], "auth_aparelho": (request.headers.get("User-Agent") or "")[:200],
            })
        try:
            novo = _spost("oct_cashback_acionamentos", reg)
        except RuntimeError as e:
            if "Could not find the" not in str(e) or selfie:
                raise                                    # com selfie as colunas novas são obrigatórias
            for c in ("bico", "itens", "placa", "km"):   # tabela sem alguma coluna nova
                reg.pop(c, None)
            novo = _spost("oct_cashback_acionamentos", reg)
        if selfie:
            _faces_limpar()
        return jsonify({"ok": True, "acionamento": (novo[0] if novo else None),
                        "validade_min": ACIONAMENTO_VALIDADE_MIN, "cashback": com_cashback, "aviso": aviso})
    except Exception as e:
        return jsonify({"erro": "falha ao acionar: " + str(e)[:200]}), 500


@bp_cashback.route("/cashback/api/prazo-status", methods=["GET"])
def api_prazo_status():
    """O cliente logado pode comprar A PRAZO neste posto? (mostra/esconde a opção)"""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    posto = _uuid_ok(request.args.get("posto"))
    if not posto:
        return jsonify({"ok": True, "prazo": False})
    ok, motivo, conta = _prazo_liberado(posto, cli["cpf"])
    return jsonify({"ok": True, "prazo": ok, "motivo": motivo,
                    "empresa": (conta or {}).get("nome") if (conta or {}).get("empresa") else None})


@bp_cashback.route("/cashback/api/frota", methods=["GET"])
def api_frota():
    """Placas disponíveis pro colaborador logado neste posto.
    Regra: veículo COM motorista definido só aparece pro próprio motorista;
    veículo SEM motorista aparece pra qualquer colaborador da empresa."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    posto = _uuid_ok(request.args.get("posto"))
    if not posto:
        return jsonify([])
    try:
        p = _sget(f"oct_pessoas?empresa_id=eq.{posto}&documento=eq.{cli['cpf']}"
                  f"&select=id,frota_empresa_id&limit=1")
        if not p:
            return jsonify([])
        pessoa_id = p[0]["id"]
        dono = p[0].get("frota_empresa_id") or pessoa_id   # frota da empresa OU pessoal
        veics = _sget(f"oct_frota_veiculos?pessoa_id=eq.{dono}&ativo=eq.true"
                      f"&select=placa,veiculo,motorista_pessoa_id&order=placa")
        out = []
        for v in veics:
            m = v.get("motorista_pessoa_id")
            if m and m != pessoa_id:
                continue   # placa de outro motorista: não aparece
            out.append({"placa": v["placa"], "veiculo": v.get("veiculo") or "",
                        "minha": bool(m)})
        return jsonify(out)
    except Exception:
        return jsonify([])


@bp_cashback.route("/cashback/api/produtos", methods=["GET"])
def api_produtos():
    """Busca de produtos de LOJA do posto (p/ o carrinho da compra a prazo).
    Combustível de bomba fica de fora (tanque_id é dele)."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    posto = _uuid_ok(request.args.get("posto"))
    q = str(request.args.get("q") or "").strip()
    if not posto or len(q) < 2:
        return jsonify([])
    try:
        termo = rq.utils.quote(f"*{q}*", safe="")
        rows = _sget(f"oct_produtos?empresa_id=eq.{posto}&ativo=eq.true&tanque_id=is.null"
                     f"&or=(nome.ilike.{termo},codigo.ilike.{termo})"
                     f"&select=id,nome,codigo,preco_venda_a&order=nome&limit=12")
        return jsonify([{"id": r["id"], "nome": r["nome"], "codigo": r.get("codigo"),
                         "preco": float(r.get("preco_venda_a") or 0)} for r in rows
                        if float(r.get("preco_venda_a") or 0) > 0])
    except Exception as e:
        return jsonify({"erro": str(e)[:120]}), 500


def _sanear_itens(brutos):
    """Itens do carrinho do app: no máximo 20, tipos conhecidos, números válidos."""
    itens = []
    for it in (brutos or [])[:20]:
        if not isinstance(it, dict):
            continue
        if it.get("tipo") == "bico":
            try:
                b = int(it.get("bico") or 0)
            except (TypeError, ValueError):
                continue
            if b > 0:
                itens.append({"tipo": "bico", "bico": b})
        elif it.get("tipo") == "produto":
            pid = _uuid_ok(it.get("produto_id"))
            if not pid:
                continue
            try:
                qtd = round(float(it.get("qtd") or 1), 3)
            except (TypeError, ValueError):
                qtd = 1
            itens.append({"tipo": "produto", "produto_id": pid,
                          "nome": str(it.get("nome") or "")[:80],
                          "qtd": max(qtd, 0.001),
                          "preco": round(float(it.get("preco") or 0), 2)})
    return itens


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
            ("id", "status", "bico", "combustivel", "forma", "criado_em", "venda_numero", "itens", "placa", "km",
             "auth_codigo", "selfie_em")}}
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
# APP POSTOS SN (05/10/2026): vitrine (ofertas e parceiros), celular e notificações.
# Ofertas e parceiros são públicos (é publicidade); o resto exige a sessão do cliente.
# ------------------------------------------------------------------
def _vale_no_posto(linha, posto):
    emp = [e for e in (linha.get("empresa_ids") or []) if e]
    return not emp or not posto or posto in emp


@bp_cashback.route("/cashback/api/ofertas", methods=["GET"])
def api_ofertas():
    posto = _uuid_ok(request.args.get("posto"))
    agora = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        rows = _sget(f"oct_app_campanhas?ativo=eq.true&inicio=lte.{agora}&or=(fim.is.null,fim.gte.{agora})"
                     "&select=id,tipo,titulo,texto,imagem_url,link,itens,fim,destaque,ordem,empresa_ids"
                     "&order=ordem,inicio.desc&limit=60")
    except Exception as e:
        return jsonify({"erro": str(e)[:120]}), 500
    return jsonify([{k: r.get(k) for k in ("id", "tipo", "titulo", "texto", "imagem_url", "link", "itens",
                                           "fim", "destaque")}
                    for r in rows if _vale_no_posto(r, posto)])


@bp_cashback.route("/cashback/api/parceiros", methods=["GET"])
def api_parceiros():
    posto = _uuid_ok(request.args.get("posto"))
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = _sget(f"oct_app_parceiros?ativo=eq.true"
                     f"&and=(or(inicio.is.null,inicio.lte.{hoje}),or(fim.is.null,fim.gte.{hoje}))"
                     "&select=id,nome,logo_url,beneficio,descricao,endereco,cidade,telefone,link,empresa_ids"
                     "&order=ordem,nome&limit=100")
    except Exception as e:
        return jsonify({"erro": str(e)[:120]}), 500
    return jsonify([{k: v for k, v in r.items() if k != "empresa_ids"} for r in rows if _vale_no_posto(r, posto)])


@bp_cashback.route("/cashback/api/dispositivo", methods=["POST"])
def api_dispositivo():
    """O app registra o celular: token de notificação do Firebase e os DOIS consentimentos
    (avisos de serviço x publicidade — separados, como pede a LGPD). Chamado ao entrar e
    toda vez que a pessoa muda a escolha em Perfil › Notificações."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    d = request.get_json(silent=True) or {}
    token = str(d.get("token") or "").strip()
    plat = str(d.get("plataforma") or "").strip().lower()
    if len(token) < 20 or len(token) > 4096 or plat not in ("android", "ios", "web"):
        return jsonify({"erro": "aparelho inválido"}), 400
    agora = datetime.now(timezone.utc).isoformat()
    mkt = d.get("aceita_marketing") is True
    linha = {"cliente_cpf": cli["cpf"], "token": token, "plataforma": plat,
             "app_versao": str(d.get("app_versao") or "")[:30] or None,
             "aceita_avisos": d.get("aceita_avisos") is not False, "aceita_marketing": mkt,
             "aceita_marketing_em": agora if mkt else None,
             "posto_favorito": _uuid_ok(d.get("posto_favorito")) or cli.get("posto_favorito"),
             "ativo": True, "falhas": 0, "visto_em": agora}
    try:
        url, _ = _supa()
        _checa(rq.post(f"{url}/rest/v1/oct_app_dispositivos?on_conflict=token", json=linha, timeout=20,
                       headers=_sh({"Prefer": "resolution=merge-duplicates,return=minimal"})))
        if mkt != bool(cli.get("aceita_marketing")):
            _spatch(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}",
                    {"aceita_marketing": mkt, "aceita_marketing_em": agora})
    except Exception as e:
        return jsonify({"erro": "não consegui registrar o aparelho: " + str(e)[:120]}), 500
    return jsonify({"ok": True})


@bp_cashback.route("/cashback/api/dispositivo/sair", methods=["POST"])
def api_dispositivo_sair():
    """Ao sair da conta o app tira o celular da lista (para de receber notificação)."""
    d = request.get_json(silent=True) or {}
    token = str(d.get("token") or "").strip()
    if len(token) < 20:
        return jsonify({"ok": True})
    try:
        url, _ = _supa()
        rq.delete(f"{url}/rest/v1/oct_app_dispositivos?token=eq.{rq.utils.quote(token, safe='')}",
                  headers=_sh(), timeout=20)
    except Exception:
        pass
    return jsonify({"ok": True})


@bp_cashback.route("/cashback/api/notificacao/aberta", methods=["POST"])
def api_notificacao_aberta():
    """O cliente tocou na notificação: conta a abertura da campanha."""
    d = request.get_json(silent=True) or {}
    camp = _uuid_ok(d.get("campanha_id"))
    if not camp or not _limite_ok("aberta:" + _ip(), 60, 3600):
        return jsonify({"ok": True})
    cli = _cliente_do_token()
    try:
        if cli:
            env = _sget(f"oct_app_envios?campanha_id=eq.{camp}&cliente_cpf=eq.{cli['cpf']}&aberto_em=is.null"
                        "&select=id&limit=1")
            if not env:
                return jsonify({"ok": True})                 # já contada (ou não foi para ele)
            _spatch(f"oct_app_envios?id=eq.{env[0]['id']}", {"aberto_em": datetime.now(timezone.utc).isoformat()})
        c = _sget(f"oct_app_campanhas?id=eq.{camp}&select=push_aberturas")
        if c:
            _spatch(f"oct_app_campanhas?id=eq.{camp}", {"push_aberturas": int(c[0].get("push_aberturas") or 0) + 1})
    except Exception:
        pass
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# APP POSTOS SN — telas do cliente (05/10/2026): postos da rede, histórico de compras,
# caixa de notificações e preferências do perfil.
# ------------------------------------------------------------------
_MINUSCULAS_NOME = {"de", "da", "do", "das", "dos", "e"}


def _nome_bonito(nome):
    """Nome como o cliente lê: 'POSTO SEVEN BH' -> 'Posto Seven BH', 'RIBEIRAO DAS NEVES' ->
    'Ribeirao das Neves'. Só mexe no que veio TODO em maiúsculas (cadastro fiscal); palavra
    de até 2 letras fica como sigla (BH, SN), menos de/da/do/e."""
    nome = str(nome or "").strip()
    if not nome or not nome.isupper():
        return nome
    out = []
    for i, p in enumerate(nome.split()):
        if i and p.lower() in _MINUSCULAS_NOME:
            out.append(p.lower())
        elif len(p) <= 2 and p.isalpha():
            out.append(p)
        else:
            out.append(p.capitalize())
    return " ".join(out)


@bp_cashback.route("/cashback/api/rede/postos", methods=["GET"])
def api_rede_postos():
    """Postos da rede para o app (lista, distância, posto favorito). Público."""
    try:
        rows = _sget("oct_empresas?ativo=eq.true&select=id,nome,nome_fantasia,endereco,cidade,uf,latitude,longitude,"
                     "app_foto_url,cashback_ativo&order=nome_fantasia")
    except Exception as e:
        return jsonify({"erro": str(e)[:120]}), 500
    out = []
    for r in rows:
        nome = r.get("nome_fantasia") or r.get("nome") or "Posto"
        if re.search(r"BANCADA|TESTE", nome, re.I):
            continue
        out.append({"id": r["id"], "nome": _nome_bonito(nome),
                    "endereco": r.get("endereco"), "cidade": _nome_bonito(r.get("cidade")), "uf": r.get("uf"),
                    "latitude": r.get("latitude"), "longitude": r.get("longitude"), "foto_url": r.get("app_foto_url"),
                    "cashback": bool(r.get("cashback_ativo"))})
    return jsonify(out)


@bp_cashback.route("/cashback/api/historico", methods=["GET"])
def api_historico():
    """Últimas compras do cliente (vendas do PDV com o CPF dele)."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    try:
        rows = _sget(f"oct_pdv_vendas?cliente_cpf=eq.{cli['cpf']}&status=eq.concluida"
                     "&select=id,empresa_id,data_venda,created_at,valor_total,itens,numero&order=created_at.desc&limit=30")
    except Exception as e:
        return jsonify({"erro": str(e)[:120]}), 500
    nomes = _nomes_postos()
    out = []
    for v in rows:
        itens = []
        for it in (v.get("itens") or []):
            if not isinstance(it, dict):
                continue
            q = it.get("qtd") or it.get("litros")
            itens.append({"desc": str(it.get("desc") or it.get("nome") or "")[:60],
                          "tipo": it.get("tipo"), "qtd": q, "total": it.get("total")})
        out.append({"id": v["id"], "posto": nomes.get(v.get("empresa_id"), ""), "empresa_id": v.get("empresa_id"),
                    "quando": v.get("data_venda") or v.get("created_at"), "valor": v.get("valor_total"),
                    "cupom": v.get("numero"), "itens": itens})
    return jsonify(out)


@bp_cashback.route("/cashback/api/notificacoes", methods=["GET"])
def api_notificacoes():
    """Caixa de entrada do sino: as notificações enviadas a este cliente."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    try:
        rows = _sget(f"oct_app_envios?cliente_cpf=eq.{cli['cpf']}&status=eq.ok"
                     "&select=id,campanha_id,tipo,titulo,texto,enviado_em,aberto_em&order=enviado_em.desc&limit=40")
    except Exception:
        rows = []
    vistos, out = set(), []
    for r in rows:                      # 2 celulares = 2 envios: mostra uma vez só
        chave = (r.get("campanha_id") or r["id"], r.get("titulo"))
        if chave in vistos:
            continue
        vistos.add(chave)
        out.append(r)
    return jsonify(out)


@bp_cashback.route("/cashback/api/perfil", methods=["POST"])
def api_perfil():
    """Preferências do cliente: posto favorito e o aceite de publicidade (LGPD: o avisos
    de serviço é outro consentimento, por aparelho, em /dispositivo)."""
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    d = request.get_json(silent=True) or {}
    corpo, disp = {}, {}
    if "posto_favorito" in d:
        corpo["posto_favorito"] = disp["posto_favorito"] = _uuid_ok(d.get("posto_favorito"))
    if "aceita_marketing" in d:
        mkt = d.get("aceita_marketing") is True
        agora = datetime.now(timezone.utc).isoformat()
        corpo.update({"aceita_marketing": mkt, "aceita_marketing_em": agora})
        disp.update({"aceita_marketing": mkt, "aceita_marketing_em": agora if mkt else None})
    if not corpo:
        return jsonify({"ok": True})
    try:
        _spatch(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}", corpo)
        if disp:
            _spatch(f"oct_app_dispositivos?cliente_cpf=eq.{cli['cpf']}", disp)
    except Exception as e:
        return jsonify({"erro": "não consegui salvar: " + str(e)[:120]}), 500
    return jsonify({"ok": True})


# ------------------------------------------------------------------
# PONTOS (05/10/2026): 1 ponto por R$ 1, valem 6 meses, saldo único na rede pelo CPF.
# A conta (saldo, troca, vencimento) é feita pelas funções do banco (SQL-APP-PONTOS.sql),
# que travam o CPF; o crédito das vendas é o worker app_pontos.py.
# ------------------------------------------------------------------
def _rpc(nome, args):
    url, _ = _supa()
    r = _checa(rq.post(f"{url}/rest/v1/rpc/{nome}", headers=_sh(), json=args, timeout=30))
    return r.json()


def _nomes_postos():
    try:
        return {e["id"]: _nome_bonito(e.get("nome_fantasia") or e.get("nome") or "Posto")
                for e in _sget("oct_empresas?select=id,nome,nome_fantasia")}
    except Exception:
        return {}


@bp_cashback.route("/cashback/api/pontos", methods=["GET"])
def api_pontos():
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    try:
        saldo = _rpc("oct_app_pontos_saldo", {"p_cpf": cli["cpf"]})
        limite = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        abertos = _sget(f"oct_app_pontos?cliente_cpf=eq.{cli['cpf']}&restante=gt.0&vence_em=lte.{limite}"
                        "&select=restante,vence_em&order=vence_em&limit=50")
        agora = datetime.now(timezone.utc).isoformat()
        a_vencer = sum(int(a["restante"]) for a in abertos if str(a["vence_em"]) > agora[:19])
        prox = next((a["vence_em"] for a in abertos if str(a["vence_em"]) > agora[:19]), None)
        mov = _sget(f"oct_app_pontos?cliente_cpf=eq.{cli['cpf']}&select=tipo,pontos,obs,criado_em,empresa_id,vence_em"
                    "&order=criado_em.desc&limit=60")
    except Exception as e:
        return jsonify({"erro": "pontos indisponíveis: " + str(e)[:120]}), 500
    nomes = _nomes_postos()
    return jsonify({"ok": True, "saldo": int(saldo or 0), "a_vencer_30d": a_vencer, "proximo_vencimento": prox,
                    "extrato": [{"tipo": m["tipo"], "pontos": m["pontos"], "obs": m.get("obs"),
                                 "quando": m["criado_em"], "posto": nomes.get(m.get("empresa_id"), "")}
                                for m in mov]})


@bp_cashback.route("/cashback/api/premios", methods=["GET"])
def api_premios():
    posto = _uuid_ok(request.args.get("posto"))
    try:
        rows = _sget("oct_app_premios?ativo=eq.true&select=id,nome,descricao,foto_url,pontos,categoria,empresa_ids,"
                     "estoque,destaque&order=destaque.desc,ordem,pontos&limit=200")
    except Exception as e:
        return jsonify({"erro": str(e)[:120]}), 500
    return jsonify([{k: v for k, v in r.items() if k != "empresa_ids"} | {"disponivel": r.get("estoque") is None
                                                                          or int(r["estoque"]) > 0}
                    for r in rows if _vale_no_posto(r, posto)])


@bp_cashback.route("/cashback/api/resgatar", methods=["POST"])
def api_resgatar():
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    d = request.get_json(silent=True) or {}
    premio, posto = _uuid_ok(d.get("premio_id")), _uuid_ok(d.get("posto"))
    if not premio or not posto:
        return jsonify({"erro": "Escolha o prêmio e o posto onde vai retirar."}), 400
    if not _limite_ok("resgatar:" + cli["cpf"], 10, 3600):
        return _muitas()
    try:
        r = _rpc("oct_app_resgatar", {"p_cpf": cli["cpf"], "p_nome": cli.get("nome"), "p_premio": premio,
                                      "p_empresa": posto})
    except Exception as e:
        return jsonify({"erro": "não consegui trocar agora: " + str(e)[:120]}), 500
    if not (r or {}).get("ok"):
        return jsonify({"erro": (r or {}).get("erro") or "não deu para trocar", "saldo": (r or {}).get("saldo")}), 409
    r["posto"] = _nomes_postos().get(posto, "posto")
    return jsonify(r)


@bp_cashback.route("/cashback/api/resgates", methods=["GET"])
def api_resgates():
    cli = _cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    rows = _sget(f"oct_app_resgates?cliente_cpf=eq.{cli['cpf']}&select=codigo,premio_nome,empresa_id,pontos,status,"
                 "criado_em,expira_em,usado_em&order=criado_em.desc&limit=30")
    nomes = _nomes_postos()
    return jsonify([{**r, "posto": nomes.get(r.get("empresa_id"), "")} for r in rows])


_OPERADOR_USER = {}


def _operador_do_posto(empresa_id):
    """Operador LOGADO (token do Supabase) que enxerga este posto. Devolve o nome/e-mail
    para registrar quem entregou, ou None."""
    tok = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    if len(tok) < 40 or not empresa_id:
        return None
    chave = (tok, empresa_id)
    c = _OPERADOR_USER.get(chave)
    if c and c[0] > time.time():
        return c[1]
    try:
        url, key = _supa()
        h = {"apikey": key, "Authorization": "Bearer " + tok, "Content-Type": "application/json"}
        u = rq.get(f"{url}/auth/v1/user", headers=h, timeout=10)
        if u.status_code != 200 or not (u.json() or {}).get("id"):
            return None
        # 10/10/2026: a regra do banco (oct_empresas_visiveis) só conhece o posto PRINCIPAL de quem
        # não é dono; o gerente com posto EXTRA liberado (oct_perfis.empresas, 08/10) seria recusado
        # ao dar baixa nele. Vale: cadastro ATIVO e (posto principal ou posto liberado); o dono
        # continua pela regra do banco (todas as empresas dele).
        perf = _sget(f"oct_perfis?id=eq.{u.json()['id']}&select=empresa_id,empresas,master,ativo&limit=1")
        p = perf[0] if perf else None
        if not p or p.get("ativo") is False:
            return None
        if p.get("master") is True:
            vis = rq.post(f"{url}/rest/v1/rpc/oct_empresas_visiveis", headers=h, json={}, timeout=10)
            ids = set()
            for x in (vis.json() if vis.status_code == 200 else []):
                ids.add(x if isinstance(x, str) else (x.get("oct_empresas_visiveis") or x.get("id")))
        else:
            ids = {str(e) for e in ([p.get("empresa_id")] + list(p.get("empresas") or [])) if e}
        if empresa_id not in ids:
            return None
        quem = (u.json().get("email") or u.json()["id"])[:80]
        if len(_OPERADOR_USER) > 500:
            _OPERADOR_USER.clear()
        _OPERADOR_USER[chave] = (time.time() + 300, quem)
        return quem
    except Exception:
        return None


@bp_cashback.route("/cashback/api/pdv/resgate", methods=["GET"])
def api_pdv_resgate_ver():
    """O caixa digita o código: mostra o prêmio ANTES de dar baixa."""
    empresa = _uuid_ok(request.args.get("empresa"))
    if not _operador_do_posto(empresa):
        return jsonify({"erro": "entre no sistema deste posto para conferir o código"}), 401
    cod = re.sub(r"[^A-Za-z0-9]", "", str(request.args.get("codigo") or "")).upper()[:10]
    rows = _sget(f"oct_app_resgates?codigo=eq.{cod}&order=criado_em.desc&limit=1"
                 "&select=codigo,cliente_nome,premio_nome,pontos,status,empresa_id,expira_em,usado_em,usado_por") if cod else []
    if not rows:
        return jsonify({"erro": "Código não encontrado."}), 404
    r = rows[0]
    nome = (r.get("cliente_nome") or "").split()
    return jsonify({"ok": True, "codigo": r["codigo"], "premio": r["premio_nome"], "pontos": r["pontos"],
                    "status": r["status"], "outro_posto": r["empresa_id"] != empresa, "expira_em": r["expira_em"],
                    "usado_em": r.get("usado_em"), "usado_por": r.get("usado_por"),
                    "cliente": " ".join(nome[:1] + [n[:1] + "." for n in nome[1:]])})   # "RONAN J."


@bp_cashback.route("/cashback/api/pdv/resgate/usar", methods=["POST"])
def api_pdv_resgate_usar():
    d = request.get_json(silent=True) or {}
    empresa = _uuid_ok(d.get("empresa"))
    quem = _operador_do_posto(empresa)
    if not quem:
        return jsonify({"erro": "entre no sistema deste posto para dar baixa"}), 401
    try:
        r = _rpc("oct_app_resgate_usar", {"p_codigo": str(d.get("codigo") or ""), "p_empresa": empresa,
                                          "p_operador": quem})
    except Exception as e:
        return jsonify({"erro": "não consegui dar baixa: " + str(e)[:120]}), 500
    return (jsonify(r), 200) if (r or {}).get("ok") else (jsonify(r), 409)


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
# PDF de QR codes dos bicos (impressão) — individual ou em massa.
#   GET /cashback/qr-pdf?p=<empresa>&bicos=3        (um bico)
#   GET /cashback/qr-pdf?p=<empresa>&bicos=1,2,5    (lista)
#   GET /cashback/qr-pdf?p=<empresa>                (TODOS os bicos do cadastro)
# ------------------------------------------------------------------
@bp_cashback.route("/cashback/qr-pdf", methods=["GET"])
def qr_pdf():
    import io
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
    posto = _uuid_ok(request.args.get("p"))
    if not posto:
        return jsonify({"erro": "parâmetro p (empresa) inválido"}), 400
    # bicos: da URL ou do cadastro (oct_bicos)
    brutos = _so_digitos_lista(request.args.get("bicos"))
    if not brutos:
        try:
            rows = _sget(f"oct_bicos?empresa_id=eq.{posto}&select=numero&order=numero")
            brutos = sorted({int(r["numero"]) for r in rows if r.get("numero") is not None})
        except Exception:
            brutos = []
    if not brutos:
        return jsonify({"erro": "nenhum bico encontrado (cadastre os bicos ou informe ?bicos=1,2,3)"}), 404
    # nome do posto (cabeçalho do cartão)
    nome_posto = "CASHBACK"
    try:
        e = _sget(f"oct_empresas?id=eq.{posto}&select=nome,nome_fantasia")
        if e:
            nome_posto = (e[0].get("nome_fantasia") or e[0].get("nome") or "CASHBACK")[:36].upper()
    except Exception:
        pass

    base_url = request.host_url.rstrip("/")
    if "localhost" not in base_url and "127.0.0.1" not in base_url:
        base_url = base_url.replace("http://", "https://")

    LARG, ALT, COLS, LINHAS = 1240, 1754, 2, 3   # A4 ~150dpi, 6 cartões/página
    try:
        F_TIT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 64)
        F_SUB = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 26)
    except Exception:
        F_TIT = F_SUB = ImageFont.load_default()
    paginas = []
    for p0 in range(0, len(brutos), COLS * LINHAS):
        pag = Image.new("RGB", (LARG, ALT), "white")
        dr = ImageDraw.Draw(pag)
        cw, ch = LARG // COLS, ALT // LINHAS
        for i, bico in enumerate(brutos[p0:p0 + COLS * LINHAS]):
            cx0, cy0 = (i % COLS) * cw, (i // COLS) * ch
            dr.rectangle([cx0 + 14, cy0 + 14, cx0 + cw - 14, cy0 + ch - 14], outline="#bbbbbb", width=2)
            qr = qrcode.make(f"{base_url}/cashback?p={posto}&bico={bico}", box_size=10, border=2).convert("RGB")
            qs = min(cw, ch) - 240
            pag.paste(qr.resize((qs, qs)), (cx0 + (cw - qs) // 2, cy0 + 64))
            t = nome_posto
            dr.text((cx0 + (cw - dr.textlength(t, font=F_SUB)) / 2, cy0 + 26), t, fill="#666666", font=F_SUB)
            t = f"BICO {bico}"
            dr.text((cx0 + (cw - dr.textlength(t, font=F_TIT)) / 2, cy0 + 64 + qs + 6), t, fill="black", font=F_TIT)
            t = "Cashback: aponte a camera e ative"
            dr.text((cx0 + (cw - dr.textlength(t, font=F_SUB)) / 2, cy0 + 64 + qs + 84), t, fill="#666666", font=F_SUB)
        paginas.append(pag)
    buf = io.BytesIO()
    paginas[0].save(buf, format="PDF", save_all=True, append_images=paginas[1:], resolution=150)
    nome_arq = f"qrcodes-bicos-{brutos[0]}" + (f"-a-{brutos[-1]}" if len(brutos) > 1 else "") + ".pdf"
    return Response(buf.getvalue(), mimetype="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{nome_arq}"'})


def _so_digitos_lista(txt):
    out = []
    for peca in str(txt or "").split(","):
        peca = re.sub(r"\D", "", peca)
        if peca:
            out.append(int(peca))
    return sorted(set(out))


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
  <button class="sec" onclick="mostrar('tela-esqueci')">🔐 Esqueci minha senha</button>
  <div class="msg" id="lg-msg"></div>
</div>

<!-- ESQUECI A SENHA -->
<div class="card esc" id="tela-esqueci">
  <div style="font-weight:700;margin-bottom:4px">Recuperar senha</div>
  <div class="sub">Enviamos um código de 6 dígitos para você criar uma senha nova.</div>
  <label>CPF</label><input id="es-cpf" inputmode="numeric" placeholder="000.000.000-00" maxlength="14">
  <label>Receber o código por</label>
  <div style="display:flex;gap:8px;margin:6px 0 10px">
    <button id="es-wpp" onclick="esqueciPedir('whatsapp')" style="flex:1">📱 WhatsApp</button>
    <button id="es-eml" class="sec" onclick="esqueciPedir('email')" style="flex:1">✉️ E-mail</button>
  </div>
  <div id="es-passo2" class="esc">
    <label>Código recebido</label><input id="es-codigo" inputmode="numeric" maxlength="6" placeholder="000000">
    <label>Nova senha (mín. 6)</label><input id="es-senha" type="password">
    <label>Repita a nova senha</label><input id="es-senha2" type="password">
    <button onclick="esqueciTrocar()">Salvar nova senha</button>
  </div>
  <button class="sec" onclick="mostrar('tela-login')">← Voltar</button>
  <div class="msg" id="es-msg"></div>
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

<!-- CONFIRMAR: código de 6 dígitos no WhatsApp que o posto tem (05/10/2026) -->
<div class="card esc" id="tela-confirmar">
  <div style="font-weight:700;margin-bottom:4px">Confirme que é você</div>
  <div class="sub" id="cf-txt">Enviamos um código de 6 dígitos para o seu WhatsApp.</div>
  <label>Código</label><input id="cf-codigo" inputmode="numeric" maxlength="6" placeholder="000000" autocomplete="one-time-code">
  <button onclick="confirmarCodigo()">Confirmar</button>
  <button class="sec" onclick="reenviarCodigo()">Reenviar o código</button>
  <button class="sec" onclick="mostrar('tela-login')">← Voltar</button>
  <div class="msg" id="cf-msg"></div>
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
      <select id="ac-forma" onchange="carrinhoVisibilidade()"><option value="01">Dinheiro</option><option value="17">PIX</option></select>

      <!-- PLACA + KM (compra a prazo: identifica o veículo) -->
      <!-- (sem display no inline: display:flex inline VENCE a classe .esc) -->
      <div id="veiculo-box" class="esc" style="margin-top:10px">
        <div id="frota-sel-box" class="esc">
          <label>Veículo da frota</label>
          <select id="ac-frota" onchange="frotaEscolheu()"><option value="">— escolha a placa —</option></select>
        </div>
        <div style="display:flex;gap:8px">
          <div style="flex:1.4"><label>Placa do veículo</label><input id="ac-placa" placeholder="ABC1D23" maxlength="8" style="text-transform:uppercase"></div>
          <div style="flex:1"><label>KM atual</label><input id="ac-km" inputmode="numeric" placeholder="km"></div>
        </div>
      </div>
      <div id="prazo-motivo" class="sub esc" style="margin-top:8px;color:#fbbf24"></div>

      <!-- CARRINHO da compra A PRAZO: mais abastecimentos + produtos de loja -->
      <div id="carrinho-box" class="esc" style="margin-top:12px;border:1px solid var(--borda);border-radius:9px;padding:10px 12px">
        <div style="font-weight:700;font-size:.86rem">🛒 Itens da compra</div>
        <div class="sub" style="margin:2px 0 8px">O abastecimento do bico acima já entra. Adicione outros itens se precisar.</div>
        <div id="carrinho-lista"></div>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button type="button" onclick="carrinhoAbrirBico()" style="margin:0;padding:9px;background:#1a2233;border:1px solid var(--borda);font-size:.82rem">⛽ + outro bico</button>
          <button type="button" onclick="carrinhoAbrirBusca()" style="margin:0;padding:9px;background:#1a2233;border:1px solid var(--borda);font-size:.82rem">🛍 + produto</button>
        </div>
        <div id="carrinho-bico-add" class="esc" style="margin-top:8px">
          <div style="display:flex;gap:8px">
            <input id="cb-bico-num" inputmode="numeric" maxlength="3" placeholder="nº do outro bico" style="flex:1">
            <button type="button" onclick="carrinhoConfirmarBico()" style="width:auto;margin:0;padding:0 18px;background:#16a34a">OK</button>
          </div>
        </div>
        <div id="carrinho-busca" class="esc" style="margin-top:8px">
          <input id="cb-q" placeholder="Digite o nome do produto (mín. 2 letras)" autocomplete="off">
          <div id="cb-res" style="margin-top:6px"></div>
        </div>
      </div>

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

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:baseline">
      <div style="font-weight:700">⭐ Meus pontos</div>
      <div class="grande" id="pt-saldo" style="font-size:1.3rem;color:#fbbf24">—</div>
    </div>
    <div class="sub" style="margin:2px 0 0">1 ponto a cada R$ 1 nas compras identificadas com o seu CPF. Cada ponto vale 6 meses.</div>
    <div id="pt-vencer" class="aviso esc"></div>
    <div id="pt-codigo" class="cta esc" style="text-align:center"></div>
    <button onclick="abrirPremios()">🎁 Trocar pontos</button>
    <div id="pt-premios" class="esc" style="margin-top:10px"></div>
    <div id="pt-resgates" style="margin-top:10px"></div>
    <button class="sec" onclick="document.getElementById('pt-extrato').classList.toggle('esc')">Ver extrato de pontos</button>
    <div id="pt-extrato" class="lista esc"></div>
    <div class="msg" id="pt-msg"></div>
  </div>

  <div class="card">
    <div style="font-weight:700;margin-bottom:6px">⚙️ Minha conta</div>
    <button class="sec" onclick="sairTodos()">Sair de todos os aparelhos</button>
    <button class="sec" onclick="document.getElementById('ex-box').classList.toggle('esc')">Excluir minha conta</button>
    <div id="ex-box" class="esc">
      <div class="sub" style="margin-top:8px">Apaga a sua conta do app e a foto do seu rosto e desliga o cashback. O que é registro de venda e de pagamento fica com o posto. Não dá para desfazer.</div>
      <label>Sua senha</label><input id="ex-senha" type="password">
      <button onclick="excluirConta()" style="background:#b91c1c">Excluir definitivamente</button>
    </div>
    <div class="msg" id="mc-msg"></div>
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

  <!-- FOTO DO ROSTO: referência do cadastro (com o termo) e selfie que autoriza cada compra a prazo -->
  <div id="face-overlay" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.94);z-index:9999;flex-direction:column;align-items:center;justify-content:center;padding:14px;overflow:auto">
    <div id="face-titulo" style="color:#fff;font-weight:700;margin-bottom:8px;text-align:center"></div>
    <div id="face-termo-box" style="display:none;max-width:400px;width:100%;margin-bottom:10px">
      <div id="face-termo" style="max-height:150px;overflow:auto;background:#0d1017;border:1px solid #232838;border-radius:9px;padding:10px;font-size:.74rem;color:#cbd5e1;white-space:pre-wrap;text-align:left"></div>
      <label style="display:flex;gap:8px;align-items:flex-start;margin-top:8px;font-size:.82rem;color:#fff;cursor:pointer"><input type="checkbox" id="face-aceite" style="width:auto;margin-top:3px"> Li e aceito o termo de uso da minha imagem.</label>
    </div>
    <video id="face-video" playsinline muted autoplay style="width:240px;height:240px;object-fit:cover;border-radius:50%;border:3px solid #f97316;transform:scaleX(-1);background:#000"></video>
    <div id="face-msg" class="sub" style="margin-top:10px;text-align:center;min-height:1.2em"></div>
    <button type="button" onclick="faceCapturar()" style="max-width:400px;background:#16a34a">📸 Tirar a foto</button>
    <button type="button" onclick="facePorArquivo()" style="max-width:400px;background:#1a2233;border:1px solid #232838">Não abriu? Usar a câmera do celular</button>
    <button type="button" onclick="faceFechar(null)" style="max-width:400px;background:#2a2d3e">Cancelar</button>
    <input id="face-arq" type="file" accept="image/*" capture="user" style="display:none">
  </div>
  <div id="js-erro" style="display:none;position:fixed;bottom:0;left:0;right:0;background:#7f1d1d;color:#fecaca;font-size:.72rem;padding:8px 12px;z-index:99999;word-break:break-all"></div>

  <div class="card esc" id="pwa-card">
    <div style="font-weight:700">📲 Vire um app no seu celular</div>
    <div class="sub" id="pwa-txt">Instale para abrir direto da tela inicial, como um aplicativo.</div>
    <button id="pwa-btn" class="esc" onclick="pwaInstalar()">Instalar o app</button>
  </div>
</div>

<div class="sub" style="text-align:center;margin-top:14px;opacity:.45">versão app-pontos-17</div>

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
// começa VAZIO: preenche pelo QR do bico ou manualmente. Vazio + sem bico ao
// acionar = compra SÓ DE PRODUTOS (vai direto pra emissão no caixa).
document.getElementById("ac-comb").innerHTML =
  '<option value="">— sem abastecimento (só produtos) —</option>' +
  COMBS.map(c=>`<option>${c}</option>`).join("");
const BICO_URL = (new URLSearchParams(location.search).get("bico")||"").replace(/\D/g,"");
if (BICO_URL) {
  document.getElementById("ac-bico").value = BICO_URL;
  setTimeout(()=>{ try{ carregarInfoBico(); }catch(e){} }, 400);   // ficha do bico do QR
}

function mostrar(id){["tela-login","tela-cad","tela-dash","tela-esqueci","tela-confirmar"].forEach(t=>document.getElementById(t).classList.toggle("esc",t!==id));}
function nomeForma(f){return f==="17"?"PIX":f==="05"?"A PRAZO":"Dinheiro";}
function tok(){return localStorage.getItem("cb_token")||"";}
function brl(v){return "R$ "+Number(v||0).toLocaleString("pt-BR",{minimumFractionDigits:2});}
// texto que vem do banco NUNCA entra cru no HTML (auditoria #225: nome de produto com
// <script> roubava a sessão de quem buscasse produtos)
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function mascaraCpf(el){el.addEventListener("input",()=>{let v=el.value.replace(/\D/g,"").slice(0,11);el.value=v.replace(/(\d{3})(\d)/,"$1.$2").replace(/(\d{3})(\d)/,"$1.$2").replace(/(\d{3})(\d{1,2})$/,"$1-$2");});}
mascaraCpf(document.getElementById("lg-cpf"));mascaraCpf(document.getElementById("cd-cpf"));mascaraCpf(document.getElementById("es-cpf"));

async function esqueciPedir(canal){
  const m=document.getElementById("es-msg");m.className="msg";m.textContent="Enviando o código…";
  const r=await req("/cashback/api/senha/pedir",{cpf:document.getElementById("es-cpf").value,canal:canal});
  if(r.ok){document.getElementById("es-passo2").classList.remove("esc");
    m.className="msg ok";m.textContent=r.destino?("Código enviado para "+r.destino+" — vale 15 minutos."):(r.aviso||"Se este CPF tiver cadastro, o código chega em instantes.");}
  else{m.className="msg erro";m.textContent=r.erro||"Falha no envio";}
}
async function esqueciTrocar(){
  const m=document.getElementById("es-msg");
  const s1=document.getElementById("es-senha").value,s2=document.getElementById("es-senha2").value;
  if(s1!==s2){m.className="msg erro";m.textContent="As senhas não conferem";return;}
  m.className="msg";m.textContent="Salvando…";
  const r=await req("/cashback/api/senha/trocar",{cpf:document.getElementById("es-cpf").value,
    codigo:document.getElementById("es-codigo").value,senha:s1});
  if(r.ok&&r.token){localStorage.setItem("cb_token",r.token);m.className="msg ok";
    m.textContent="Senha alterada! Entrando…";carregarDash();}
  else{m.className="msg erro";m.textContent=r.erro||"Falha ao trocar a senha";}
}

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
  else if(r.verificar){abrirConfirmar(document.getElementById("lg-cpf").value,r,false);}
  else{m.className="msg erro";m.textContent=r.erro||"Falha no login";}
}

// ---- CONFIRMAÇÃO do cadastro (código no WhatsApp que o posto tem de você) ----
let _CONF={cpf:"",novo:false};
function abrirConfirmar(cpf,r,novo){
  _CONF={cpf:cpf,novo:!!novo};
  mostrar("tela-confirmar");
  document.getElementById("cf-codigo").value="";
  document.getElementById("cf-txt").textContent=r.destino
    ?("Enviamos um código de 6 dígitos para "+r.destino+(r.contato_do_posto?" (o contato que o posto tem de você).":"."))
    :"Precisamos confirmar que é você antes de entrar.";
  const m=document.getElementById("cf-msg");
  m.className="msg"+(r.erro_envio?" erro":"");m.textContent=r.erro_envio||"";
}
async function confirmarCodigo(){
  const m=document.getElementById("cf-msg");m.className="msg";m.textContent="Conferindo…";
  const r=await req("/cashback/api/cadastro/confirmar",{cpf:_CONF.cpf,codigo:document.getElementById("cf-codigo").value});
  if(r.ok&&r.token){
    localStorage.setItem("cb_token",r.token);
    await carregarDash();
    // cadastro novo: último passo é o termo + foto do rosto (para comprar a prazo pelo app; pode pular)
    if(_CONF.novo)await facialCadastrar("Último passo: foto do seu rosto (para comprar a prazo pelo app)");
  } else {m.className="msg erro";m.textContent=r.erro||"Não consegui confirmar";}
}
async function reenviarCodigo(){
  const m=document.getElementById("cf-msg");m.className="msg";m.textContent="Enviando…";
  const r=await req("/cashback/api/cadastro/reenviar",{cpf:_CONF.cpf});
  if(r.ok){m.className="msg ok";m.textContent=r.destino?("Código reenviado para "+r.destino+"."):"Se o código não chegar, peça ajuda no caixa do posto.";}
  else{m.className="msg erro";m.textContent=r.erro||"Falha no envio";}
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
  if(r.verificar){m.textContent="";abrirConfirmar(document.getElementById("cd-cpf").value,r,true);}
  else{m.className="msg erro";m.textContent=r.erro||"Falha no cadastro";}
}

// ---- FOTO DO ROSTO (referência do cadastro e selfie de cada compra a prazo) ----
let TEM_FACIAL=false,_faceStream=null,_faceResolve=null;
function faceAbrir(titulo,comTermo){
  return new Promise(async resolve=>{
    _faceResolve=resolve;
    document.getElementById("face-titulo").textContent=titulo;
    document.getElementById("face-termo-box").style.display=comTermo?"block":"none";
    document.getElementById("face-aceite").checked=false;
    const msg=document.getElementById("face-msg");msg.textContent="Abrindo a câmera…";
    const ov=document.getElementById("face-overlay");
    if(ov.parentElement!==document.body)document.body.appendChild(ov);   // nasce dentro de uma tela que pode estar escondida
    ov.style.display="flex";
    if(comTermo){try{const t=await req("/cashback/api/termo");document.getElementById("face-termo").textContent=t.texto||"";}catch(e){}}
    try{
      if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia)throw new Error("sem câmera");
      _faceStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:"user",width:{ideal:640},height:{ideal:640}},audio:false});
      const v=document.getElementById("face-video");v.srcObject=_faceStream;try{await v.play();}catch(e){}
      msg.textContent="Enquadre o rosto no círculo e toque em tirar a foto.";
    }catch(e){msg.textContent="Não consegui abrir a câmera aqui — use o botão da câmera do celular.";}
  });
}
function _faceParar(){if(_faceStream){_faceStream.getTracks().forEach(t=>t.stop());_faceStream=null;}document.getElementById("face-video").srcObject=null;}
function faceFechar(valor){_faceParar();document.getElementById("face-overlay").style.display="none";const r=_faceResolve;_faceResolve=null;if(r)r(valor);}
function _faceAceiteOk(){
  if(document.getElementById("face-termo-box").style.display==="none")return true;
  if(document.getElementById("face-aceite").checked)return true;
  document.getElementById("face-msg").textContent="Marque que leu e aceita o termo.";return false;
}
function _faceReduzir(fonte,w,h){
  const esc=Math.min(1,640/Math.max(w,h)),c=document.createElement("canvas");
  c.width=Math.round(w*esc);c.height=Math.round(h*esc);
  c.getContext("2d").drawImage(fonte,0,0,c.width,c.height);
  return c.toDataURL("image/jpeg",0.82);
}
function faceCapturar(){
  if(!_faceAceiteOk())return;
  const v=document.getElementById("face-video");
  if(!_faceStream||!v.videoWidth){document.getElementById("face-msg").textContent="Câmera indisponível — use o botão da câmera do celular.";return;}
  faceFechar(_faceReduzir(v,v.videoWidth,v.videoHeight));
}
function facePorArquivo(){if(_faceAceiteOk())document.getElementById("face-arq").click();}
document.getElementById("face-arq").addEventListener("change",ev=>{
  const f=ev.target.files&&ev.target.files[0];ev.target.value="";if(!f)return;
  const u=URL.createObjectURL(f),im=new Image();
  im.onload=()=>{URL.revokeObjectURL(u);faceFechar(_faceReduzir(im,im.naturalWidth,im.naturalHeight));};
  im.onerror=()=>{URL.revokeObjectURL(u);document.getElementById("face-msg").textContent="Não consegui ler a foto — tente de novo.";};
  im.src=u;
});
// termo + foto de referência. true = cadastrou
async function facialCadastrar(titulo){
  const foto=await faceAbrir(titulo||"Foto do seu rosto para o cadastro",true);
  if(!foto)return false;
  const r=await req("/cashback/api/facial",{foto:foto,aceite:true});
  if(r.ok){TEM_FACIAL=true;return true;}
  alert(r.erro||"Não consegui salvar a foto.");
  return false;
}

function sair(){localStorage.removeItem("cb_token");mostrar("tela-login");}
async function sairTodos(){
  const m=document.getElementById("mc-msg");m.className="msg";m.textContent="Encerrando…";
  const r=await req("/cashback/api/conta/sair-todos",{});
  if(r.ok)sair(); else{m.className="msg erro";m.textContent=r.erro||"Falha";}
}
async function excluirConta(){
  const m=document.getElementById("mc-msg");m.className="msg";m.textContent="Excluindo…";
  const r=await req("/cashback/api/conta/excluir",{senha:document.getElementById("ex-senha").value});
  if(r.ok){
    localStorage.removeItem("cb_token");mostrar("tela-login");
    const lg=document.getElementById("lg-msg");lg.className="msg ok";lg.textContent="Sua conta foi excluída.";
  } else {m.className="msg erro";m.textContent=r.erro||"Não consegui excluir";}
}

async function carregarDash(){
  const r=await req("/cashback/api/me");
  if(!r.ok){sair();return;}
  mostrar("tela-dash");
  TEM_FACIAL=!!r.tem_facial;
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
    const soProd=!r.acionamento.bico&&!r.acionamento.combustivel;
    at.innerHTML="✅ <b>"+(r.acionamento.forma==="05"?"Compra A PRAZO acionada!":"Benefício acionado!")+"</b><br>"+
      (soProd
        ? ("🛍 Só produtos · " + nomeForma(r.acionamento.forma) + "<br>Retire seus produtos no caixa — a emissão sai sozinha.")
        : ((r.acionamento.bico?("Bico "+esc(r.acionamento.bico)+" · "):"")+
           esc(r.acionamento.combustivel||"Combustível") + " · " + nomeForma(r.acionamento.forma) +
           "<br>Vá até a bomba e abasteça — acompanhe abaixo."))+
      "<br><br><a href='#' onclick='cancelarAcionamento();return false'>cancelar</a>";
    ligarEspelho();
  } else {fm.classList.remove("esc");at.classList.add("esc");garantirPosto();verificarPrazo();}
  carregarPontos();
  const lst=document.getElementById("dh-lista");
  if(!(r.cashbacks||[]).length){lst.innerHTML='<div class="sub">Nenhum cashback ainda — abasteça para começar! 🚗</div>';}
  else lst.innerHTML=r.cashbacks.map(c=>{
    const cls=c.status==="pago"?"t-pago":(c.status==="pendente"||c.status==="processando")?"t-pendente":"t-outros";
    const rot=c.status==="pago"?"PAGO":(c.status==="pendente"||c.status==="processando")?"A RECEBER":String(c.status||"").toUpperCase();
    const q=c.quando?new Date(c.quando).toLocaleDateString("pt-BR")+" "+new Date(c.quando).toLocaleTimeString("pt-BR",{hour:"2-digit",minute:"2-digit"}):"";
    return `<div class="item"><div><b>${brl(c.valor)}</b> <span class="sub">· ${Number(c.litros||0).toFixed(1)} L${c.posto?" · "+esc(c.posto):""}</span><br><span class="sub">${q}</span></div><span class="tag ${cls}">${esc(rot)}</span></div>`;
  }).join("");
}

// ---- PONTOS (05/10/2026): 1 por R$ 1, valem 6 meses, troca por prêmio com código no caixa ----
let PT_SALDO=0;
const PT_TIPO={ganho:"Ganhou",estorno:"Voltou",resgate:"Trocou",vencido:"Venceu",cancelado:"Venda cancelada",ajuste:"Ajuste"};
async function carregarPontos(){
  const r=await req("/cashback/api/pontos");
  const sd=document.getElementById("pt-saldo");
  if(!r.ok){sd.textContent="—";return;}
  PT_SALDO=r.saldo||0;
  sd.textContent=PT_SALDO.toLocaleString("pt-BR")+" pts";
  const av=document.getElementById("pt-vencer");
  if(r.a_vencer_30d>0&&r.proximo_vencimento){
    av.classList.remove("esc");
    av.textContent="⏳ "+r.a_vencer_30d+" ponto(s) vencem nos próximos 30 dias (o primeiro em "+new Date(r.proximo_vencimento).toLocaleDateString("pt-BR")+"). Troque antes!";
  } else av.classList.add("esc");
  const ex=document.getElementById("pt-extrato");
  ex.innerHTML=(r.extrato||[]).length?r.extrato.map(m=>{
    const q=new Date(m.quando);
    return `<div class="item"><div><b style="color:${m.pontos>=0?"#4ade80":"#f87171"}">${m.pontos>0?"+":""}${esc(m.pontos)}</b> <span class="sub">· ${esc(PT_TIPO[m.tipo]||m.tipo)}</span><br><span class="sub">${esc(m.obs||"")}${m.posto?" · "+esc(m.posto):""} · ${q.toLocaleDateString("pt-BR")}</span></div></div>`;
  }).join(""):'<div class="sub">Nenhum ponto ainda. Informe o seu CPF no caixa (ou acione pelo app) e ganhe 1 ponto a cada R$ 1.</div>';
  carregarResgates();
}
async function carregarResgates(){
  const lst=await fetch(API+"/cashback/api/resgates",{headers:{"Authorization":"Bearer "+tok()}}).then(x=>x.json()).catch(()=>[]);
  const vivos=(Array.isArray(lst)?lst:[]).filter(x=>x.status==="emitido");
  document.getElementById("pt-resgates").innerHTML=vivos.map(x=>
    `<div class="item"><div>🎟 <b style="letter-spacing:2px;font-size:1.05rem">${esc(x.codigo)}</b> · ${esc(x.premio_nome)}<br><span class="sub">Retire no ${esc(x.posto)} até ${new Date(x.expira_em).toLocaleDateString("pt-BR")}</span></div><span class="tag t-pendente">A RETIRAR</span></div>`).join("");
}
async function abrirPremios(){
  const box=document.getElementById("pt-premios");
  if(!box.classList.contains("esc")){box.classList.add("esc");return;}
  const postoSel=document.getElementById("ac-posto");
  const posto=POSTO||(postoSel&&postoSel.value)||"";
  if(!posto){const m=document.getElementById("pt-msg");m.className="msg erro";m.textContent="Escolha o posto (no quadro acima) ou leia o QR de uma bomba.";garantirPosto();return;}
  box.classList.remove("esc");box.innerHTML='<div class="sub">Carregando prêmios…</div>';
  const lst=await fetch(API+"/cashback/api/premios?posto="+posto).then(x=>x.json()).catch(()=>[]);
  const ps=Array.isArray(lst)?lst:[];
  box.innerHTML=ps.length?ps.map(p=>{
    const pode=p.disponivel&&PT_SALDO>=p.pontos;
    return `<div class="item" style="align-items:center;gap:10px">
      ${p.foto_url?`<img src="${esc(p.foto_url)}" alt="" style="width:54px;height:54px;object-fit:cover;border-radius:8px;flex:0 0 auto">`:""}
      <div style="flex:1;min-width:0"><b>${esc(p.nome)}</b><br><span class="sub" style="color:#fbbf24">${esc(p.pontos)} pontos${p.disponivel?"":" · esgotado"}</span></div>
      <button onclick="resgatarPremio('${esc(p.id)}','${esc(posto)}')" ${pode?"":"disabled"} style="width:auto;margin:0;padding:8px 12px;${pode?"":"opacity:.4"}">Trocar</button></div>`;
  }).join(""):'<div class="sub">Este posto ainda não tem prêmios para troca.</div>';
}
async function resgatarPremio(premio,posto){
  const m=document.getElementById("pt-msg");m.className="msg";m.textContent="Trocando…";
  const r=await req("/cashback/api/resgatar",{premio_id:premio,posto:posto});
  if(!r.ok){m.className="msg erro";m.textContent=r.erro||"Não deu para trocar";return;}
  m.textContent="";
  document.getElementById("pt-premios").classList.add("esc");
  const c=document.getElementById("pt-codigo");c.classList.remove("esc");
  c.innerHTML="🎟 Mostre este código no caixa do <b>"+esc(r.posto)+"</b>:<div style='font-size:2rem;font-weight:800;letter-spacing:6px;margin:6px 0'>"+esc(r.codigo)+"</div>"+
    esc(r.premio)+" · vale até "+new Date(r.expira_em).toLocaleDateString("pt-BR")+". Se não retirar, os pontos voltam.";
  carregarPontos();
}

// mostra a opção A PRAZO só pra cliente LIBERADO pelo posto (revalidado no servidor)
async function verificarPrazo(){
  const sel=document.getElementById("ac-forma");
  const opt=sel.querySelector('option[value="05"]');
  if(!POSTO){if(opt)opt.remove();return;}
  try{
    const r=await req("/cashback/api/prazo-status?posto="+POSTO,null);
    const rotulo=r.empresa?("🧾 A Prazo (conta: "+r.empresa+")"):"🧾 A Prazo (minha conta no posto)";
    const mot=document.getElementById("prazo-motivo");
    if(r.prazo){
      if(opt)opt.textContent=rotulo;
      else sel.insertAdjacentHTML("beforeend",'<option value="05">'+esc(rotulo)+'</option>');
      mot.classList.add("esc");
    } else {
      if(opt){opt.remove();carrinhoVisibilidade();}
      // diz o MOTIVO de o a prazo não estar disponível (antes escondia em silêncio)
      if(r.motivo){mot.textContent="🧾 A prazo indisponível: "+r.motivo;mot.classList.remove("esc");}
      else mot.classList.add("esc");
    }
  }catch(e){}
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
        (Array.isArray(ps)?ps:[]).map(p=>`<option value="${esc(p.id)}">${esc(p.nome)}</option>`).join("");
    }catch(e){sel.innerHTML='<option value="">Falha ao listar postos</option>';}
  }
  sel.onchange=()=>{if(sel.value){POSTO=sel.value;localStorage.setItem("cb_posto",POSTO);}};
}

// ---- CARRINHO da compra a prazo (outros bicos + produtos de loja) ----
let CARRINHO=[];   // [{tipo:'bico',bico}|{tipo:'produto',produto_id,nome,qtd,preco}]
let _cbBuscaTimer=null;
function carrinhoVisibilidade(){
  const prazo=document.getElementById("ac-forma").value==="05";
  document.getElementById("carrinho-box").classList.toggle("esc",!prazo);
  document.getElementById("veiculo-box").classList.toggle("esc",!prazo);
  if(prazo)carregarFrota();
  if(!prazo){CARRINHO=[];carrinhoRender();}
}

// placas da FROTA da empresa (motorista fixo só vê a placa dele)
let _frotaCarregada=false;
async function carregarFrota(){
  if(_frotaCarregada||!POSTO)return;
  _frotaCarregada=true;
  try{
    const lista=await fetch(API+"/cashback/api/frota?posto="+POSTO,
      {headers:{"Authorization":"Bearer "+tok()}}).then(r=>r.json());
    if(!Array.isArray(lista)||!lista.length)return;
    const sel=document.getElementById("ac-frota");
    sel.innerHTML='<option value="">— escolha a placa —</option>'+
      lista.map(v=>`<option value="${esc(v.placa)}">${esc(v.placa)}${v.veiculo?" · "+esc(v.veiculo):""}${v.minha?" ⭐":""}</option>`).join("")+
      '<option value="__outra__">outra placa (digitar)…</option>';
    document.getElementById("frota-sel-box").classList.remove("esc");
    document.getElementById("ac-placa").parentElement.style.display="none";  // esconde só a PLACA (KM continua)
    if(lista.length===1){sel.value=lista[0].placa;frotaEscolheu();}   // placa única: já seleciona
  }catch(e){}
}
function frotaEscolheu(){
  const sel=document.getElementById("ac-frota");
  const linhaDigitar=document.getElementById("ac-placa").parentElement;
  if(sel.value==="__outra__"){linhaDigitar.style.display="";document.getElementById("ac-placa").value="";document.getElementById("ac-placa").focus();return;}
  linhaDigitar.style.display="none";
  document.getElementById("ac-placa").value=sel.value||"";
}
function carrinhoRender(){
  const el=document.getElementById("carrinho-lista");
  const bicoP=(document.getElementById("ac-bico").value||"").replace(/\D/g,"");
  let html=bicoP?`<div class="item"><div>⛽ Abastecimento — bico <b>${bicoP}</b></div><span class="sub">principal</span></div>`:"";
  html+=CARRINHO.map((it,i)=>it.tipo==="bico"
    ?`<div class="item"><div>⛽ Abastecimento — bico <b>${it.bico}</b></div><a href="#" onclick="carrinhoRemover(${i});return false" style="color:#f87171">✕</a></div>`
    :`<div class="item"><div>🛍 ${esc(it.qtd)}x ${esc(it.nome)} <span class="sub">· ${brl(it.preco*it.qtd)}</span></div><a href="#" onclick="carrinhoRemover(${i});return false" style="color:#f87171">✕</a></div>`
  ).join("");
  el.innerHTML=html||'<div class="sub">Nenhum item ainda.</div>';
}
function carrinhoRemover(i){CARRINHO.splice(i,1);carrinhoRender();}
// "+ outro bico": campo na tela (prompt() não funciona no app instalado do iPhone)
function carrinhoAbrirBico(){
  const bx=document.getElementById("carrinho-bico-add");
  bx.classList.toggle("esc");
  if(!bx.classList.contains("esc"))document.getElementById("cb-bico-num").focus();
}
function carrinhoConfirmarBico(){
  const inp=document.getElementById("cb-bico-num");
  const b=(inp.value||"").replace(/\D/g,"");
  if(!b)return;
  const bicoP=(document.getElementById("ac-bico").value||"").replace(/\D/g,"");
  const m=document.getElementById("ac-msg");
  if(b===bicoP||CARRINHO.some(x=>x.tipo==="bico"&&String(x.bico)===b)){
    m.className="msg erro";m.textContent="O bico "+b+" já está na lista.";return;
  }
  m.textContent="";
  CARRINHO.push({tipo:"bico",bico:parseInt(b,10)});
  inp.value="";document.getElementById("carrinho-bico-add").classList.add("esc");
  carrinhoRender();
}
function carrinhoAbrirBusca(){
  const bx=document.getElementById("carrinho-busca");
  bx.classList.toggle("esc");
  if(!bx.classList.contains("esc"))document.getElementById("cb-q").focus();
}
let _cbResultados=[];
document.getElementById("cb-q").addEventListener("input",()=>{
  clearTimeout(_cbBuscaTimer);
  _cbBuscaTimer=setTimeout(async()=>{
    const q=document.getElementById("cb-q").value.trim(),res=document.getElementById("cb-res");
    if(q.length<2){res.innerHTML="";return;}
    res.innerHTML='<div class="sub">Buscando…</div>';
    try{
      // fetch direto: o helper req() espalha a resposta num objeto e DESTRÓI arrays
      const lista=await fetch(API+"/cashback/api/produtos?posto="+POSTO+"&q="+encodeURIComponent(q),
        {headers:{"Authorization":"Bearer "+tok()}}).then(r=>r.json());
      _cbResultados=Array.isArray(lista)?lista:[];
      res.innerHTML=_cbResultados.length
        ?_cbResultados.map((p,i)=>`<div class="item">
            <div style="flex:1;min-width:0"><div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(p.nome)}</div><span class="sub">${brl(p.preco)}</span></div>
            <div style="display:flex;gap:6px;align-items:center">
              <input id="cb-qtd-${i}" inputmode="numeric" value="1" style="width:54px;padding:8px;text-align:center">
              <button type="button" onclick="carrinhoAddProduto(${i})" style="width:auto;margin:0;padding:8px 14px;background:#16a34a">+</button>
            </div></div>`).join("")
        :'<div class="sub">Nada encontrado.</div>';
    }catch(e){res.innerHTML='<div class="sub">Falha na busca.</div>';}
  },450);
});
function carrinhoAddProduto(i){
  const p=_cbResultados[i];
  if(!p)return;
  const qtd=parseFloat(String(document.getElementById("cb-qtd-"+i).value||"1").replace(",","."))||1;
  if(qtd<=0)return;
  CARRINHO.push({tipo:"produto",produto_id:p.id,nome:p.nome,qtd:qtd,preco:p.preco});
  document.getElementById("cb-q").value="";document.getElementById("cb-res").innerHTML="";
  document.getElementById("carrinho-busca").classList.add("esc");
  carrinhoRender();
}

async function acionar(){
  const m=document.getElementById("ac-msg");m.className="msg";
  const postoSel=document.getElementById("ac-posto");
  const posto=POSTO||(postoSel&&postoSel.value)||"";
  if(!posto){m.className="msg erro";m.textContent="Escolha o POSTO acima (ou leia o QR do bico).";garantirPosto();return;}
  // regras do A PRAZO: combustível VAZIO + sem bico = só produtos (precisa ter itens);
  // combustível escolhido = vai abastecer (precisa do bico)
  const fPag=document.getElementById("ac-forma").value;
  const bicoV=(document.getElementById("ac-bico").value||"").replace(/\D/g,"");
  const combV=document.getElementById("ac-comb").value;
  if(fPag==="05"){
    const temProduto=CARRINHO.some(x=>x.tipo==="produto");
    if(!bicoV&&!combV&&!temProduto){m.className="msg erro";m.textContent="Adicione produtos ao carrinho — ou informe o bico se for abastecer.";return;}
    if(!bicoV&&combV){m.className="msg erro";m.textContent="Escolheu o combustível: informe o Nº DO BICO (ou deixe o combustível vazio para só produtos).";return;}
  }
  // A PRAZO: a sua foto é a assinatura da compra — tirada agora, antes de abastecer
  let selfie=null;
  if(fPag==="05"){
    if(!TEM_FACIAL){
      m.textContent="Para comprar a prazo pelo app precisamos da foto do seu rosto (uma vez só).";
      if(!(await facialCadastrar())){m.className="msg erro";m.textContent="Sem a foto do cadastro, a compra a prazo é feita no caixa.";return;}
    }
    selfie=await faceAbrir("Sua foto autoriza esta compra a prazo",false);
    if(!selfie){m.className="msg erro";m.textContent="Sem a foto, a compra a prazo não é autorizada pelo app.";return;}
  }
  m.textContent="Acionando…";
  const r=await req("/cashback/api/acionar",{posto:posto,selfie:selfie,bico:document.getElementById("ac-bico").value,
    combustivel:document.getElementById("ac-comb").value,forma:document.getElementById("ac-forma").value,
    itens:(document.getElementById("ac-forma").value==="05"?CARRINHO:[]),
    placa:document.getElementById("ac-placa").value,km:document.getElementById("ac-km").value});
  if(r.ok){m.textContent="";CARRINHO=[];carrinhoRender();carregarDash();ligarEspelho();}
  else{m.className="msg erro";m.textContent=r.erro||"Falha ao acionar";
    if(r.precisa_facial)TEM_FACIAL=false;
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
  if(r.fase==="aguardando_inicio"&&!ac.bico){
    fase.textContent="🛍 Compra enviada ao caixa";
    num.textContent="—";det.textContent="Retire seus produtos — a emissão sai em instantes.";
  } else if(r.fase==="aguardando_inicio"){
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
      (ac.forma==="05"
        ? (ac.auth_codigo ? ". Vai direto pra sua CONTA no posto — já autorizada pela sua foto ✅"
                          : ". Vai direto pra sua CONTA no posto — só assinar a via no caixa 🧾")
        : ". Agora pague no caixa em "+nomeForma(ac.forma)+" 💳");
  } else if(r.fase==="cashback"){
    const c=r.cashback||{};
    fase.textContent=c.status==="pago"?"🎉 Cashback PAGO no seu Pix!":"🕐 Cashback a caminho…";
    num.textContent=brl(c.valor_cashback||0);
    det.textContent=c.status==="pago"?"Confira seu extrato — e obrigado pela preferência!":"Pagamento em processamento (cai em instantes).";
    if(c.status==="pago"){clearInterval(_lvTimer);_lvTimer=null;setTimeout(carregarDash,4000);}
  } else if(r.fase==="usado"&&ac.forma==="05"){
    fase.textContent="🧾 Venda A PRAZO lançada na sua conta!";
    num.textContent=ac.venda_numero?("cupom "+ac.venda_numero):"✓";
    det.textContent=(ac.auth_codigo?("Autorizada pela sua foto · código "+ac.auth_codigo+". O cupom segue no seu WhatsApp/e-mail. "):"")+"Obrigado pela preferência — bom trajeto! ⛽";
    clearInterval(_lvTimer);_lvTimer=null;setTimeout(carregarDash,ac.auth_codigo?12000:5000);
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
      sel.insertAdjacentHTML("beforeend",`<option>${esc(r.combustivel)}</option>`);
    sel.value=r.combustivel;
    info.innerHTML="⛽ <b style='color:#4ade80'>"+esc(r.combustivel)+"</b>"+
      (r.preco_litro?(" · ≈ R$ "+Number(r.preco_litro).toLocaleString("pt-BR",{minimumFractionDigits:2})+"/L"):"");
  }catch(e){info.textContent="";}
}
document.getElementById("ac-bico").addEventListener("input",()=>{
  clearTimeout(_infoBicoTimer);
  // apagou o bico -> combustível volta a vazio (vazio = só produtos)
  if(!(document.getElementById("ac-bico").value||"").replace(/\D/g,"")){
    document.getElementById("ac-comb").value="";
    document.getElementById("ac-bico-info").textContent="";
  }
  _infoBicoTimer=setTimeout(carregarInfoBico,500);try{carrinhoRender();}catch(e){}
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
