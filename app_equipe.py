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

# Tipos de pedido que JÁ chegam ao app. Aferição usa o que já existe (oct_pdv_abastecimentos.status = 'afericao_pendente' e a mesma
# decisão do retaguarda); desconto e a prazo dependem do PDV pedir pelo servidor (próxima etapa).
TIPOS_NO_AR = {"afericao": True, "desconto": False, "prazo": False}
APROVACOES_NO_AR = any(TIPOS_NO_AR.values())
PERMISSOES = ("aprovar_desconto", "aprovar_prazo", "aprovar_afericao",
              "ver_vendas", "ver_litros", "ver_tanques", "ver_recebido", "ver_precos", "ver_alertas")
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
            return r[0] if r else {}
        except Exception:
            return {}                               # SQL-APP-EQUIPE.sql ainda não rodado
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
    try:
        p = _perfil(uid)
    except Exception:
        return None
    if not _entra(p) or int(_prefs(uid).get("token_versao") or 0) != ver:
        return None
    return p


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
                         f"&empresa_id=eq.{eid}&data_abast=gte.{hoje}&tipo=not.eq.afericao&order=data_abast.desc&limit=1000&offset={pg * 1000}")
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


def _pedidos(p, resolvidas):
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


def _pendentes_resumo(p):
    """(quantos, texto) de pedidos esperando este usuário — alimenta o cartão do painel (cache curto)."""
    try:
        nomes = _afe_postos(p)
        if not nomes:
            return 0, ""
        def busca():
            return cp._sget(f"oct_pdv_abastecimentos?select=id,empresa_id&empresa_id=in.({','.join(nomes)})&status=eq.afericao_pendente&limit=100")
        rows = _guardado("pend|" + ",".join(sorted(nomes)), 20, busca)
        if not rows:
            return 0, ""
        postos = sorted({nomes.get(str(r.get("empresa_id")), "") for r in rows} - {""})
        return len(rows), ("Aferição · " + ", ".join(postos)) if postos else "Aferição"
    except Exception as e:
        print("[app-equipe] pendentes:", str(e)[:100])
        return 0, ""


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


@bp_equipe.route("/app/equipe/api/aprovacoes/<pid>", methods=["GET"])
def api_aprovacao(pid):
    p = _eu()
    if not p:
        return _nao_entrou()
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
