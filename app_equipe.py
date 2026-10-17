# -*- coding: utf-8 -*-
"""
App Postos SN — ACESSO DA EQUIPE (servidor), 11/10/2026.

O dono e os gerentes entram no app com o MESMO usuário e senha do sistema e acompanham
os postos pelo celular. Telas em app_www/js/equipe.js; rotas /app/equipe/api/*.

Decisões do Ronan (10/10/2026):
  * o gerente vê o mesmo que o dono, mas o dono liga e desliga, POR PERFIL, o que cada
    perfil pode ver e aprovar (tela Permissões do app);
  * aprovações (desconto, a prazo, aferição) com um toque e validade de 1 minuto — é a
    PRÓXIMA etapa (mexe no PDV e depende do aviso no celular). Enquanto
    APROVACOES_NO_AR for False, o servidor diz ao app que ninguém aprova nada e as telas
    de aprovação não aparecem. ETAPA 1 (12/10): aferição já chega ao app (ver TIPOS_NO_AR).

Regras deste módulo:
  * QUEM entra: cadastro ATIVO em oct_perfis e (dono OU perfil com alguma permissão do
    app ligada). O servidor confere a senha no Supabase Auth e entrega uma sessão PRÓPRIA
    (30 dias, assinada com chave derivada da CHAVE_MESTRA); a sessão do Supabase criada
    na conferência é encerrada em seguida e nada dela é guardado.
  * O QUE cada um vê: só os postos dele (oct_perfis.empresa_id + empresas; o dono, todos
    os ativos) e só os números que o perfil pode ver — é o SERVIDOR que deixa de mandar,
    esconder no app é só acabamento.
  * PERMISSÕES: mesma tabela do resto do sistema (oct_perfis_permissoes), chaves
    "app.<nome>"; exceção por pessoa em oct_perfis.perm_mais / perm_menos. Sem linha
    gravada vale o padrão (PADRAO): gerente tudo ligado, os outros tudo desligado.
  * NÚMEROS: as mesmas contas do Monitor do retaguarda (monitor_tanques.js): venda do
    dia = bomba (oct_pdv_abastecimentos, sem aferição) + loja; sem pista publicada,
    cupons + fila. Tanque baixo = 15% ou menos; sem leitura = 30 min.
"""
import base64
import hashlib
import hmac
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests as rq
from flask import Blueprint, jsonify, request

import cashback_portal as cp

bp_equipe = Blueprint("equipe", __name__)


# ------------------------------------------------------------------ medição de lentidão (17/10/2026)
# Ronan: "o app está travando muito, clico em Conta e demora quase 30 s". 30 s = o _sget do cashback_portal esperando 10 s
# de leitura + 20 s da 2ª tentativa. Para achar a causa com número (e não no chute): cada pedido ao servidor e cada
# consulta ao banco mais lentos ficam guardados em memória e saem em /app/diag/lentidao (só rota, tabela e tempo — nada de
# dado de cliente). ?zerar=1 recomeça a contagem.
from collections import deque
import threading as _th

_DIAG = {"desde": time.time(), "rotas": {}, "lentas": deque(maxlen=120), "banco": {}, "banco_lento": deque(maxlen=120)}
_DIAG_TRAVA = _th.Lock()
_DIAG_LENTO_MS = 800


def _diag_conta(tab, chave, ms):
    x = tab.setdefault(chave, [0, 0.0, 0.0])
    x[0] += 1
    x[1] += ms
    x[2] = max(x[2], ms)


_sget_original = cp._sget


def _sget_medido(q):
    t0 = time.time()
    try:
        return _sget_original(q)
    finally:
        ms = (time.time() - t0) * 1000
        tabela = str(q).split("?")[0]
        with _DIAG_TRAVA:
            _diag_conta(_DIAG["banco"], tabela, ms)
            if ms >= _DIAG_LENTO_MS:
                _DIAG["banco_lento"].append((time.strftime("%H:%M:%S", time.gmtime(time.time() - 3 * 3600)), tabela, round(ms)))


cp._sget = _sget_medido          # vale para o cashback_portal e para todos os módulos que consultam o banco por ele


@bp_equipe.before_app_request
def _diag_inicio():
    request._diag_t0 = time.time()


@bp_equipe.after_app_request
def _diag_fim(resp):
    try:
        ms = (time.time() - getattr(request, "_diag_t0", time.time())) * 1000
        rota = request.url_rule.rule if request.url_rule else "(estático)"
        with _DIAG_TRAVA:
            _diag_conta(_DIAG["rotas"], request.method + " " + rota, ms)
            if ms >= _DIAG_LENTO_MS:
                _DIAG["lentas"].append((time.strftime("%H:%M:%S", time.gmtime(time.time() - 3 * 3600)), request.method + " " + rota, round(ms), resp.status_code))
    except Exception:
        pass
    return resp


@bp_equipe.route("/app/diag/lentidao", methods=["GET"])
def api_diag_lentidao():
    with _DIAG_TRAVA:
        if request.args.get("zerar"):
            _DIAG.update(desde=time.time(), rotas={}, banco={})
            _DIAG["lentas"].clear()
            _DIAG["banco_lento"].clear()
        fmt = lambda tab: sorted(({"nome": k, "vezes": v[0], "media_ms": round(v[1] / v[0]), "max_ms": round(v[2])} for k, v in tab.items()),
                                 key=lambda x: -x["max_ms"])[:40]
        return jsonify({"processo": os.getpid(), "minutos": round((time.time() - _DIAG["desde"]) / 60, 1),
                        "rotas": fmt(_DIAG["rotas"]), "banco": fmt(_DIAG["banco"]),
                        "pedidos_lentos": list(_DIAG["lentas"])[-40:], "consultas_lentas": list(_DIAG["banco_lento"])[-40:]})

# Tipos de pedido que JÁ chegam ao app. Aferição usa o que já existe (oct_pdv_abastecimentos.status = 'afericao_pendente' e a mesma
# decisão do retaguarda); desconto e a prazo dependem do PDV pedir pelo servidor (próxima etapa).
TIPOS_NO_AR = {"afericao": True, "desconto": True, "prazo": False}
APROVACOES_NO_AR = any(TIPOS_NO_AR.values())
PERMISSOES = ("aprovar_desconto", "aprovar_prazo", "aprovar_afericao",
              "ver_vendas", "ver_litros", "ver_tanques", "ver_recebido", "ver_precos", "ver_alertas",
              "ver_comissao")                 # comissão dos vendedores (gestão, 17/10/2026)
APROVAR = tuple(k for k in PERMISSOES if k.startswith("aprovar_"))
VER_POSTO = ("ver_vendas", "ver_litros", "ver_tanques")
PERFIS = (("gerente", "Gerente"), ("financeiro", "Financeiro"), ("contabilidade", "Contabilidade"))
PADRAO = {"gerente": True}          # perfil sem linha gravada: gerente liga tudo, os outros nada
PREFIXO = "app."
AVISOS = ("pedidos", "tanques", "turno")
SESSAO_DIAS = 30
FUSO = timedelta(hours=-3)          # horário de Brasília (sem horário de verão desde 2019)
TANQUE_BAIXO_PCT = 15
SEM_SINAL_MIN = 30
# aferição volta para o tanque: não é venda. A marca está no STATUS (o tipo continua 'abastecimento') — até 17/10/2026 o
# painel só tirava tipo=afericao e contava a aferição como venda.
SEM_AFERICAO = "tipo=not.eq.afericao&status=not.in.(afericao_pendente,afericao_autorizada)"
LOGIN_MAX_FALHAS = 5
LOGIN_BLOQUEIO_SEG = 15 * 60

_POOL = ThreadPoolExecutor(max_workers=14)          # consultas de um posto
_POOL_POSTOS = ThreadPoolExecutor(max_workers=6)    # um posto por vez de cada pedido (pools separados: sem espera circular)
_CACHE = {}
_FALHAS = {}


# ------------------------------------------------------------------ utilidades
def _guardado(chave, seg, fn):
    agora = time.time()
    c = _CACHE.get(chave)
    if c and c[0] > agora:
        return c[1]
    v = fn()
    if len(_CACHE) > 400:
        _CACHE.clear()
    _CACHE[chave] = (agora + seg, v)
    return v


def _esquecer(prefixo):
    for k in list(_CACHE):
        if k.startswith(prefixo):
            _CACHE.pop(k, None)


def _num(v):
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _agora_local():
    return datetime.now(timezone.utc).replace(tzinfo=None) + FUSO


_RE_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def _instante(txt, local=False):
    """Texto de data do banco -> datetime em UTC (sem fuso), ou None.
    local=True: o texto é a hora LOCAL do posto, mesmo rotulada "+00:00" — é assim que o
    núcleo grava oct_pdv_abastecimentos.data_abast (por isso o Monitor compara só o dia)."""
    m = _RE_ISO.match(str(txt or "").strip())
    if not m:
        return None
    d = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
    fuso = m.group(3)
    if local or not fuso:
        return d - FUSO
    if fuso != "Z":
        sinal = 1 if fuso[0] == "+" else -1
        d -= sinal * timedelta(hours=int(fuso[1:3]), minutes=int(fuso[-2:]))
    return d


def _iso_utc(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def _ha(minutos):
    minutos = int(max(0, minutos))
    if minutos < 60:
        return f"há {minutos} min"
    if minutos < 48 * 60:
        return f"há {minutos // 60} h"
    return f"há {minutos // 1440} dias"


def _litros_txt(v):
    return f"{int(round(_num(v))):,}".replace(",", ".") + " L"


def _combustivel(nome, curto=False):
    """Nome de bomba legível: 'GASOLINA ADT' -> 'Gasolina aditivada' ('Aditivada' no cartão)."""
    s = str(nome or "").strip()
    u = s.upper().replace("-", "").replace(" ", "")
    if "S10" in u:
        return "Diesel S10"
    if "S500" in u:
        return "Diesel S500"
    if "DIESEL" in u:
        return "Diesel"
    if "ETANOL" in u or "ALCOOL" in u or "ÁLCOOL" in u:
        return "Etanol"
    if "GASOLINA" in u:
        if re.search(r"GASOLINAAD|PODIUM|PREMIUM|GRID", u):
            return "Aditivada" if curto else "Gasolina aditivada"
        return "Gasolina" if curto else "Gasolina comum"
    if "ARLA" in u:
        return "Arla"
    if "GNV" in u:
        return "GNV"
    return (s[:1].upper() + s[1:].lower()) if s else "—"


_ORDEM_COMB = ["Gasolina comum", "Gasolina", "Gasolina aditivada", "Aditivada", "Etanol", "Diesel S10", "Diesel S500", "Diesel", "Arla", "GNV"]


def _ordem(nome):
    return _ORDEM_COMB.index(nome) if nome in _ORDEM_COMB else len(_ORDEM_COMB)


# o mesmo de/para do Monitor (monitor_tanques.js, _MON_FORMA), com "A prazo" curto para o celular
FORMAS = {"01": "Dinheiro", "02": "Cheque", "03": "Crédito", "04": "Débito", "05": "A prazo", "10": "Crédito", "11": "Crédito",
          "15": "Boleto", "17": "Pix", "18": "Pix", "19": "Pix", "90": "A prazo", "99": "Outro"}


_FORMA_POR_NOME = (("PIX", "Pix"), ("DEBITO", "Débito"), ("CREDITO", "Crédito"), ("PRAZO", "A prazo"), ("DINHEIRO", "Dinheiro"),
                   ("CHEQUE", "Cheque"), ("BOLETO", "Boleto"))


def _forma(cod, nome=None):
    """Nome padrão da forma de pagamento: pelo código fiscal; sem código, pelo nome escrito
    (a fila traz 'CREDITO', 'Cartão Débito', 'NOTA A PRAZO'...)."""
    c = str(cod or "").strip().zfill(2) if str(cod or "").strip() else ""
    if c in FORMAS:
        return FORMAS[c]
    n = str(nome or "").strip()
    u = n.upper().translate(str.maketrans("ÁÀÂÃÉÊÍÓÔÕÚÇ", "AAAAEEIOOOUC"))
    for chave, padrao in _FORMA_POR_NOME:
        if chave in u:
            return padrao
    return (n[:1].upper() + n[1:].lower()) if n else "Outro"


# ------------------------------------------------------------------ sessão
def _token_gerar(uid, versao=0):
    exp = int(time.time()) + SESSAO_DIAS * 86400
    corpo = f"eq1|{uid}|{exp}|{int(versao or 0)}"
    ass = hmac.new(cp._chave("equipe"), corpo.encode(), hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(f"{corpo}|{ass}".encode()).decode()


def _token_validar(tok):
    """(id do usuário, versão) ou None."""
    try:
        partes = base64.urlsafe_b64decode(str(tok).encode()).decode().split("|")
        if len(partes) != 5 or partes[0] != "eq1":
            return None
        _, uid, exp, ver, ass = partes
        if int(exp) < time.time() or not cp._uuid_ok(uid):
            return None
        esperado = hmac.new(cp._chave("equipe"), f"eq1|{uid}|{exp}|{ver}".encode(), hashlib.sha256).hexdigest()[:40]
        return (uid, int(ver)) if hmac.compare_digest(ass, esperado) else None
    except Exception:
        return None


def _perfil(uid):
    def busca():
        r = cp._sget(f"oct_perfis?id=eq.{uid}&select=id,nome,papel,ativo,empresa_id,empresas,perm_mais,perm_menos,email_login,usuario&limit=1")
        return r[0] if r else None
    return _guardado("perfil|" + uid, 60, busca)


def _eh_dono(p):
    return (p or {}).get("papel") == "master"       # a mesma régua do banco (oct_eh_master)


def _prefs(uid):
    """Linha de oct_app_equipe (avisos, versão da sessão). Sem a tabela/linha: padrão."""
    def busca():
        try:
            r = cp._sget(f"oct_app_equipe?usuario_id=eq.{uid}&select=avisos,token_versao&limit=1")
        except Exception as e:
            if "PGRST205" in str(e) or "oct_app_equipe" in str(e) and "404" in str(e):
                return {}                           # SQL-APP-EQUIPE.sql ainda não rodado
            raise                                   # banco lento/fora: NÃO é sessão encerrada (ver _eu)
        return r[0] if r else {}
    return _guardado("prefs|" + uid, 60, busca)


def _padroes_do_perfil(papel):
    """{permissão: bool} do perfil: linha gravada em oct_perfis_permissoes, senão o PADRAO."""
    def busca():
        base = {k: bool(PADRAO.get(papel)) for k in PERMISSOES}
        try:
            for r in cp._sget(f"oct_perfis_permissoes?perfil=eq.{papel}&chave=like.{PREFIXO}*&select=chave,liberado"):
                k = str(r.get("chave") or "")[len(PREFIXO):]
                if k in base:
                    base[k] = bool(r.get("liberado"))
        except Exception:
            pass
        return base
    return _guardado("padrao|" + str(papel), 60, busca)


def pode_de(p):
    """O que este cadastro pode no app. Dono: tudo. Os demais: perfil + exceções da pessoa."""
    if _eh_dono(p):
        pode = {k: True for k in PERMISSOES}
    else:
        pode = dict(_padroes_do_perfil(p.get("papel")))
        for k in PERMISSOES:
            if (PREFIXO + k) in (p.get("perm_mais") or []):
                pode[k] = True
            if (PREFIXO + k) in (p.get("perm_menos") or []):
                pode[k] = False
    for k in APROVAR:                           # só o que já chega ao app pode ser aprovado por ele
        if not TIPOS_NO_AR.get(k[len("aprovar_"):]):
            pode[k] = False
    return pode


def _entra(p):
    if not p or p.get("ativo") is False:
        return False
    return _eh_dono(p) or any(pode_de(p).values())


def _empresas_ativas():
    def busca():
        out = {}
        for e in cp._sget("oct_empresas?select=id,nome,nome_fantasia,ativo&order=nome_fantasia"):
            nome = e.get("nome_fantasia") or e.get("nome") or "Posto"
            if e.get("ativo") is False or re.search(r"BANCADA|TESTE", nome, re.I):
                continue
            out[e["id"]] = cp._nome_bonito(nome)
        return out
    return _guardado("empresas", 120, busca)


def postos_de(p):
    """[(id, nome)] dos postos que este cadastro enxerga, em ordem de nome."""
    ativas = _empresas_ativas()
    if _eh_dono(p):
        ids = list(ativas)
    else:
        ids = [str(e) for e in ([p.get("empresa_id")] + list(p.get("empresas") or [])) if e and str(e) in ativas]
    vistos, out = set(), []
    for i in ids:
        if i not in vistos:
            vistos.add(i)
            out.append((i, ativas[i]))
    return sorted(out, key=lambda x: x[1])


def _eu():
    """Cadastro de quem chama (sessão da equipe válida e ainda com acesso), ou None."""
    tok = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    val = _token_validar(tok) if tok else None
    if not val:
        return None
    uid, ver = val
    # 17/10/2026: falha do banco aqui virava None -> 401 "Sessão encerrada" -> o app deslogava a pessoa (o Ronan caiu na
    # tela de entrar depois da lentidão de ~30 s). Agora o erro SOBE e vira 503 em _erro_do_servidor: o app mostra o que
    # tem guardado e tenta de novo; só token inválido, acesso cortado ou "sair de todos os aparelhos" encerram a sessão.
    p = _perfil(uid)
    if not _entra(p) or int(_prefs(uid).get("token_versao") or 0) != ver:
        return None
    return p


@bp_equipe.errorhandler(Exception)
def _erro_do_servidor(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    print(f"[app-equipe] {request.method} {request.path}: {str(e)[:200]}")
    return jsonify({"erro": "O servidor demorou para responder. Tente de novo em instantes."}), 503


def _nao_entrou():
    return jsonify({"erro": "Sessão encerrada. Entre de novo."}), 401


# ------------------------------------------------------------------ entrar
def _senha_auth(s):
    s = str(s or "")
    return s + "#octano-pin" if re.fullmatch(r"\d{4}", s) else s     # igual ao octSenhaAuth do retaguarda


def _email_do_login(login):
    login = str(login or "").strip().lower()
    if "@" in login:
        return login
    try:
        lk = cp._sget(f"oct_login_lookup?usuario=eq.{rq.utils.quote(login, safe='')}&select=email_login&limit=1")
        if lk and lk[0].get("email_login"):
            return lk[0]["email_login"]
    except Exception:
        pass
    return login + "@octano.interno"


def _conferir_senha(email, senha):
    """Confere no Supabase Auth. Devolve (id do usuário | None, erro de rede | None)."""
    url, key = cp._supa()
    try:
        r = rq.post(f"{url}/auth/v1/token?grant_type=password", headers={"apikey": key, "Content-Type": "application/json"},
                    json={"email": email, "password": _senha_auth(senha)}, timeout=15)
    except Exception:
        return None, "rede"
    if r.status_code != 200:
        return None, ("rede" if r.status_code >= 500 else None)
    j = r.json() or {}
    uid = (j.get("user") or {}).get("id")
    try:                    # a sessão do Supabase só serviu para conferir a senha: encerra só ELA
        rq.post(f"{url}/auth/v1/logout?scope=local", headers={"apikey": key, "Authorization": "Bearer " + str(j.get("access_token") or "")}, timeout=8)
    except Exception:
        pass
    return uid, None


@bp_equipe.route("/app/equipe/api/estado", methods=["GET"])
def api_estado():
    return jsonify({"ok": True, "equipe": True, "aprovacoes": APROVACOES_NO_AR})


@bp_equipe.route("/app/equipe/api/entrar", methods=["POST"])
def api_entrar():
    d = request.get_json(silent=True) or {}
    login, senha = str(d.get("email") or d.get("usuario") or "").strip().lower()[:120], str(d.get("senha") or "")[:200]
    if not login or not senha:
        return jsonify({"erro": "Preencha o usuário e a senha."}), 400
    if not cp._limite_ok("eq-entrar|" + cp._ip(), 20, 600):
        return cp._muitas()
    agora = time.time()
    falhas = [t for t in _FALHAS.get(login, []) if agora - t < LOGIN_BLOQUEIO_SEG]
    if len(falhas) >= LOGIN_MAX_FALHAS:
        return cp._muitas("Muitas tentativas com este usuário. Espere 15 minutos e tente de novo.")
    uid, erro = _conferir_senha(_email_do_login(login), senha)
    if erro:
        return jsonify({"erro": "Não consegui conferir a senha agora. Tente de novo em instantes."}), 502
    if not uid:
        if len(_FALHAS) > 2000:
            _FALHAS.clear()
        _FALHAS[login] = falhas + [agora]
        return jsonify({"erro": "Usuário ou senha inválidos."}), 401
    _FALHAS.pop(login, None)
    _esquecer("perfil|" + uid)
    try:
        p = _perfil(uid)
    except Exception:
        return jsonify({"erro": "Não consegui ler o seu cadastro agora. Tente de novo."}), 502
    if not p or p.get("ativo") is False:
        return jsonify({"erro": "Seu cadastro está desativado no sistema."}), 403
    if not _entra(p):
        return jsonify({"erro": "Seu perfil ainda não tem acesso ao app da equipe. Peça ao dono para liberar em Permissões do app."}), 403
    _esquecer("prefs|" + uid)
    return jsonify({"ok": True, "token": _token_gerar(uid, _prefs(uid).get("token_versao") or 0)})


@bp_equipe.route("/app/equipe/api/eu", methods=["GET"])
def api_eu():
    p = _eu()
    if not p:
        return _nao_entrou()
    dono = _eh_dono(p)
    av = _prefs(p["id"]).get("avisos") or {}
    papel_nome = "Gerencial" if dono else dict(PERFIS).get(p.get("papel"), "Equipe")
    return jsonify({"ok": True, "nome": p.get("nome") or "", "email": p.get("usuario") or p.get("email_login") or "",
                    "papel": p.get("papel"), "papel_nome": papel_nome, "master": dono,
                    "postos": [n for _, n in postos_de(p)], "pode": pode_de(p),
                    "avisos": {k: bool(av.get(k, k != "turno")) for k in AVISOS},
                    "aprovacoes_no_ar": APROVACOES_NO_AR,
                    # aviso no celular só existe com o Firebase configurado (app publicado nas lojas)
                    "push": bool((os.environ.get("FIREBASE_SA") or "").strip())})


# ------------------------------------------------------------------ números do posto (as contas do Monitor)
def montar_posto(b, agora):
    """PURA. b = linhas cruas {pista, vendas, fila, turno, tanques, medicoes, precos}; agora = hora local.
    Devolve o posto COMPLETO (quem filtra pelo perfil é filtrar_posto)."""
    hoje = agora.strftime("%Y-%m-%d")
    agora_utc = agora - FUSO
    pista = [a for a in (b.get("pista") or []) if str(a.get("data_abast") or "")[:10] == hoje]     # dia futuro = relógio errado
    tem_pista = bool(pista)
    pista_valor = sum(_num(a.get("valor_total")) or _num(a.get("litros")) * _num(a.get("preco_litro")) for a in pista)
    pista_litros = sum(_num(a.get("litros")) for a in pista)

    vendas = [v for v in (b.get("vendas") or []) if (v.get("status") or "") != "cancelada"]
    formas, loja, litros_cupons = {}, 0.0, 0.0
    for v in vendas:
        for it in (v.get("itens") or []):
            if not isinstance(it, dict):
                continue
            if it.get("tipo") == "abastecimento":
                litros_cupons += _num(it.get("qtd"))
            else:
                loja += _num(it.get("total"))
        # no dinheiro o pagamento traz o valor ENTREGUE (com troco): vale o líquido da venda
        pags = [x for x in (v.get("pagamentos") or []) if isinstance(x, dict)]
        nao_din, tem_din = 0.0, False
        for x in pags:
            if str(x.get("forma") or "").zfill(2) == "01":
                tem_din = True
                continue
            nao_din += _num(x.get("valor"))
            n = _forma(x.get("forma"), x.get("nome"))
            formas[n] = formas.get(n, 0.0) + _num(x.get("valor"))
        if tem_din or not pags:
            din = max(0.0, _num(v.get("valor_total")) - nao_din)
            if din > 0:
                formas["Dinheiro"] = formas.get("Dinheiro", 0.0) + din
    fila_total = fila_litros = 0.0
    fila = b.get("fila") or []
    for f in fila:
        vf = _num(f.get("valor")) - _num(f.get("desconto")) + _num(f.get("acrescimo"))
        fila_total += vf
        fila_litros += _num(f.get("litros"))
        n = _forma(f.get("forma"), f.get("forma_nome"))
        formas[n] = formas.get(n, 0.0) + vf
        if not _num(f.get("litros")) > 0:
            loja += vf                                  # produto de loja ainda na fila
    total = (pista_valor + loja) if tem_pista else (sum(_num(v.get("valor_total")) for v in vendas) + fila_total)
    litros = pista_litros if tem_pista else (litros_cupons + fila_litros)

    # tanques: cadastro (capacidade) + a leitura mais nova de cada um
    ultima = {}
    for m in (b.get("medicoes") or []):
        try:
            n = int(m.get("tanque_numero"))
        except Exception:
            continue
        if n not in ultima or str(m.get("medido_em") or "") > str(ultima[n].get("medido_em") or ""):
            ultima[n] = m
    tanques, medido = [], None
    for t in sorted((b.get("tanques") or []), key=lambda x: _num(x.get("numero"))):
        cap = _num(t.get("capacidade"))
        m = ultima.get(int(_num(t.get("numero"))))
        if cap <= 0 or not m:
            continue                                    # sem capacidade ou sem leitura: não dá para dizer o nível
        vol = _num(m.get("volume"))
        pct = max(0.0, min(100.0, vol / cap * 100))
        quando = _instante(m.get("medido_em"))
        if quando and (medido is None or quando > medido):
            medido = quando
        tanques.append({"numero": int(_num(t.get("numero"))), "produto": _combustivel(t.get("combustivel") or m.get("combustivel"), curto=True),
                        "litros": int(round(vol)), "capacidade": int(round(cap)), "pct": int(round(pct)), "baixo": pct <= TANQUE_BAIXO_PCT})

    # sinais de vida: leitura do tanque, último abastecimento publicado, último cupom
    sinais = [medido]
    if pista:
        sinais.append(max((_instante(a.get("data_abast"), local=True) for a in pista), key=lambda d: d or datetime.min))
    if vendas:
        sinais.append(max((_instante(v.get("data_venda")) for v in vendas), key=lambda d: d or datetime.min))
    sinais = [s for s in sinais if s]
    ultimo = max(sinais) if sinais else None
    parado = int((agora_utc - ultimo).total_seconds() // 60) if ultimo else None
    online = parado is not None and parado < SEM_SINAL_MIN
    sonda_min = int((agora_utc - medido).total_seconds() // 60) if medido else None
    sonda_parada = online and bool(b.get("tanques")) and (sonda_min is None or sonda_min >= SEM_SINAL_MIN)

    turnos = b.get("turno") or []
    turno = turnos[0] if turnos else None

    # preço na bomba: um por combustível; se os bicos divergem, mostra cada preço com os bicos
    por_comb = {}
    for x in (b.get("precos") or []):
        pr = round(_num(x.get("preco_nivel0")), 3)
        if pr > 0:
            por_comb.setdefault(_combustivel(x.get("combustivel")), {}).setdefault(pr, []).append(x.get("bico_numero"))
    precos = []
    for nome in sorted(por_comb, key=_ordem):
        if len(por_comb[nome]) == 1:
            precos.append([nome, next(iter(por_comb[nome]))])
        else:
            for pr in sorted(por_comb[nome]):
                bicos = sorted(int(_num(n)) for n in por_comb[nome][pr])
                precos.append([f"{nome} (bico {', '.join(str(n) for n in bicos)})", pr])

    out = {"online": online, "vendas": round(total, 2), "litros": round(litros, 1), "cupons": len(vendas), "tem_pista": tem_pista,
           # posto que ainda não publica a pista (só cupons): não há contagem de abastecimentos
           "abastecimentos": len(pista) if (tem_pista or not vendas) else None,
           "turno": (turno or {}).get("operador") or None, "turno_desde": (turno or {}).get("aberto_em") or None,
           "tanques": tanques, "medido_em": _iso_utc(medido),
           "formas": [[n, round(v, 2)] for n, v in sorted(formas.items(), key=lambda x: -x[1]) if v > 0.004],
           # a forma só existe depois que o caixa casa/emite: o que falta para fechar com a venda do dia
           "formas_falta": round(max(0.0, total - sum(formas.values())), 2),
           "precos": precos, "alertas": []}
    if not online:
        out["sem_resposta"] = "sem resposta " + _ha(parado) if parado is not None else "sem dados hoje"
        out["alertas"].append({"tipo": "sem_resposta", "titulo": "sem resposta",
                               "texto": ("Nada chegou do posto " + _ha(parado) + ".") if parado is not None else "Nenhum dado do posto hoje."})
    elif sonda_parada:
        out["alerta"] = "sonda parada"
        out["alertas"].append({"tipo": "sonda", "titulo": "sonda parada",
                               "texto": ("Sem leitura nova dos tanques " + _ha(sonda_min) + ".") if sonda_min is not None else "Nenhuma leitura dos tanques."})
    for t in tanques:
        if t["baixo"]:
            out["alertas"].append({"tipo": "tanque", "titulo": f"{t['produto']} em {t['pct']}%", "texto": f"{_litros_txt(t['litros'])} no tanque."})
    if not out.get("alerta") and online and any(t["baixo"] for t in tanques):
        out["alerta"] = "tanque baixo"
    return out


def _coletar(eid):
    agora = _agora_local()
    hoje = agora.strftime("%Y-%m-%d")
    desde = _iso_utc(datetime(agora.year, agora.month, agora.day) - FUSO)

    def pista():
        out = []
        for pg in range(5):                             # o PostgREST corta em 1000 linhas
            r = cp._sget("oct_pdv_abastecimentos?select=data_abast,litros,valor_total,preco_litro"
                         f"&empresa_id=eq.{eid}&data_abast=gte.{hoje}&{SEM_AFERICAO}&order=data_abast.desc&limit=1000&offset={pg * 1000}")
            out += r
            if len(r) < 1000:
                break
        return out

    consultas = {
        "pista": pista,
        "vendas": lambda: cp._sget(f"oct_pdv_vendas?select=valor_total,data_venda,pagamentos,status,itens&empresa_id=eq.{eid}&data_venda=gte.{desde}&order=data_venda.desc&limit=500"),
        "fila": lambda: cp._sget(f"oct_fila_transmissao?select=litros,valor,forma,forma_nome,desconto,acrescimo&empresa_id=eq.{eid}&status=eq.fila&criado_em=gte.{desde}&limit=1000"),
        "turno": lambda: cp._sget(f"oct_pdv_turnos?select=operador,aberto_em&empresa_id=eq.{eid}&status=eq.aberto&order=aberto_em.desc&limit=1"),
        "tanques": lambda: cp._sget(f"oct_tanques?select=numero,combustivel,capacidade&empresa_id=eq.{eid}&ativo=is.true&order=numero"),
        "medicoes": lambda: cp._sget(f"oct_medicoes?select=tanque_numero,combustivel,volume,medido_em&empresa_id=eq.{eid}&order=medido_em.desc&limit=40"),
        "precos": lambda: cp._sget(f"oct_precos_bomba?select=bico_numero,combustivel,preco_nivel0&empresa_id=eq.{eid}&order=bico_numero"),
    }
    futuros = {k: _POOL.submit(f) for k, f in consultas.items()}
    bruto, falhou = {}, []
    for k, f in futuros.items():
        try:
            bruto[k] = f.result(timeout=25)
        except Exception as e:
            bruto[k] = []
            falhou.append(k)
            print(f"[app-equipe] {eid} {k}: {str(e)[:120]}")
    p = montar_posto(bruto, agora)
    if falhou:
        p["incompleto"] = falhou
    return p


def dados_posto(eid):
    return _guardado("posto|" + eid, 30, lambda: _coletar(eid))


def filtrar_posto(d, eid, nome, pode, detalhe=False):
    """O que ESTE perfil recebe do posto: campo que ele não pode ver não sai do servidor."""
    out = {"id": eid, "nome": nome, "online": True, "turno": d.get("turno"), "turno_desde": d.get("turno_desde")}
    if pode.get("ver_vendas"):
        out["vendas"] = d.get("vendas")
    if pode.get("ver_litros"):
        out.update(litros=d.get("litros"), cupons=d.get("cupons"), abastecimentos=d.get("abastecimentos"))
    if pode.get("ver_tanques"):
        out.update(tanques=d.get("tanques") or [], medido_em=d.get("medido_em"))
    if pode.get("ver_alertas"):
        out["online"] = bool(d.get("online"))
        for k in ("sem_resposta", "alerta"):
            if d.get(k) and (k != "alerta" or d[k] != "tanque baixo" or pode.get("ver_tanques")):
                out[k] = d[k]
    if detalhe:
        if pode.get("ver_recebido"):
            out["formas"] = d.get("formas") or []
            if pode.get("ver_vendas") and _num(d.get("formas_falta")) >= 1:
                out["formas_falta"] = d["formas_falta"]
        if pode.get("ver_precos"):
            out["precos"] = d.get("precos") or []
    return out


def _alertas(d, eid, nome, pode):
    if not pode.get("ver_alertas"):
        return []
    out = []
    for a in d.get("alertas") or []:
        if a.get("tipo") == "tanque" and not pode.get("ver_tanques"):
            continue
        out.append({"posto_id": eid, "titulo": f"{nome} · {a['titulo']}", "texto": a.get("texto") or ""})
    return out


@bp_equipe.route("/app/equipe/api/painel", methods=["GET"])
def api_painel():
    p = _eu()
    if not p:
        return _nao_entrou()
    pode = pode_de(p)
    meus = postos_de(p) if any(pode.get(k) for k in VER_POSTO) else []
    futuros = [(eid, nome, _POOL_POSTOS.submit(dados_posto, eid)) for eid, nome in meus]
    postos, alertas = [], []
    for eid, nome, f in futuros:
        try:
            d = f.result(timeout=40)
        except Exception as e:
            print(f"[app-equipe] painel {eid}: {str(e)[:120]}")
            postos.append({"id": eid, "nome": nome, "online": False, "sem_resposta": "não consegui ler agora"})
            continue
        postos.append(filtrar_posto(d, eid, nome, pode))
        alertas += _alertas(d, eid, nome, pode)
    pend, resumo = _pendentes_resumo(p)
    if pode.get("ver_litros"):
        try:
            _aquecer_meses()                     # gráfico de meses do Comparar períodos pronto antes de alguém abrir
        except Exception:
            pass
    return jsonify({"ok": True, "postos": postos, "alertas": alertas, "pendentes": pend, "pendentes_resumo": resumo})


@bp_equipe.route("/app/equipe/api/posto/<eid>", methods=["GET"])
def api_posto(eid):
    p = _eu()
    if not p:
        return _nao_entrou()
    pode = pode_de(p)
    meus = dict(postos_de(p))
    if not cp._uuid_ok(eid) or eid not in meus or not any(pode.get(k) for k in VER_POSTO):
        return jsonify({"erro": "Posto não liberado para o seu perfil."}), 404
    try:
        d = dados_posto(eid)
    except Exception as e:
        return jsonify({"erro": "Não consegui ler o posto agora: " + str(e)[:80]}), 502
    return jsonify(dict(filtrar_posto(d, eid, meus[eid], pode, detalhe=True), ok=True))


# ------------------------------------------------------------------ GESTÃO, fase 1 (17/10/2026)
# Telas em app_www/js/equipe_gestao.js — as funções do app gerencial do TecnoX que o Ronan pediu (fotos em
# app-postos-sn/tecnox-referencia-2026-10-17). Fontes:
#   * combustível = a pista (oct_pdv_abastecimentos, sem aferição; data_abast é a hora LOCAL rotulada +00:00); posto sem
#     pista no período usa os itens "abastecimento" dos cupons, como o Monitor;
#   * loja = itens "produto" dos cupons (oct_pdv_vendas, data_venda em UTC); formas = pagamentos dos cupons (+ fila de hoje);
#   * estoque do dia (o livro, como no LMC) = medição da abertura + descargas (sonda com entrega_em_progresso) − vendido
#     pelos bicos daquele tanque; medidor = a última leitura;
#   * comissão = pista por crachá (vendedor) com o nome do cadastro (oct_pessoas.cartao_idf); a regra ainda não existe.
GESTAO_MAX_DIAS = 400
DESCARGA_MIN_L = 300            # subida mínima do tanque para a janela de "entrega" da sonda contar como descarga
_POOL_AQUECER = ThreadPoolExecutor(max_workers=2)   # meses fechados calculados em segundo plano (não disputa com os pedidos)


def _tudo(q, maxpag=80):
    """Todas as linhas de uma consulta (o PostgREST corta em 1000). q precisa de order."""
    out = []
    for pg in range(maxpag):
        r = cp._sget(f"{q}&limit=1000&offset={pg * 1000}")
        out += r
        if len(r) < 1000:
            break
    return out


def _dia(txt):
    try:
        return datetime.strptime(str(txt or "")[:10], "%Y-%m-%d")
    except Exception:
        return None


def _seguinte(iso):
    return (_dia(iso) + timedelta(days=1)).strftime("%Y-%m-%d")


def _utc_do_dia(iso):
    """Meia-noite LOCAL do dia -> instante UTC (para colunas gravadas em UTC: cupons, fila, medições)."""
    return _iso_utc(_dia(iso) - FUSO)


def _periodo_args(pre=""):
    """(de, ate, erro) do pedido: datas AAAA-MM-DD, de <= ate, até hoje, no máximo GESTAO_MAX_DIAS."""
    hoje = _agora_local().strftime("%Y-%m-%d")
    de, ate = str(request.args.get(pre + "de") or hoje)[:10], str(request.args.get(pre + "ate") or hoje)[:10]
    if not _dia(de) or not _dia(ate) or de > ate:
        return None, None, "Período inválido."
    ate = min(ate, hoje)
    if de > ate:
        return None, None, "O período é no futuro."
    if (_dia(ate) - _dia(de)).days + 1 > GESTAO_MAX_DIAS:
        return None, None, "Escolha um período de até 13 meses."
    return de, ate, None


def _dias_efetivos(de, ate, agora):
    """Dias do período para média: hoje conta só a parte do dia que já passou."""
    hoje = agora.strftime("%Y-%m-%d")
    dias = (_dia(ate) - _dia(de)).days + 1
    if ate < hoje:
        return float(dias)
    return dias - 1 + max(0.05, (agora.hour * 60 + agora.minute) / 1440)


def _postos_da_gestao(p):
    """([(id, nome)] do filtro, [(id, nome)] liberados, erro). ?posto=<id> filtra um só."""
    meus = postos_de(p)
    eid = str(request.args.get("posto") or "").strip()
    if eid:
        if eid not in dict(meus):
            return None, meus, "Posto não liberado para o seu perfil."
        return [(eid, dict(meus)[eid])], meus, None
    return meus, meus, None


def _pista(eid, de, ate):
    hoje = _agora_local().strftime("%Y-%m-%d")
    return _guardado(f"g-pista|{eid}|{de}|{ate}", 60 if ate >= hoje else 1800, lambda: _tudo(
        "oct_pdv_abastecimentos?select=data_abast,litros,valor_total,preco_litro,combustivel,produto_nome,vendedor,tanque_id"
        f"&empresa_id=eq.{eid}&data_abast=gte.{de}&data_abast=lt.{_seguinte(ate)}&{SEM_AFERICAO}&order=data_abast"))


def _cupons(eid, de, ate):
    hoje = _agora_local().strftime("%Y-%m-%d")
    return _guardado(f"g-cupons|{eid}|{de}|{ate}", 60 if ate >= hoje else 1800, lambda: _tudo(
        "oct_pdv_vendas?select=valor_total,pagamentos,status,itens"
        f"&empresa_id=eq.{eid}&data_venda=gte.{_utc_do_dia(de)}&data_venda=lt.{_utc_do_dia(_seguinte(ate))}&order=data_venda"))


def _fila_hoje(eid):
    hoje = _agora_local().strftime("%Y-%m-%d")
    return _guardado(f"g-fila|{eid}", 60, lambda: cp._sget(
        f"oct_fila_transmissao?select=litros,valor,forma,forma_nome,desconto,acrescimo&empresa_id=eq.{eid}&status=eq.fila"
        f"&criado_em=gte.{_utc_do_dia(hoje)}&limit=1000"))


def _valor_abast(a):
    return _num(a.get("valor_total")) or _num(a.get("litros")) * _num(a.get("preco_litro"))


def somar_vendas(postos_dados):
    """PURA. postos_dados = [{pista, cupons, fila}] -> números do período somados (combustível, loja, formas, vendedores).
    Por posto: com pista no período, o combustível vem dela; sem pista, dos itens 'abastecimento' dos cupons."""
    litros = valor = 0.0
    veiculos = 0
    por_comb, por_hora, formas, prods, vend = {}, [0.0] * 24, {}, {}, {}
    loja_qtd = loja_valor = 0.0
    sem_cracha = [0.0, 0]
    for d in postos_dados:
        pista = [a for a in (d.get("pista") or []) if _num(a.get("litros")) > 0]
        cupons = [v for v in (d.get("cupons") or []) if (v.get("status") or "") != "cancelada"]
        if pista:
            for a in pista:
                L, V = _num(a.get("litros")), _valor_abast(a)
                litros += L
                valor += V
                veiculos += 1
                nome = _combustivel(a.get("combustivel") or a.get("produto_nome"))
                pc = por_comb.setdefault(nome, [0.0, 0.0])
                pc[0] += L
                pc[1] += V
                m = _RE_ISO.match(str(a.get("data_abast") or ""))
                if m:
                    por_hora[int(m.group(2)[:2])] += L
                cr = str(a.get("vendedor") or "").strip().upper()
                if cr:
                    x = vend.setdefault(cr, {"litros": 0.0, "valor": 0.0, "abastecimentos": 0, "postos": {}})
                    x["litros"] += L
                    x["valor"] += V
                    x["abastecimentos"] += 1
                    x["postos"][d.get("posto")] = x["postos"].get(d.get("posto"), 0) + 1
                else:
                    sem_cracha[0] += L
                    sem_cracha[1] += 1
        for v in cupons:
            tem_abast = False
            for it in (v.get("itens") or []):
                if not isinstance(it, dict):
                    continue
                if it.get("tipo") == "abastecimento":
                    tem_abast = True
                    if not pista:
                        L, V = _num(it.get("qtd")), _num(it.get("total"))
                        litros += L
                        valor += V
                        nome = _combustivel(it.get("desc"))
                        pc = por_comb.setdefault(nome, [0.0, 0.0])
                        pc[0] += L
                        pc[1] += V
                else:
                    q, t = _num(it.get("qtd")) or 1, _num(it.get("total"))
                    loja_qtd += q
                    loja_valor += t
                    nm = str(it.get("desc") or "Produto").strip()
                    pr = prods.setdefault(nm, [0.0, 0.0])
                    pr[0] += q
                    pr[1] += t
            if tem_abast and not pista:
                veiculos += 1
            # formas: no dinheiro o pagamento traz o valor ENTREGUE (com troco) — vale o líquido da venda (como no Monitor)
            pags = [x for x in (v.get("pagamentos") or []) if isinstance(x, dict)]
            nao_din, tem_din = 0.0, False
            for x in pags:
                if str(x.get("forma") or "").zfill(2) == "01":
                    tem_din = True
                    continue
                nao_din += _num(x.get("valor"))
                n = _forma(x.get("forma"), x.get("nome"))
                formas[n] = formas.get(n, 0.0) + _num(x.get("valor"))
            if tem_din or not pags:
                din = max(0.0, _num(v.get("valor_total")) - nao_din)
                if din > 0:
                    formas["Dinheiro"] = formas.get("Dinheiro", 0.0) + din
        for f in (d.get("fila") or []):
            vf = _num(f.get("valor")) - _num(f.get("desconto")) + _num(f.get("acrescimo"))
            n = _forma(f.get("forma"), f.get("forma_nome"))
            formas[n] = formas.get(n, 0.0) + vf
            if not _num(f.get("litros")) > 0:
                loja_valor += vf
                loja_qtd += 1
    return {"litros": litros, "valor": valor, "veiculos": veiculos, "por_comb": por_comb, "por_hora": por_hora,
            "loja_qtd": loja_qtd, "loja_valor": loja_valor, "prods": prods, "formas": formas, "vendedores": vend,
            "sem_cracha": sem_cracha}


def _coletar_periodo(postos, de, ate, com_fila=True):
    hoje = _agora_local().strftime("%Y-%m-%d")

    def um(eid, nome):
        d = {"posto": nome}
        d["pista"], d["cupons"] = _pista(eid, de, ate), _cupons(eid, de, ate)
        d["fila"] = _fila_hoje(eid) if (com_fila and ate >= hoje) else []
        return d
    futuros = [_POOL_POSTOS.submit(um, eid, nome) for eid, nome in postos]
    return [f.result(timeout=90) for f in futuros]


def _r2(v):
    return round(v, 2) if v is not None else None


def _resumo_cmp(s):
    return {"litros": round(s["litros"], 1), "valor": _r2(s["valor"] + s["loja_valor"]), "veiculos": s["veiculos"],
            "ticket_litros": _r2(s["litros"] / s["veiculos"]) if s["veiculos"] else None,
            "ticket_valor": _r2(s["valor"] / s["veiculos"]) if s["veiculos"] else None,
            "prod_qtd": round(s["loja_qtd"]), "prod_valor": _r2(s["loja_valor"]),
            "prod_ticket": _r2(s["loja_valor"] / s["loja_qtd"]) if s["loja_qtd"] else None}


def _tira_sem_permissao(pode, d, campos_valor, campos_litros):
    for k in campos_valor if not pode.get("ver_vendas") else ():
        d[k] = None
    for k in campos_litros if not pode.get("ver_litros") else ():
        d[k] = None
    return d


@bp_equipe.route("/app/equipe/api/vendas", methods=["GET"])
def api_vendas():
    p = _eu()
    if not p:
        return _nao_entrou()
    pode = pode_de(p)
    if not (pode.get("ver_vendas") or pode.get("ver_litros")):
        return jsonify({"erro": "Seu perfil não vê as vendas."}), 403
    de, ate, erro = _periodo_args()
    postos, meus, erro2 = _postos_da_gestao(p)
    if erro or erro2:
        return jsonify({"erro": erro or erro2}), 400
    agora = _agora_local()
    try:
        s = somar_vendas(_coletar_periodo(postos, de, ate))
        prev = None
        mes_ini = agora.strftime("%Y-%m-01")
        if ate == agora.strftime("%Y-%m-%d"):       # período até hoje: previsão do mês no ritmo do mês até agora
            sm = s if de == mes_ini else somar_vendas(_coletar_periodo(postos, mes_ini, ate, com_fila=False))
            media_mes = sm["litros"] / _dias_efetivos(mes_ini, ate, agora)
            dias_mes = ((agora.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
            prev = (round(media_mes * dias_mes), round(media_mes))
    except Exception as e:
        print("[app-equipe] vendas:", str(e)[:160])
        return jsonify({"erro": "Não consegui ler as vendas agora. Tente de novo."}), 502
    total = s["valor"] + s["loja_valor"]
    comb = {"litros": round(s["litros"], 1), "valor": _r2(s["valor"]), "veiculos": s["veiculos"],
            "ticket_litros": _r2(s["litros"] / s["veiculos"]) if s["veiculos"] else None,
            "ticket_valor": _r2(s["valor"] / s["veiculos"]) if s["veiculos"] else None,
            "media_dia_litros": round(s["litros"] / _dias_efetivos(de, ate, agora)),
            "previsao_mes_litros": prev[0] if prev else None, "media_mes_litros": prev[1] if prev else None,
            "por_combustivel": [[n, round(v[0], 1), _r2(v[1])] for n, v in sorted(s["por_comb"].items(), key=lambda x: -x[1][0])],
            "por_hora": [[h, round(v, 1)] for h, v in enumerate(s["por_hora"])]}
    if not pode.get("ver_vendas"):
        comb.update(valor=None, ticket_valor=None, por_combustivel=[[n, L, None] for n, L, _ in comb["por_combustivel"]])
    if not pode.get("ver_litros"):
        comb.update(litros=None, veiculos=None, ticket_litros=None, media_dia_litros=None, previsao_mes_litros=None,
                    media_mes_litros=None, por_hora=[], por_combustivel=[[n, None, V] for n, _, V in comb["por_combustivel"]])
    prod = None
    if pode.get("ver_vendas"):
        top = sorted(s["prods"].items(), key=lambda x: -x[1][1])[:5]
        prod = {"qtd": round(s["loja_qtd"]), "valor": _r2(s["loja_valor"]),
                "ticket": _r2(s["loja_valor"] / s["loja_qtd"]) if s["loja_qtd"] else None,
                "participacao": round(s["loja_valor"] / total * 100, 1) if total else None,
                "top": [[cp._nome_bonito(n) or n, round(v[0]), _r2(v[1])] for n, v in top]}
    formas = [[n, _r2(v)] for n, v in sorted(s["formas"].items(), key=lambda x: -x[1]) if v > 0.004] if pode.get("ver_recebido") else []
    falta = round(max(0.0, total - sum(s["formas"].values())), 2)
    if formas and pode.get("ver_vendas") and falta >= 1:
        # hoje: o caixa ainda vai casar; período passado: venda da bomba cujo caixa não passou pelo Octano (ex.: PDV do TecnoX)
        formas.append(["Sem forma de pagamento ainda" if ate == agora.strftime("%Y-%m-%d") else "Sem forma registrada no Octano", falta])
    return jsonify({"ok": True, "de": de, "ate": ate, "postos": [[i, n] for i, n in meus],
                    "total_valor": _r2(total) if pode.get("ver_vendas") else None,
                    "combustivel": comb, "produtos": prod, "formas": formas})


def _litros_mes(eid, ini, fim):
    """Litros da pista num mês (só a coluna de litros: leve). Mês fechado fica guardado 6 h."""
    hoje = _agora_local().strftime("%Y-%m-%d")
    return _guardado(f"g-mes|{eid}|{ini}", 120 if fim >= hoje else 6 * 3600, lambda: sum(_num(a.get("litros")) for a in _tudo(
        f"oct_pdv_abastecimentos?select=litros&empresa_id=eq.{eid}&data_abast=gte.{ini}&data_abast=lt.{_seguinte(fim)}&{SEM_AFERICAO}&order=id")))


@bp_equipe.route("/app/equipe/api/comparativo", methods=["GET"])
def api_comparativo():
    p = _eu()
    if not p:
        return _nao_entrou()
    pode = pode_de(p)
    if not (pode.get("ver_vendas") or pode.get("ver_litros")):
        return jsonify({"erro": "Seu perfil não vê as vendas."}), 403
    de, ate, erro = _periodo_args()
    cde, cate, erro_b = _periodo_args("c")
    postos, meus, erro2 = _postos_da_gestao(p)
    if erro or erro_b or erro2:
        return jsonify({"erro": erro or erro_b or erro2}), 400
    try:
        fa = _POOL.submit(lambda: somar_vendas(_coletar_periodo(postos, de, ate)))
        fb = _POOL.submit(lambda: somar_vendas(_coletar_periodo(postos, cde, cate)))
        a, b = _resumo_cmp(fa.result(timeout=120)), _resumo_cmp(fb.result(timeout=120))
    except Exception as e:
        print("[app-equipe] comparativo:", str(e)[:160])
        return jsonify({"erro": "Não consegui comparar agora. Tente de novo."}), 502
    VAL, LIT = ("valor", "ticket_valor", "prod_valor", "prod_ticket", "prod_qtd"), ("litros", "veiculos", "ticket_litros")
    return jsonify({"ok": True, "postos": [[i, n] for i, n in meus],
                    "a": _tira_sem_permissao(pode, a, VAL, LIT), "b": _tira_sem_permissao(pode, b, VAL, LIT)})


def _ultimos_meses(agora, n=6):
    """[(AAAA-MM, primeiro dia, último dia ou hoje)] dos últimos n meses, o atual por último."""
    out, ini = [], agora.replace(day=1)
    for _ in range(n):
        fim = min((ini.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1), agora)
        out.append((ini.strftime("%Y-%m"), ini.strftime("%Y-%m-%d"), fim.strftime("%Y-%m-%d")))
        ini = (ini - timedelta(days=1)).replace(day=1)
    return out[::-1]


_aquecido = [0.0]


def _aquecer_meses():
    """Calcula em segundo plano os meses fechados de todos os postos (6 h de validade): o gráfico do Comparar abre na hora.
    Chamado quando alguém da equipe abre o Painel; no máximo uma vez a cada 5 h por processo."""
    if time.time() - _aquecido[0] < 5 * 3600:
        return
    _aquecido[0] = time.time()
    agora = _agora_local()
    for eid in _empresas_ativas():
        for _, ini, fim in _ultimos_meses(agora)[:-1]:
            _POOL_AQUECER.submit(_litros_mes, eid, ini, fim)


@bp_equipe.route("/app/equipe/api/meses", methods=["GET"])
def api_meses():
    """Litros por mês (últimos 6) do filtro de postos — o gráfico do Comparar períodos, pedido à parte (é a parte lenta)."""
    p = _eu()
    if not p:
        return _nao_entrou()
    if not pode_de(p).get("ver_litros"):
        return jsonify({"ok": True, "meses": []})
    postos, _, erro = _postos_da_gestao(p)
    if erro:
        return jsonify({"erro": erro}), 400
    try:
        meses = []
        for ym, ini, fim in _ultimos_meses(_agora_local()):
            futs = [_POOL.submit(_litros_mes, eid, ini, fim) for eid, _ in postos]
            meses.append([ym, round(sum(f.result(timeout=120) for f in futs))])
        while meses and not meses[0][1]:                  # meses antes do Octano começar a gravar a pista
            meses.pop(0)
    except Exception as e:
        print("[app-equipe] meses:", str(e)[:160])
        return jsonify({"erro": "Não consegui ler os meses agora."}), 502
    return jsonify({"ok": True, "meses": meses})


def estoque_do_dia(tanques, leituras, pista, abertura_ontem, agora):
    """PURA. Livro do dia por tanque (como no LMC) × medidor.
    tanques = oct_tanques; leituras = oct_medicoes de hoje (ordem de medido_em); pista = abastecimentos de hoje (com tanque_id);
    abertura_ontem = {numero: volume} da última leitura antes da meia-noite (posto sem leitura hoje ainda).
    Descarga = trecho com entrega_em_progresso: entrou = volume depois − volume antes + o que os bicos venderam no meio."""
    por_tq = {}
    for m in leituras:
        if _num(m.get("volume")) > 0 and _num(m.get("altura")) > 0:      # a sonda às vezes manda 0/0: descarta
            try:
                por_tq.setdefault(int(m.get("tanque_numero")), []).append(m)
            except Exception:
                pass
    num_do_id = {str(t.get("id")): int(_num(t.get("numero"))) for t in tanques}
    vendas = {}
    for a in pista:
        n = num_do_id.get(str(a.get("tanque_id")))
        if n is not None:
            vendas.setdefault(n, []).append((_instante(a.get("data_abast"), local=True), _num(a.get("litros"))))
    out = []
    for t in sorted(tanques, key=lambda x: _num(x.get("numero"))):
        n, cap = int(_num(t.get("numero"))), _num(t.get("capacidade"))
        ls = por_tq.get(n, [])
        vend = vendas.get(n, [])
        vendido = sum(v for _, v in vend)
        medidor = _num(ls[-1].get("volume")) if ls else None
        abertura = _num(ls[0].get("volume")) if ls else abertura_ontem.get(n)
        entrou, i = 0.0, 0
        while i < len(ls):
            if ls[i].get("entrega_em_progresso"):
                j = i
                while j < len(ls) and ls[j].get("entrega_em_progresso"):
                    j += 1
                antes = ls[i - 1] if i > 0 else ls[i]
                depois = ls[j] if j < len(ls) else ls[j - 1]
                subiu = _num(depois.get("volume")) - _num(antes.get("volume"))
                # a sonda marca "entrega" à toa (janelas sem mudança de volume, Florestal 17/10): só vale se o tanque SUBIU
                # de verdade — caminhão traz milhares de litros. Sem isso, um caminhão abastecendo 250 L no meio virava descarga.
                if subiu >= DESCARGA_MIN_L:
                    t0, t1 = _instante(antes.get("medido_em")), _instante(depois.get("medido_em"))
                    entrou += subiu + sum(v for q, v in vend if q and t0 and t1 and t0 <= q <= t1)
                i = j
            else:
                i += 1
        sistema = (abertura + entrou - vendido) if abertura is not None else None
        out.append({"numero": n, "produto": _combustivel(t.get("combustivel")), "capacidade": int(round(cap)),
                    "medidor": int(round(medidor)) if medidor is not None else None,
                    "sistema": int(round(sistema)) if sistema is not None else None,
                    "abertura": int(round(abertura)) if abertura is not None else None,
                    "entrou": int(round(entrou)), "vendido": int(round(vendido)),
                    "cabe": int(round(max(0.0, cap - medidor))) if (medidor is not None and cap > 0) else None,
                    "pct": round(medidor / cap * 100) if (medidor is not None and cap > 0) else None,
                    "baixo": bool(medidor is not None and cap > 0 and medidor / cap * 100 <= TANQUE_BAIXO_PCT)})
    medido = max((_instante(ls[-1].get("medido_em")) for ls in por_tq.values() if ls), default=None)
    return out, _iso_utc(medido)


def _estoque_posto(eid):
    def busca():
        agora = _agora_local()
        hoje = agora.strftime("%Y-%m-%d")
        meia_noite = _utc_do_dia(hoje)
        f = {"tanques": _POOL.submit(cp._sget, f"oct_tanques?select=id,numero,combustivel,capacidade&empresa_id=eq.{eid}&ativo=is.true&order=numero"),
             "leituras": _POOL.submit(_tudo, "oct_medicoes?select=tanque_numero,volume,altura,medido_em,entrega_em_progresso"
                                      f"&empresa_id=eq.{eid}&medido_em=gte.{meia_noite}&order=medido_em", 12),
             "ontem": _POOL.submit(cp._sget, f"oct_medicoes?select=tanque_numero,volume,altura&empresa_id=eq.{eid}"
                                   f"&medido_em=lt.{meia_noite}&volume=gt.0&altura=gt.0&order=medido_em.desc&limit=60"),
             "pista": _POOL.submit(_pista, eid, hoje, hoje)}
        r = {k: v.result(timeout=60) for k, v in f.items()}
        ab = {}
        for m in r["ontem"]:
            try:
                ab.setdefault(int(m.get("tanque_numero")), _num(m.get("volume")))
            except Exception:
                pass
        return estoque_do_dia(r["tanques"], r["leituras"], r["pista"], ab, agora)
    return _guardado("g-estoque|" + eid, 60, busca)


@bp_equipe.route("/app/equipe/api/estoque", methods=["GET"])
def api_estoque():
    p = _eu()
    if not p:
        return _nao_entrou()
    if not pode_de(p).get("ver_tanques"):
        return jsonify({"erro": "Seu perfil não vê os tanques."}), 403
    postos, meus, erro = _postos_da_gestao(p)
    if erro:
        return jsonify({"erro": erro}), 400
    out = []
    for eid, nome in postos:
        try:
            tq, medido = _estoque_posto(eid)
            out.append({"id": eid, "nome": nome, "medido_em": medido, "tanques": tq})
        except Exception as e:
            print(f"[app-equipe] estoque {eid}: {str(e)[:140]}")
            out.append({"id": eid, "nome": nome, "medido_em": None, "tanques": [], "erro": "não consegui ler agora"})
    return jsonify({"ok": True, "postos": [[i, n] for i, n in meus], "estoque": out})


# ---- DESCARGAS (17/10/2026, pedido do Ronan: "histórico de descarga para identificar se houve ou não a entrega" — e, em
# seguida, "não precisa ter casamento com nota, pode ser só a variação identificada no medidor")
# Entrega = registro da SONDA do posto (oct_sonda_entregas: console do medidor e detector do núcleo, conferidos contra o PDF
# do console, ≤ 20 L). Não usa oct_nfe_descarga: em 17/10 ela cortava a descarga ao meio (AC T3 13/10: sonda 4.974 L,
# tabela 2.571 L).
def _entregas_unicas(linhas):
    """oct_sonda_entregas traz a MESMA descarga duas vezes (console do medidor + detector do núcleo): junta por tanque e
    início a até 20 min, ficando com a do console."""
    out = []
    for x in sorted(linhas, key=lambda r: (0 if r.get("origem") == "console" else 1, str(r.get("inicio")))):
        ini = _instante(x.get("inicio"))
        if not ini:
            continue
        vol = _num(x.get("entregue")) or (_num(x.get("vol_fim")) - _num(x.get("vol_ini")))
        if vol < 150:
            continue
        tq = int(_num(x.get("tanque_numero")))
        if any(o["tanque"] == tq and abs((o["inicio"] - ini).total_seconds()) <= 1200 for o in out):
            continue
        out.append({"tanque": tq, "inicio": ini, "fim": _instante(x.get("fim")), "litros": vol,
                    "antes": _num(x.get("vol_ini")), "depois": _num(x.get("vol_fim"))})
    return out


def _descargas_posto(eid, de, ate):
    hoje = _agora_local().strftime("%Y-%m-%d")

    def busca():
        f = {"entregas": _POOL.submit(cp._sget, "oct_sonda_entregas?select=tanque_numero,inicio,fim,vol_ini,vol_fim,entregue,origem"
                                      f"&empresa_id=eq.{eid}&inicio=gte.{_utc_do_dia(de)}&inicio=lt.{_utc_do_dia(_seguinte(ate))}"
                                      "&order=inicio&limit=1000"),
             "tanques": _POOL.submit(cp._sget, f"oct_tanques?select=numero,combustivel&empresa_id=eq.{eid}&order=numero"),
             "algum": _POOL.submit(cp._sget, f"oct_sonda_entregas?select=id&empresa_id=eq.{eid}&limit=1")}
        r = {k: v.result(timeout=60) for k, v in f.items()}
        nome_tq = {int(_num(t.get("numero"))): _combustivel(t.get("combustivel")) for t in r["tanques"]}
        return _entregas_unicas(r["entregas"]), nome_tq, bool(r["algum"])
    return _guardado(f"g-desc|{eid}|{de}|{ate}", 120 if ate >= hoje else 1800, busca)


@bp_equipe.route("/app/equipe/api/descargas", methods=["GET"])
def api_descargas():
    p = _eu()
    if not p:
        return _nao_entrou()
    if not pode_de(p).get("ver_tanques"):
        return jsonify({"erro": "Seu perfil não vê os tanques."}), 403
    de, ate, erro = _periodo_args()
    postos, meus, erro2 = _postos_da_gestao(p)
    if erro or erro2:
        return jsonify({"erro": erro or erro2}), 400
    descargas, sem_sonda = [], []
    try:
        futs = [(nome, _POOL_POSTOS.submit(_descargas_posto, eid, de, ate)) for eid, nome in postos]
        for nome, f in futs:
            entregas, nome_tq, tem_sonda = f.result(timeout=90)
            if not tem_sonda:
                sem_sonda.append(nome)
            for e in entregas:
                descargas.append({"posto": nome, "tanque": e["tanque"], "produto": nome_tq.get(e["tanque"], "Tanque %s" % e["tanque"]),
                                  "inicio": _iso_utc(e["inicio"]), "fim": _iso_utc(e["fim"]), "litros": round(e["litros"]),
                                  "antes": round(e["antes"]), "depois": round(e["depois"])})
    except Exception as e:
        print("[app-equipe] descargas:", str(e)[:160])
        return jsonify({"erro": "Não consegui ler as descargas agora. Tente de novo."}), 502
    descargas.sort(key=lambda d: d["inicio"] or "", reverse=True)
    return jsonify({"ok": True, "postos": [[i, n] for i, n in meus], "descargas": descargas, "sem_sonda": sem_sonda,
                    "resumo": {"descargas": len(descargas), "litros": sum(d["litros"] for d in descargas)}})


def _crachas():
    """{crachá: nome} do cadastro (oct_pessoas.cartao_idf)."""
    def busca():
        out = {}
        for x in cp._sget("oct_pessoas?select=nome,cartao_idf&cartao_idf=not.is.null&limit=2000"):
            cr = str(x.get("cartao_idf") or "").strip().upper()
            if cr and x.get("nome"):
                partes = (cp._nome_bonito(x["nome"]) or x["nome"]).split()
                out[cr] = " ".join(partes[:1] + partes[-1:]) if len(partes) > 1 else partes[0]
        return out
    return _guardado("g-crachas", 600, busca)


@bp_equipe.route("/app/equipe/api/comissao", methods=["GET"])
def api_comissao():
    p = _eu()
    if not p:
        return _nao_entrou()
    if not pode_de(p).get("ver_comissao"):
        return jsonify({"erro": "Seu perfil não vê a comissão."}), 403
    de, ate, erro = _periodo_args()
    postos, meus, erro2 = _postos_da_gestao(p)
    if erro or erro2:
        return jsonify({"erro": erro or erro2}), 400
    try:
        s = somar_vendas(_coletar_periodo(postos, de, ate, com_fila=False))
        nomes = _crachas()
    except Exception as e:
        print("[app-equipe] comissao:", str(e)[:160])
        return jsonify({"erro": "Não consegui ler as vendas agora. Tente de novo."}), 502
    vs = [{"nome": nomes.get(cr) or ("Crachá …" + cr[-4:]), "posto": max(x["postos"], key=x["postos"].get) if x["postos"] else "",
           "litros": round(x["litros"], 1), "valor": _r2(x["valor"]), "abastecimentos": x["abastecimentos"], "comissao": None}
          for cr, x in s["vendedores"].items()]
    vs.sort(key=lambda v: -(v["valor"] or 0))
    sc = s["sem_cracha"]
    return jsonify({"ok": True, "postos": [[i, n] for i, n in meus], "configurada": False, "total": None, "vendedores": vs,
                    "sem_cracha": {"litros": round(sc[0], 1), "abastecimentos": sc[1]} if sc[1] else None})


# ------------------------------------------------------------------ aprovações
# ETAPA 1 — AFERIÇÃO. O PDV marca o abastecimento como aferição e ele fica 'afericao_pendente' em oct_pdv_abastecimentos até
# alguém decidir. O retaguarda decide (afericoes.js) gravando o status; o worker do núcleo lê o status na nuvem a cada 30 s
# e aplica no PDV. O app grava EXATAMENTE a mesma coisa — por isso nada muda no PDV nem no núcleo.
_AFE_COLS = ("id,empresa_id,turno_id,bico,bomba,combustivel,produto_nome,litros,preco_litro,valor_total,valor,vendedor,status,tipo,"
             "data_abast,data_mov,created_at,observacao")
_RE_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_RE_CRACHA = re.compile(r"^[0-9A-Fa-f]{10,}$")
AFERICAO_LITROS_NORMAL = 21.0            # a aferição usa vaso de até 20 L: acima disso a linha "Litros" vai em alerta


def _virg(v, dec=2):
    return f"{_num(v):.{dec}f}".replace(".", ",")


def _reais(v):
    return "R$ " + f"{_num(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _hora_local_txt(d_utc):
    return (d_utc + FUSO).strftime("%d/%m %H:%M") if d_utc else "—"


def _afe_operadores(rows):
    """{turno_id: (nome do operador, nº do turno)} — quem estava no caixa quando a aferição foi marcada
    (o campo `vendedor` da aferição é o crachá/ID da bomba, não um nome)."""
    ids = sorted({str(r.get("turno_id")) for r in rows if r.get("turno_id") and _RE_UUID.match(str(r.get("turno_id")))})
    if not ids:
        return {}
    try:
        t = cp._sget(f"oct_pdv_turnos?select=id,numero,operador&id=in.({','.join(ids)})")
    except Exception:
        return {}
    return {str(x.get("id")): (cp._nome_bonito(x.get("operador") or ""), x.get("numero")) for x in t if x.get("operador")}


def _afe_pedido(r, nomes, detalhe=False, turnos=None):
    eid = str(r.get("empresa_id") or "")
    op = (turnos or {}).get(str(r.get("turno_id")))
    quando = _instante(r.get("data_abast"), local=True) or _instante(r.get("data_mov")) or _instante(r.get("created_at"))
    comb = _combustivel(r.get("combustivel") or r.get("produto_nome"))
    litros = _num(r.get("litros"))
    cracha = str(r.get("vendedor") or "").strip()
    pediu = op[0] if op else ("Caixa" if (not cracha or _RE_CRACHA.match(cracha)) else cp._nome_bonito(cracha))
    pendente = r.get("status") == "afericao_pendente"
    out = {"id": "afe:" + str(r.get("id")), "tipo": "afericao", "titulo": f"Aferição · bico {r.get('bico') or '—'} · {comb}",
           "posto": nomes.get(eid, "—"), "pediu": pediu, "quando": _iso_utc(quando), "vence_em": None,
           "situacao": "pendente" if pendente else "aprovado"}
    if detalhe:
        valor = _num(r.get("valor_total")) or _num(r.get("valor")) or round(litros * _num(r.get("preco_litro")), 2)
        linhas = [["Posto", nomes.get(eid, "—")], ["Combustível", comb], ["Bico", str(r.get("bico") or "—")],
                  ["Litros", _virg(litros, 3 if litros < 1 else 2) + " L", "alerta" if litros > AFERICAO_LITROS_NORMAL else ""],
                  ["Valor se fosse venda", _reais(valor)], ["Horário", _hora_local_txt(quando)], ["Quem marcou", pediu]] + ([["Turno", str(int(op[1]))]] if op and op[1] is not None else [])
        out.update({"destaque": _virg(litros, 3 if litros < 1 else 2) + " L",
                    "resumo": f"{comb} · bico {r.get('bico') or '—'} · {_hora_local_txt(quando)}",
                    "linhas": linhas, "motivo": "",
                    "resolvido": "" if pendente else (r.get("observacao") or "Aferição autorizada.")})
        if litros > AFERICAO_LITROS_NORMAL:
            out["resumo"] += " · mais de 20 L: confira antes de aprovar"
    return out


def _afe_postos(p):
    """{empresa_id: nome} dos postos de quem pode aprovar aferição; vazio se o perfil não pode."""
    if not pode_de(p).get("aprovar_afericao"):
        return {}
    return {i: n for i, n in postos_de(p)}


def _pedidos_afericao(p, resolvidas):
    nomes = _afe_postos(p)
    if not nomes:
        return []
    ids = ",".join(nomes)
    if resolvidas:
        desde = _iso_utc(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30))
        q = (f"oct_pdv_abastecimentos?select={_AFE_COLS}&empresa_id=in.({ids})&status=eq.afericao_autorizada"
             f"&data_mov=gte.{desde}&order=data_mov.desc&limit=40")
    else:
        q = f"oct_pdv_abastecimentos?select={_AFE_COLS}&empresa_id=in.({ids})&status=eq.afericao_pendente&order=data_mov.desc&limit=100"
    rows = cp._sget(q)
    turnos = _afe_operadores(rows)
    return [_afe_pedido(r, nomes, turnos=turnos) for r in rows]


# ---- pedidos do CAIXA (desconto; a prazo na etapa seguinte): oct_app_aprovacoes, criados pelo núcleo do posto em até 1 minuto
_APV_COLS = ("id,empresa_id,tipo,status,titulo,destaque,resumo,linhas,motivo,pediu_por,criado_em,vence_em,decidido_nome,decidido_em,motivo_recusa")
_NOME_TIPO = {"afericao": "Aferição", "desconto": "Desconto", "prazo": "Venda a prazo"}


def _agora_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _apv_postos(p, tipo):
    """{empresa_id: nome} dos postos de quem pode aprovar ESTE tipo (desconto, prazo); vazio se não pode."""
    if not TIPOS_NO_AR.get(tipo) or not pode_de(p).get("aprovar_" + tipo):
        return {}
    return {i: n for i, n in postos_de(p)}


def _apv_pedido(r, nomes, detalhe=False):
    eid = str(r.get("empresa_id") or "")
    criado, vence, st = _instante(r.get("criado_em")), _instante(r.get("vence_em")), r.get("status")
    if st == "pendente":
        sit = "pendente" if (vence and vence > _agora_utc()) else "expirado"
    elif st in ("aprovado", "recusado"):
        sit = st
    else:                                   # expirado, cancelado
        sit = "expirado"
    out = {"id": "apv:" + str(r.get("id")), "tipo": r.get("tipo"), "titulo": r.get("titulo") or "Pedido do caixa",
           "posto": nomes.get(eid, "—"), "pediu": (cp._nome_bonito(r.get("pediu_por") or "") or "Caixa"),
           "quando": _iso_utc(criado), "vence_em": _iso_utc(vence) if sit == "pendente" else None, "situacao": sit}
    if detalhe:
        quem = cp._nome_bonito(r.get("decidido_nome") or "") if r.get("decidido_nome") else ""
        if sit == "aprovado":
            res = "Aprovado" + (f" por {quem}" if quem else "") + "."
        elif sit == "recusado":
            res = "Recusado" + (f" por {quem}" if quem else "") + "." + (f" Motivo: {r.get('motivo_recusa')}" if r.get("motivo_recusa") else "")
        elif st == "cancelado":
            res = "O caixa cancelou o pedido."
        else:
            res = "Venceu sem resposta."
        out.update({"destaque": r.get("destaque") or "", "resumo": r.get("resumo") or "", "linhas": r.get("linhas") or [],
                    "motivo": r.get("motivo") or "", "resolvido": "" if sit == "pendente" else res})
    return out


def _pedidos_caixa(p, resolvidas):
    """Pedidos do caixa dos tipos que o perfil aprova. Sem a tabela (SQL-APP-APROVACOES.sql) = lista vazia, sem derrubar o resto."""
    tipos = [t for t in ("desconto", "prazo") if _apv_postos(p, t)]
    if not tipos:
        return []
    nomes = {i: n for i, n in postos_de(p)}
    if not nomes:
        return []
    ids, lim = ",".join(nomes), "tipo=in.(" + ",".join(tipos) + ")"
    try:
        if resolvidas:
            desde = _iso_utc(_agora_utc() - timedelta(days=30))
            rows = cp._sget(f"oct_app_aprovacoes?select={_APV_COLS}&empresa_id=in.({ids})&{lim}&criado_em=gte.{desde}&order=criado_em.desc&limit=80")
            out = [_apv_pedido(r, nomes) for r in rows]
            return [x for x in out if x["situacao"] != "pendente"][:40]
        agora = _iso_utc(_agora_utc())
        rows = cp._sget(f"oct_app_aprovacoes?select={_APV_COLS}&empresa_id=in.({ids})&{lim}&status=eq.pendente&vence_em=gt.{agora}&order=criado_em.desc&limit=50")
        return [_apv_pedido(r, nomes) for r in rows]
    except Exception as e:
        print("[app-equipe] pedidos do caixa:", str(e)[:100])
        return []


def _pedidos(p, resolvidas):
    lst = _pedidos_afericao(p, resolvidas) + _pedidos_caixa(p, resolvidas)
    lst.sort(key=lambda x: x.get("quando") or "", reverse=True)
    return lst


def _pendentes_resumo(p):
    """(quantos, texto) de pedidos esperando este usuário — alimenta o cartão do painel e a vigia do app (cache de poucos segundos)."""
    try:
        lst = _guardado("pend|" + str(p.get("id")), 4, lambda: _pedidos(p, False))
        if not lst:
            return 0, ""
        partes = sorted({f"{_NOME_TIPO.get(x.get('tipo'), 'Pedido')} · {x.get('posto')}" for x in lst})
        return len(lst), ", ".join(partes[:2]) + ("…" if len(partes) > 2 else "")
    except Exception as e:
        print("[app-equipe] pendentes:", str(e)[:100])
        return 0, ""


@bp_equipe.route("/app/equipe/api/pendentes", methods=["GET"])
def api_pendentes():
    p = _eu()
    if not p:
        return _nao_entrou()
    n, resumo = _pendentes_resumo(p)
    return jsonify({"ok": True, "pendentes": n, "resumo": resumo})


@bp_equipe.route("/app/equipe/api/aprovacoes", methods=["GET"])
def api_aprovacoes():
    p = _eu()
    if not p:
        return _nao_entrou()
    try:
        return jsonify(_pedidos(p, request.args.get("situacao") == "resolvidas"))
    except Exception as e:
        print("[app-equipe] aprovacoes:", str(e)[:120])
        return jsonify({"erro": "Não consegui ler os pedidos agora."}), 502


def _afe_carregar(p, pid):
    """(linha, nomes, resposta_de_erro) — confere formato, permissão e se o posto é de quem chama."""
    if not str(pid).startswith("afe:") or not _RE_UUID.match(str(pid)[4:]):
        return None, None, (jsonify({"erro": "Pedido não encontrado."}), 404)
    nomes = _afe_postos(p)
    if not nomes:
        return None, None, (jsonify({"erro": "Seu perfil não aprova aferição."}), 403)
    rows = cp._sget(f"oct_pdv_abastecimentos?select={_AFE_COLS}&id=eq.{str(pid)[4:]}&limit=1")
    if not rows or str(rows[0].get("empresa_id")) not in nomes:
        return None, None, (jsonify({"erro": "Pedido não encontrado."}), 404)
    return rows[0], nomes, None


def _apv_carregar(p, pid):
    """(linha, nomes, resposta_de_erro) de um pedido do caixa: formato, permissão do tipo e posto do usuário."""
    if not _RE_UUID.match(str(pid)[4:]):
        return None, None, (jsonify({"erro": "Pedido não encontrado."}), 404)
    rows = cp._sget(f"oct_app_aprovacoes?select={_APV_COLS}&id=eq.{str(pid)[4:]}&limit=1")
    if not rows:
        return None, None, (jsonify({"erro": "Pedido não encontrado."}), 404)
    r = rows[0]
    nomes = _apv_postos(p, str(r.get("tipo")))
    if not nomes:
        return None, None, (jsonify({"erro": "Seu perfil não aprova este tipo de pedido."}), 403)
    if str(r.get("empresa_id")) not in nomes:
        return None, None, (jsonify({"erro": "Pedido não encontrado."}), 404)
    return r, nomes, None


def _apv_detalhe(p, pid):
    try:
        r, nomes, erro = _apv_carregar(p, pid)
    except Exception as e:
        print("[app-equipe] pedido do caixa:", str(e)[:120])
        return jsonify({"erro": "Não consegui ler o pedido agora."}), 502
    if erro:
        return erro
    return jsonify(dict(_apv_pedido(r, nomes, detalhe=True), ok=True))


def _apv_decidir(p, pid, aprovar, motivo):
    try:
        r, nomes, erro = _apv_carregar(p, pid)
        if erro:
            return erro
        if r.get("status") != "pendente":
            msg = "O caixa cancelou o pedido." if r.get("status") == "cancelado" else "Esse pedido já foi resolvido."
            return jsonify({"erro": msg}), 409
        agora = _agora_utc()
        vence = _instante(r.get("vence_em"))
        if not vence or vence <= agora:
            return jsonify({"erro": "Passou 1 minuto sem resposta. O caixa precisa pedir de novo."}), 410
        quem = cp._nome_bonito(p.get("nome") or "gerente")
        corpo = {"status": "aprovado" if aprovar else "recusado", "decidido_por": p.get("id"), "decidido_nome": quem,
                 "decidido_em": _iso_utc(agora), "motivo_recusa": (motivo or None) if not aprovar else None}
        # a condição vai NO BANCO: só decide quem chegar antes do vencimento e enquanto estiver pendente
        feito = cp._spatch(f"oct_app_aprovacoes?id=eq.{str(pid)[4:]}&status=eq.pendente&vence_em=gt.{_iso_utc(agora)}", corpo, prefer="return=representation")
        if not feito:
            return jsonify({"erro": "Esse pedido já foi resolvido ou venceu."}), 409
        _esquecer("pend|")
        print(f"[app-equipe] pedido {str(pid)[4:]} ({r.get('tipo')}) {'APROVADO' if aprovar else 'RECUSADO'} por {quem} ({p.get('id')})")
        return jsonify({"ok": True, "situacao": "aprovado" if aprovar else "recusado"})
    except Exception as e:
        print("[app-equipe] decidir pedido do caixa:", str(e)[:120])
        return jsonify({"erro": "Não consegui registrar agora. Tente de novo."}), 502


@bp_equipe.route("/app/equipe/api/aprovacoes/<pid>", methods=["GET"])
def api_aprovacao(pid):
    p = _eu()
    if not p:
        return _nao_entrou()
    if str(pid).startswith("apv:"):
        return _apv_detalhe(p, pid)
    try:
        r, nomes, erro = _afe_carregar(p, pid)
    except Exception as e:
        print("[app-equipe] pedido:", str(e)[:120])
        return jsonify({"erro": "Não consegui ler o pedido agora."}), 502
    if erro:
        return erro
    return jsonify(dict(_afe_pedido(r, nomes, detalhe=True, turnos=_afe_operadores([r])), ok=True))


@bp_equipe.route("/app/equipe/api/aprovacoes/<pid>/decidir", methods=["POST"])
def api_aprovacao_decidir(pid):
    p = _eu()
    if not p:
        return _nao_entrou()
    d = request.get_json(silent=True) or {}
    aprovar = bool(d.get("aprovar"))
    motivo = re.sub(r"\s+", " ", str(d.get("motivo") or "")).strip()[:120]
    if str(pid).startswith("apv:"):
        return _apv_decidir(p, pid, aprovar, motivo)
    try:
        r, nomes, erro = _afe_carregar(p, pid)
        if erro:
            return erro
        if r.get("status") != "afericao_pendente":
            return jsonify({"erro": "Esse pedido já foi resolvido por outra pessoa."}), 409
        quem = cp._nome_bonito(p.get("nome") or "gerente")
        quando = _hora_local_txt(datetime.now(timezone.utc).replace(tzinfo=None))
        if aprovar:      # igual a afericoes.js › autorizar
            corpo = {"tipo": "afericao", "status": "afericao_autorizada", "preco_litro": 0, "valor": 0,
                     "observacao": f"Aferição autorizada - combustível retornado ao tanque (pelo app: {quem}, {quando})"}
        else:            # igual a afericoes.js › cancelar: volta a ser abastecimento vendável
            corpo = {"tipo": "abastecimento", "status": "pendente",
                     "observacao": f"Aferição recusada pelo app ({quem}, {quando})" + (f": {motivo}" if motivo else "")}
        feito = cp._spatch(f"oct_pdv_abastecimentos?id=eq.{str(pid)[4:]}&status=eq.afericao_pendente", corpo, prefer="return=representation")
        if not feito:
            return jsonify({"erro": "Esse pedido já foi resolvido por outra pessoa."}), 409
        _esquecer("pend|")
        print(f"[app-equipe] aferição {str(pid)[4:]} {'AUTORIZADA' if aprovar else 'RECUSADA'} por {quem} ({p.get('id')})")
        return jsonify({"ok": True, "situacao": "aprovado" if aprovar else "recusado"})
    except Exception as e:
        print("[app-equipe] decidir:", str(e)[:120])
        return jsonify({"erro": "Não consegui registrar agora. Tente de novo."}), 502


# ------------------------------------------------------------------ avisos no celular (por pessoa)
@bp_equipe.route("/app/equipe/api/avisos", methods=["POST"])
def api_avisos():
    p = _eu()
    if not p:
        return _nao_entrou()
    d = request.get_json(silent=True) or {}
    novos = {k: bool(d[k]) for k in AVISOS if k in d}
    if not novos:
        return jsonify({"erro": "Nada para salvar."}), 400
    uid = p["id"]
    _esquecer("prefs|" + uid)
    atual = dict(_prefs(uid).get("avisos") or {})
    atual.update(novos)
    try:
        cp._spost("oct_app_equipe?on_conflict=usuario_id", {"usuario_id": uid, "avisos": atual, "atualizado_em": _iso_utc(_agora_local() - FUSO)},
                  prefer="resolution=merge-duplicates,return=minimal")
    except Exception as e:
        falta = "oct_app_equipe" in str(e) or "404" in str(e)
        return jsonify({"erro": "Falta criar a tabela do app da equipe (SQL-APP-EQUIPE.sql)." if falta else "Não consegui salvar: " + str(e)[:80]}), 503
    _esquecer("prefs|" + uid)
    return jsonify({"ok": True, "avisos": atual})


# ------------------------------------------------------------------ permissões do app (só o dono)
@bp_equipe.route("/app/equipe/api/permissoes", methods=["GET"])
def api_permissoes():
    p = _eu()
    if not p:
        return _nao_entrou()
    if not _eh_dono(p):
        return jsonify({"erro": "Só o dono muda as permissões do app."}), 403
    ativas = _empresas_ativas()
    try:
        gente = cp._sget("oct_perfis?select=nome,papel,ativo,empresa_id,empresas&papel=in.(" + ",".join(k for k, _ in PERFIS) + ")&order=nome")
    except Exception as e:
        return jsonify({"erro": str(e)[:120]}), 502
    perfis = []
    for pid, nome in PERFIS:
        _esquecer("padrao|" + pid)                      # o dono sempre vê o que está gravado agora
        pessoas = []
        for g in gente:
            if g.get("papel") != pid or g.get("ativo") is False:
                continue
            ids = [str(e) for e in ([g.get("empresa_id")] + list(g.get("empresas") or [])) if e and str(e) in ativas]
            pessoas.append({"nome": cp._nome_bonito(g.get("nome") or "—"), "postos": sorted({ativas[i] for i in ids})})
        perfis.append({"id": pid, "nome": nome, "pode": dict(_padroes_do_perfil(pid)), "pessoas": pessoas})
    return jsonify({"ok": True, "perfis": perfis, "aprovacoes_no_ar": APROVACOES_NO_AR, "aprovacoes_tipos": dict(TIPOS_NO_AR)})


@bp_equipe.route("/app/equipe/api/permissoes", methods=["POST"])
def api_permissoes_gravar():
    p = _eu()
    if not p:
        return _nao_entrou()
    if not _eh_dono(p):
        return jsonify({"erro": "Só o dono muda as permissões do app."}), 403
    d = request.get_json(silent=True) or {}
    perfil, chave = str(d.get("perfil") or ""), str(d.get("chave") or "")
    if perfil not in dict(PERFIS) or chave not in PERMISSOES or not isinstance(d.get("ligado"), bool):
        return jsonify({"erro": "Permissão desconhecida."}), 400
    try:
        cp._spost("oct_perfis_permissoes?on_conflict=perfil,chave",
                  {"perfil": perfil, "chave": PREFIXO + chave, "liberado": d["ligado"], "atualizado_por": p["id"], "atualizado_em": _iso_utc(_agora_local() - FUSO)},
                  prefer="resolution=merge-duplicates,return=minimal")
    except Exception as e:
        return jsonify({"erro": "Não consegui salvar: " + str(e)[:80]}), 502
    _esquecer("padrao|" + perfil)
    return jsonify({"ok": True})


# ================================================================== AVISO NO CELULAR DA EQUIPE (16/10/2026)
# O gerente/dono recebe o pedido do caixa na tela do celular, com o app fechado (Firebase, app_push.enviar_um).
# Tabelas em repo/sql/SQL-APP-EQUIPE-PUSH.sql: oct_app_equipe_dispositivos (celulares de quem entrou no "Acesso da
# equipe") e oct_app_push_avisados (marca "pedido já avisado" — o servidor roda em 2 processos e só quem grava a marca
# primeiro manda). Quem recebe: cadastro ativo, com a permissão de aprovar AQUELE tipo, que enxerga AQUELE posto e
# com o aviso "pedidos" ligado na Conta (padrão: ligado). Sem as tabelas ou sem FIREBASE_SA a vigia fica parada.
AVISO_CICLO_SEG = 3                 # o pedido de desconto vence em 60 s: o aviso tem de sair logo
AVISO_AFERICAO_JANELA_H = 12        # aferição pendente de abastecimento mais velho que isso não gera aviso
_aviso_on = False
_aviso_vistos = {}                  # chave -> instante (não tenta gravar a marca de novo a cada volta)


def _log_aviso(msg):
    print(f"[equipe-aviso] {msg}", flush=True)


@bp_equipe.route("/app/equipe/api/dispositivo", methods=["POST"])
def api_dispositivo():
    """O app registra o celular de quem entrou no Acesso da equipe (chamado ao entrar e ao abrir o app)."""
    p = _eu()
    if not p:
        return _nao_entrou()
    d = request.get_json(silent=True) or {}
    token = str(d.get("token") or "").strip()
    plat = str(d.get("plataforma") or "").strip().lower()
    if len(token) < 20 or len(token) > 4096 or plat not in ("android", "ios"):
        return jsonify({"erro": "aparelho inválido"}), 400
    try:
        cp._spost("oct_app_equipe_dispositivos?on_conflict=token",
                  {"token": token, "usuario_id": p["id"], "plataforma": plat, "app_versao": str(d.get("app_versao") or "")[:30] or None,
                   "ativo": True, "falhas": 0, "visto_em": _iso_utc(_agora_utc())},
                  prefer="resolution=merge-duplicates,return=minimal")
    except Exception as e:
        falta = "oct_app_equipe_dispositivos" in str(e) or "404" in str(e)
        return jsonify({"erro": "Falta criar a tabela dos celulares da equipe (SQL-APP-EQUIPE-PUSH.sql)." if falta
                        else "Não consegui registrar o aparelho: " + str(e)[:80]}), 503
    _esquecer("eqdisp")
    return jsonify({"ok": True})


@bp_equipe.route("/app/equipe/api/dispositivo/sair", methods=["POST"])
def api_dispositivo_sair():
    """Ao sair do Acesso da equipe o celular para de receber os pedidos."""
    d = request.get_json(silent=True) or {}
    token = str(d.get("token") or "").strip()
    if len(token) >= 20:
        try:
            url, _ = cp._supa()
            rq.delete(f"{url}/rest/v1/oct_app_equipe_dispositivos?token=eq.{rq.utils.quote(token, safe='')}", headers=cp._sh(), timeout=20)
        except Exception:
            pass
        _esquecer("eqdisp")
    return jsonify({"ok": True})


def _aparelhos_equipe():
    """[(token, plataforma, perfil)] dos celulares ativos da equipe — 30 s em cache."""
    def busca():
        out = []
        for r in cp._sget("oct_app_equipe_dispositivos?ativo=eq.true&select=token,plataforma,usuario_id&limit=500"):
            try:
                p = _perfil(str(r.get("usuario_id")))
            except Exception:
                p = None
            if p and _entra(p):
                out.append((r["token"], r.get("plataforma"), p))
        return out
    return _guardado("eqdisp", 30, busca)


def destinatarios(eid, tipo, aparelhos=None):
    """Tokens que devem receber o pedido `tipo` do posto `eid`."""
    out = []
    for token, _plat, p in (aparelhos if aparelhos is not None else _aparelhos_equipe()):
        if not pode_de(p).get("aprovar_" + tipo):
            continue
        if eid not in {i for i, _ in postos_de(p)}:
            continue
        if (_prefs(p["id"]).get("avisos") or {}).get("pedidos", True) is False:
            continue
        out.append(token)
    return out


def texto_aviso(ped):
    """(título, texto) da notificação de um pedido montado por _apv_pedido/_afe_pedido(detalhe=True)."""
    posto = ped.get("posto") or "posto"
    if ped["tipo"] == "afericao":
        corpo = " · ".join(x for x in (ped.get("resumo") or ped.get("titulo") or "", ped.get("destaque") or "") if x)
        return f"Aferição para aprovar · {posto}", corpo + ". Toque para aprovar."
    partes = [ped.get("destaque") or "", ped.get("resumo") or "", ("pedido por " + ped["pediu"]) if ped.get("pediu") else ""]
    return f"{ped.get('titulo') or 'Pedido do caixa'} · {posto}", " · ".join(x for x in partes if x) + ". Responda em até 1 minuto."


def _marcar_avisado(chave):
    """True só para o processo que gravou a marca primeiro (o outro recebe lista vazia)."""
    return bool(cp._spost("oct_app_push_avisados?on_conflict=chave", {"chave": chave},
                          prefer="resolution=ignore-duplicates,return=representation"))


def _pedidos_novos():
    """[(chave, empresa_id, tipo, pedido detalhado)] pendentes agora que ainda não passaram por este processo."""
    agora = _agora_utc()
    ativas = _empresas_ativas()
    novos = []
    tipos = [t for t in ("desconto", "prazo") if TIPOS_NO_AR.get(t)]
    if tipos:
        rows = cp._sget(f"oct_app_aprovacoes?select={_APV_COLS}&status=eq.pendente&tipo=in.({','.join(tipos)})"
                        f"&vence_em=gt.{_iso_utc(agora)}&order=criado_em.desc&limit=50")
        for r in rows:
            ch = "apv:" + str(r.get("id"))
            if ch not in _aviso_vistos:
                novos.append((ch, str(r.get("empresa_id")), r.get("tipo"), _apv_pedido(r, ativas, detalhe=True)))
    if TIPOS_NO_AR.get("afericao"):
        desde = _iso_utc(agora - timedelta(hours=AVISO_AFERICAO_JANELA_H))
        rows = cp._sget(f"oct_pdv_abastecimentos?select={_AFE_COLS}&status=eq.afericao_pendente&created_at=gte.{desde}"
                        f"&order=created_at.desc&limit=50")
        rows = [r for r in rows if ("afe:" + str(r.get("id"))) not in _aviso_vistos]
        turnos = _afe_operadores(rows) if rows else {}
        for r in rows:
            novos.append(("afe:" + str(r.get("id")), str(r.get("empresa_id")), "afericao", _afe_pedido(r, ativas, detalhe=True, turnos=turnos)))
    return novos


def rodada_avisos(enviar=None):
    """Uma volta da vigia. `enviar(token, titulo, texto, dados)` -> (ok, erro, morto); padrão = Firebase (canal "aprovacoes")."""
    if enviar is None:
        import app_push
        enviar = lambda tok, tit, txt, dados: app_push.enviar_um(tok, tit, txt, None, dados, "aprovacoes")
    feitos, agora = 0, time.time()
    for ch in [k for k, t in _aviso_vistos.items() if agora - t > 86400]:
        _aviso_vistos.pop(ch, None)
    for ch, eid, tipo, ped in _pedidos_novos():
        _aviso_vistos[ch] = agora
        if ped.get("situacao") != "pendente":
            continue
        tokens = destinatarios(eid, tipo)
        if not tokens or not _marcar_avisado(ch):
            continue
        titulo, texto = texto_aviso(ped)
        dados = {"tipo": "pedido", "rota": "#/eq-pedido/" + ped["id"]}
        ok_n = 0
        for tok in tokens:
            ok, erro, morto = enviar(tok, titulo, texto, dados)
            ok_n += 1 if ok else 0
            if morto:
                try:
                    cp._spatch(f"oct_app_equipe_dispositivos?token=eq.{rq.utils.quote(tok, safe='')}", {"ativo": False})
                    _esquecer("eqdisp")
                except Exception:
                    pass
            elif not ok:
                _log_aviso(f"{ch}: falhou num aparelho: {erro}")
        try:
            cp._spatch(f"oct_app_push_avisados?chave=eq.{rq.utils.quote(ch, safe='')}", {"enviados": ok_n})
        except Exception:
            pass
        _log_aviso(f"{ch} ({tipo}) avisado em {ok_n}/{len(tokens)} celular(es)")
        feitos += 1
    return feitos


def iniciar_avisos():
    """Liga a vigia (uma thread por processo do gunicorn). EQUIPE_PUSH=0 desliga."""
    global _aviso_on
    if _aviso_on or os.environ.get("EQUIPE_PUSH", "1").strip().lower() in ("0", "false", "nao", "off"):
        return
    _aviso_on = True
    import threading

    def loop():
        import app_push
        time.sleep(20)
        avisou_parado = False
        while True:
            try:
                if not app_push.configurado():
                    if not avisou_parado:
                        _log_aviso("parado: falta FIREBASE_SA")
                        avisou_parado = True
                    time.sleep(60)
                    continue
                rodada_avisos()
            except Exception as e:
                msg = str(e)
                if "oct_app_push_avisados" in msg or "oct_app_equipe_dispositivos" in msg:
                    _log_aviso("parado: falta rodar SQL-APP-EQUIPE-PUSH.sql")
                    time.sleep(300)
                    continue
                _log_aviso(f"erro na volta: {msg[:160]}")
                time.sleep(15)
            time.sleep(AVISO_CICLO_SEG)

    threading.Thread(target=loop, daemon=True).start()
    _log_aviso(f"vigia dos pedidos ligada (a cada {AVISO_CICLO_SEG} s)")
