# -*- coding: utf-8 -*-
"""
app_indicacao.py — "Indique um amigo" do app Postos SN (31/10/2026).

Pedido do Ronan: "ao indicar um amigo e ele baixar o app e criar a conta a pessoa que indicou ganha o desconto
dobrado no próximo abastecimento ... depois de usar esse desconto volta ao desconto normal". Regras combinadas:
  * cada cliente tem um código de convite (oct_cashback_clientes.convite_codigo, nasce na 1ª vez que ele abre a
    tela); o amigo digita o código no cadastro, ou até `dias_codigo` dias depois na tela "Indicar amigos";
  * o bônus de quem indicou é LIBERADO no 1º abastecimento do amigo pelo app (cadastro sozinho é fácil de forjar);
    antes disso o card do Início mostra "<amigo> terminou o cadastro";
  * cada amigo = UM abastecimento com o desconto do app multiplicado (config.multiplicador, 2×), em fila;
  * o bônus entra no "Desconto agora" do bico (cashback_portal.api_acionar): o acionamento já sai com o R$/L
    multiplicado, e o núcleo e o PDV dos postos usam o valor do acionamento — nada muda neles;
  * só é gasto quando o abastecimento acontece (preço voltou com o abastecimento informado); não abasteceu, o
    desconto expirou ou falhou = o bônus volta a ficar disponível;
  * travas: o amigo nunca abasteceu pelo app, CPF/celular/chave Pix diferentes de quem indicou e não pode ser o
    mesmo celular (token de notificação) dos dois.
Tabelas: repo/sql/SQL-APP-INDICACAO.sql. Sem a tabela de config (SQL não rodou) ou com ela desligada, nada
aparece no app e o cadastro segue igual — menos para os CPFs de config.testadores (SQL-APP-INDICACAO-TESTE.sql), que
já veem e usam a campanha desligada (teste no celular antes de ligar para todos).

A cada minuto (os 2 workers do gunicorn rodam; o PATCH condicionado deixa só um fazer cada coisa):
  1. bônus reservados: abasteceu = usado; não abasteceu = volta a disponível;
  2. indicações à espera: amigo abasteceu pelo app = libera o bônus de quem indicou + aviso no celular.
"""

import os
import random
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests as rq
from flask import Blueprint, jsonify, request

import cashback_portal as cp

bp_indicacao = Blueprint("indicacao", __name__)

CICLO_SEG = 60
RESERVA_PERDIDA_S = 180      # bônus reservado sem acionamento gravado (o acionar falhou no meio): volta depois disso
RESTAURADO_ESPERA_S = 120    # preço voltou sem o abastecimento informado: procura pelo preço; nada achado depois disso = volta
RESERVA_MAX_H = 6            # pedido que o posto nunca atendeu (posto fora do ar): devolve depois disso
JANELA_DIAS = 120            # indicação que nunca abasteceu: a rotina para de olhar depois disso
_on = False
_cfg = {"ts": 0.0, "v": None}
_exemplos = {"ts": 0.0, "v": []}


def _log(msg):
    print(f"[app-indicacao] {msg}", flush=True)


def _agora():
    return datetime.now(timezone.utc)


def _q(s):
    return rq.utils.quote(str(s), safe="")


def _data(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def config(forcar=False):
    """Linha da campanha (cache de 60 s). Sem a tabela = desligada (o SQL ainda não rodou)."""
    if forcar or _cfg["v"] is None or time.time() - _cfg["ts"] > 60:
        try:
            rows = cp._sget("oct_app_indicacao_config?id=eq.1"
                            "&select=ativo,multiplicador,amigo_ganha,dias_codigo,testadores&limit=1")
            v = rows[0] if rows else None
        except Exception:
            v = None
        _cfg.update(ts=time.time(), v=v or {"ativo": False, "multiplicador": 2, "amigo_ganha": False, "dias_codigo": 7,
                                             "testadores": []})
    return _cfg["v"]


def ligada(cpf=None):
    """Campanha valendo para este cliente: ligada para todos ou, desligada, só nas contas de teste (01/11/2026, Ronan:
    "vamos testar primeiro no meu iPhone" — config.testadores = CPFs que já veem e usam a campanha)."""
    cfg = config()
    if cfg.get("ativo"):
        return True
    cpf = cp._so_digitos(cpf)
    return bool(cpf) and cpf in {cp._so_digitos(x) for x in cfg.get("testadores") or []}


def normalizar(codigo):
    s = unicodedata.normalize("NFKD", str(codigo or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]", "", s)[:16]


def _primeiro_nome(nome):
    p = (str(nome or "").strip().split() or [""])[0]
    return p[:1].upper() + p[1:].lower()


def _fone(s):
    return cp._so_digitos(s)[-8:]


# ------------------------------------------------------------------ código de convite
def codigo_do_cliente(cli):
    """O código do cliente (cria na 1ª vez): primeiro nome + 3 números, ex. RONAN482."""
    if cli.get("convite_codigo"):
        return cli["convite_codigo"]
    base = normalizar(_primeiro_nome(cli.get("nome")))[:8] or "SN"
    for tentativa in range(6):
        cod = base + str(random.randint(100, 999) if tentativa < 4 else random.randint(1000, 99999))
        if cp._sget(f"oct_cashback_clientes?convite_codigo=eq.{cod}&select=cpf&limit=1"):
            continue
        try:
            cp._spatch(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}&convite_codigo=is.null", {"convite_codigo": cod})
        except RuntimeError:
            continue                                   # outro cliente pegou o mesmo código no mesmo instante
        rows = cp._sget(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}&select=convite_codigo&limit=1")
        if rows and rows[0].get("convite_codigo"):
            cli["convite_codigo"] = rows[0]["convite_codigo"]
            return cli["convite_codigo"]
    return None


def _conferir(codigo, cpf, telefone, chave_pix):
    """O código vale para esta pessoa? -> (quem indicou, None) ou (None, mensagem)."""
    cod = normalizar(codigo)
    if not cod:
        return None, None
    rows = cp._sget(f"oct_cashback_clientes?convite_codigo=eq.{cod}"
                    "&select=cpf,nome,telefone,chave_pix,verificado_em&limit=1")
    if not rows or not rows[0].get("verificado_em"):
        return None, "Código de convite não encontrado. Confira com quem convidou você (ou deixe o campo em branco)."
    ind = rows[0]
    if ind["cpf"] == cpf:
        return None, "Você não pode usar o seu próprio código de convite."
    if telefone and _fone(telefone) and _fone(ind.get("telefone")) == _fone(telefone):
        return None, "Este convite não vale: o celular é o mesmo de quem convidou."
    if chave_pix and str(ind.get("chave_pix") or "").strip().lower() == str(chave_pix).strip().lower():
        return None, "Este convite não vale: a chave Pix é a mesma de quem convidou."
    if cp._sget(f"oct_cashback_acionamentos?cliente_cpf=eq.{cpf}&select=id&limit=1"):
        return None, "O convite vale só para quem ainda não abasteceu pelo app."
    if cp._sget(f"oct_app_indicacoes?indicado_cpf=eq.{cpf}&select=id&limit=1"):
        return None, "Este CPF já usou um código de convite."
    return ind, None


def conferir_no_cadastro(codigo, cpf, telefone, chave_pix):
    """Cadastro com código: (código normalizado, None) / (None, erro). Campanha desligada = ignora o código."""
    if not normalizar(codigo) or not ligada(cpf):
        return None, None
    ind, erro = _conferir(codigo, cpf, telefone, chave_pix)
    return (normalizar(codigo), None) if ind else (None, erro)


def _registrar(cli, codigo, ind):
    """Grava a indicação, o bônus do amigo (se a campanha dá) e avisa quem indicou."""
    cfg = config()
    nome = _primeiro_nome(cli.get("nome"))
    try:
        nova = cp._spost("oct_app_indicacoes", {"indicador_cpf": ind["cpf"], "indicado_cpf": cli["cpf"],
                                                "indicado_nome": nome[:40], "codigo": codigo})
    except RuntimeError as e:
        if "duplicate" in str(e) or "23505" in str(e):
            return None                                 # já indicado (dois toques, dois workers)
        raise
    ind_id = nova[0]["id"] if nova else None
    if ind_id and cfg.get("amigo_ganha"):
        try:
            cp._spost("oct_app_bonus", {"cliente_cpf": cli["cpf"], "origem": "indicado", "indicacao_id": ind_id,
                                        "multiplicador": cfg.get("multiplicador") or 2}, prefer="return=minimal")
        except Exception as e:
            _log(f"bônus do amigo não gravou ({cli['cpf'][:3]}…): {e}")
    _avisar(ind["cpf"], f"👋 {nome} entrou com o seu convite",
            f"Quando {nome} fizer o primeiro abastecimento pelo app, o seu próximo desconto sai dobrado.")
    return ind_id


def registrar(cli):
    """Conta confirmada (cashback_portal.api_cadastro_confirmar): o código digitado no cadastro vira indicação."""
    cod = normalizar(cli.get("convite_usado"))
    if not cod:
        return None
    try:
        cp._spatch(f"oct_cashback_clientes?cpf=eq.{cli['cpf']}", {"convite_usado": None})
    except Exception:
        pass
    if not ligada(cli["cpf"]):
        return None
    ind, erro = _conferir(cod, cli["cpf"], cli.get("telefone"), cli.get("chave_pix"))
    if not ind:
        _log(f"convite {cod} não vale para {cli['cpf'][:3]}…: {erro}")
        return None
    return _registrar(cli, cod, ind)


def _avisar(cpf, titulo, texto):
    try:
        import app_push
        app_push.avisar_cliente(cpf, titulo, texto, {"rota": "#/indicar"})
    except Exception as e:
        _log(f"aviso não saiu ({cpf[:3]}…): {e}")


# ------------------------------------------------------------------ bônus no bico
def _disponivel(cpf):
    rows = cp._sget(f"oct_app_bonus?cliente_cpf=eq.{cpf}&status=eq.disponivel"
                    "&select=id,multiplicador,origem&order=criado_em&limit=1")
    return rows[0] if rows else None


def multiplicar(info, mult):
    """Desconto do bico (cp._desconto_do_bico) com o bônus. None se não cabe (teto de R$ 1,00/L ou preço mínimo)."""
    normal = round(float(info["desconto_litro"]), 3)
    desc = round(min(normal * float(mult or 2), cp.DESCONTO_APP_MAX), 3)
    preco = round(float(info["preco_normal"]) - desc, 3)
    if desc <= normal or preco < 0.5:
        return None
    return {"desconto_litro": desc, "preco_desconto": preco, "desconto_normal": normal,
            "multiplicador": float(mult or 2)}


def para_o_bico(cpf, info):
    """O app pergunta o desconto do bico: com bônus disponível, mostra o valor multiplicado (sem reservar)."""
    if not info.get("ativo"):
        return None
    try:
        resolver(cpf)
        b = _disponivel(cpf)
        v = multiplicar(info, b["multiplicador"]) if b else None
        if not v:
            return None
        n = cp._sget(f"oct_app_bonus?cliente_cpf=eq.{cpf}&status=eq.disponivel&select=id")
        return dict(v, quantos=len(n), origem=b["origem"])
    except Exception:
        return None


def reservar(cpf, info, acionamento_id):
    """'Desconto agora' com bônus: reserva o 1º disponível para este acionamento. -> valores (com bonus_id) ou None."""
    try:
        resolver(cpf)
        b = _disponivel(cpf)
        v = multiplicar(info, b["multiplicador"]) if b else None
        if not v:
            return None
        pega = cp._spatch(f"oct_app_bonus?id=eq.{b['id']}&status=eq.disponivel",
                          {"status": "reservado", "acionamento_id": acionamento_id, "reservado_em": _agora().isoformat(),
                           "desconto_litro": v["desconto_litro"], "desconto_normal": v["desconto_normal"]},
                          prefer="return=representation")
        return dict(v, bonus_id=b["id"]) if pega else None
    except Exception as e:
        _log(f"reserva do bônus falhou ({cpf[:3]}…): {e}")
        return None


def devolver(bonus_id):
    try:
        cp._spatch(f"oct_app_bonus?id=eq.{bonus_id}&status=eq.reservado",
                   {"status": "disponivel", "acionamento_id": None, "reservado_em": None,
                    "desconto_litro": None, "desconto_normal": None})
    except Exception as e:
        _log(f"não devolveu o bônus {bonus_id}: {e}")


def _destino(b, ac):
    """Bônus reservado -> ('usado', abastecimento_id, litros) | ('disponivel', None, None) | (None, ...) = esperar."""
    agora = _agora()
    idade = (agora - (_data(b.get("reservado_em")) or agora)).total_seconds()
    if not ac:
        return ("disponivel", None, None) if idade > RESERVA_PERDIDA_S else (None, None, None)
    info = ac.get("bomba_info") if isinstance(ac.get("bomba_info"), dict) else {}
    bs = ac.get("bomba_status")
    ab = cp._uuid_ok(info.get("abastecimento_id"))
    if bs == "restaurado" and ab:
        return "usado", ab, None
    if bs == "restaurado":
        # o núcleo pode voltar o preço antes de ler o abastecimento: procura um do bico, depois do pedido, no preço do bônus
        try:
            achou = cp._sget(f"oct_pdv_abastecimentos?empresa_id=eq.{ac['empresa_id']}&bico=eq.{ac.get('bico')}"
                             f"&data_abast=gte.{_q(cp._hora_do_posto(ac['criado_em']))}"
                             "&select=id,litros,preco_litro&order=data_abast&limit=5")
        except Exception:
            achou = []
        alvo = float(ac.get("preco_desconto") or 0)
        um = next((a for a in achou if abs(float(a.get("preco_litro") or 0) - alvo) < 0.0015), None)
        if um:
            return "usado", um["id"], um.get("litros")
        voltou = (agora - (_data(ac.get("bomba_em")) or agora)).total_seconds()
        return ("disponivel", None, None) if voltou > RESTAURADO_ESPERA_S else (None, None, None)
    if bs in ("falhou", "expirado", "cancelado"):
        return "disponivel", None, None
    if idade > RESERVA_MAX_H * 3600:
        return ("usado", None, None) if ac.get("status") == "usado" else ("disponivel", None, None)
    return None, None, None


def resolver(cpf=None):
    """Fecha os bônus reservados: abasteceu = usado; não abasteceu = volta a disponível."""
    q = "oct_app_bonus?status=eq.reservado&select=id,cliente_cpf,acionamento_id,reservado_em&limit=200"
    if cpf:
        q += f"&cliente_cpf=eq.{cpf}"
    res = cp._sget(q)
    if not res:
        return 0
    ids = [b["acionamento_id"] for b in res if cp._uuid_ok(b.get("acionamento_id"))]
    acs = {}
    if ids:
        for a in cp._sget(f"oct_cashback_acionamentos?id=in.({','.join(ids)})"
                          "&select=id,status,bomba_status,bomba_info,bomba_em,criado_em,empresa_id,bico,preco_desconto"):
            acs[a["id"]] = a
    feitos = 0
    for b in res:
        dest, ab, litros = _destino(b, acs.get(b.get("acionamento_id")))
        if dest == "disponivel":
            devolver(b["id"])
            feitos += 1
        elif dest == "usado":
            if ab and litros is None:
                try:
                    r = cp._sget(f"oct_pdv_abastecimentos?id=eq.{ab}&select=litros&limit=1")
                    litros = r[0].get("litros") if r else None
                except Exception:
                    litros = None
            cp._spatch(f"oct_app_bonus?id=eq.{b['id']}&status=eq.reservado",
                       {"status": "usado", "usado_em": _agora().isoformat(), "litros": litros})
            feitos += 1
    return feitos


# ------------------------------------------------------------------ 1º abastecimento do amigo
def liberar():
    """Indicações à espera cujo amigo já abasteceu pelo app: libera o bônus de quem indicou."""
    corte = (_agora() - timedelta(days=JANELA_DIAS)).isoformat()
    pend = cp._sget(f"oct_app_indicacoes?status=eq.cadastrado&criado_em=gte.{_q(corte)}"
                    "&select=id,indicador_cpf,indicado_cpf,indicado_nome,criado_em&limit=500")
    if not pend:
        return 0
    cpfs = ",".join(sorted({p["indicado_cpf"] for p in pend}))
    # abasteceu pelo app = acionamento com bico que virou venda (usado) ou desconto na bomba com o abastecimento informado
    acs = cp._sget(f"oct_cashback_acionamentos?cliente_cpf=in.({cpfs})&bico=not.is.null"
                   "&or=(status.eq.usado,bomba_info->>abastecimento_id.not.is.null)"
                   "&select=id,cliente_cpf,criado_em&order=criado_em")
    cfg, feitos = config(), 0
    for p in pend:
        ini = _data(p["criado_em"])
        ac = next((a for a in acs if a["cliente_cpf"] == p["indicado_cpf"]
                   and (not ini or (_data(a["criado_em"]) or ini) >= ini - timedelta(minutes=5))), None)
        if not ac:
            continue
        # mesmo celular nos dois CPFs (token de notificação igual) = a pessoa indicou a si mesma
        devs = cp._sget(f"oct_app_dispositivos?cliente_cpf=in.({p['indicador_cpf']},{p['indicado_cpf']})&select=cliente_cpf,token")
        tok_a = {d["token"] for d in devs if d["cliente_cpf"] == p["indicador_cpf"]}
        tok_b = {d["token"] for d in devs if d["cliente_cpf"] == p["indicado_cpf"]}
        if tok_a & tok_b:
            cp._spatch(f"oct_app_indicacoes?id=eq.{p['id']}&status=eq.cadastrado",
                       {"status": "recusada", "motivo": "mesmo celular de quem indicou", "acionamento_id": ac["id"]})
            _log(f"indicação {p['id']} recusada: mesmo celular")
            continue
        pega = cp._spatch(f"oct_app_indicacoes?id=eq.{p['id']}&status=eq.cadastrado",
                          {"status": "abasteceu", "abasteceu_em": _agora().isoformat(), "acionamento_id": ac["id"]},
                          prefer="return=representation")
        if not pega:
            continue                                    # o outro worker já fez
        try:
            cp._spost("oct_app_bonus", {"cliente_cpf": p["indicador_cpf"], "origem": "indicou", "indicacao_id": p["id"],
                                        "multiplicador": cfg.get("multiplicador") or 2}, prefer="return=minimal")
        except RuntimeError as e:
            if "duplicate" not in str(e) and "23505" not in str(e):
                raise
            continue
        nome = p.get("indicado_nome") or "Seu amigo"
        _avisar(p["indicador_cpf"], "⛽ Desconto dobrado liberado!",
                f"{nome} fez o primeiro abastecimento pelo app. No seu próximo abastecimento com o desconto do app, "
                "o desconto sai em dobro.")
        feitos += 1
    return feitos


# ------------------------------------------------------------------ o que o app mostra
def _exemplos_desconto():
    """Onde o desconto do app vale agora (para o card dizer quanto fica o desconto dobrado). Cache de 60 s."""
    if time.time() - _exemplos["ts"] < 60:
        return _exemplos["v"]
    out = []
    try:
        cabs = [c["empresa_id"] for c in cp._sget("oct_app_desconto?ativo=eq.true&select=empresa_id")]
        if cabs:
            itens = cp._sget(f"oct_app_desconto_itens?empresa_id=in.({','.join(cabs)})&desconto_litro=gt.0"
                             "&select=empresa_id,produto_id,desconto_litro")
            if itens:
                prods = {p["id"]: p for p in cp._sget(f"oct_produtos?id=in.({','.join({i['produto_id'] for i in itens})})"
                                                      "&select=id,nome,preco_venda_a")}
                emps = {e["id"]: e for e in cp._sget(f"oct_empresas?id=in.({','.join(cabs)})&select=id,nome,nome_fantasia")}
                for i in itens:
                    p, e = prods.get(i["produto_id"]) or {}, emps.get(i["empresa_id"]) or {}
                    desc = round(float(i["desconto_litro"]), 3)
                    out.append({"posto": cp._nome_bonito(e.get("nome_fantasia") or e.get("nome") or "Posto"),
                                "combustivel": cp._nome_bonito(p.get("nome") or ""), "desconto": desc,
                                "preco_normal": float(p["preco_venda_a"]) if p.get("preco_venda_a") else None})
    except Exception:
        out = _exemplos["v"]
    _exemplos.update(ts=time.time(), v=out)
    return out


def _pode_digitar(cli):
    """Quem criou a conta há pouco (sem código) ainda pode digitar o código de quem convidou."""
    cfg = config()
    criado = _data(cli.get("verificado_em") or cli.get("criado_em"))
    if not criado or _agora() - criado > timedelta(days=int(cfg.get("dias_codigo") or 0)):
        return False
    if cp._sget(f"oct_app_indicacoes?indicado_cpf=eq.{cli['cpf']}&select=id&limit=1"):
        return False
    return not cp._sget(f"oct_cashback_acionamentos?cliente_cpf=eq.{cli['cpf']}&select=id&limit=1")


@bp_indicacao.route("/cashback/api/indicacao/ativa", methods=["GET"])
def api_ativa():
    """Público (tela de cadastro): a campanha está valendo? (mostra o campo do código)"""
    cfg = config()
    return jsonify({"ok": True, "ativo": bool(cfg.get("ativo")), "multiplicador": float(cfg.get("multiplicador") or 2)})


@bp_indicacao.route("/cashback/api/indicacao", methods=["GET"])
def api_indicacao():
    """Card do Início e tela "Indicar amigos": código, amigos e bônus do cliente logado."""
    cli = cp._cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    cfg = config()
    ativo = ligada(cli["cpf"])
    try:
        resolver(cli["cpf"])
        bonus = cp._sget(f"oct_app_bonus?cliente_cpf=eq.{cli['cpf']}"
                         "&select=id,origem,status,multiplicador,indicacao_id,desconto_litro,litros,usado_em,criado_em&order=criado_em")
        amigos = cp._sget(f"oct_app_indicacoes?indicador_cpf=eq.{cli['cpf']}"
                          "&select=id,indicado_nome,status,criado_em,abasteceu_em&order=criado_em.desc&limit=50")
    except Exception:
        bonus, amigos = [], []          # SQL ainda não rodou
    if not ativo and not bonus and not amigos:
        return jsonify({"ok": True, "ativo": False})
    por_ind = {b["indicacao_id"]: b for b in bonus if b.get("origem") == "indicou"}
    ex = _exemplos_desconto()
    mult = float(cfg.get("multiplicador") or 2)
    return jsonify({
        "ok": True, "ativo": ativo, "teste": ativo and not cfg.get("ativo"),
        "multiplicador": mult, "amigo_ganha": bool(cfg.get("amigo_ganha")),
        "codigo": codigo_do_cliente(cli) if ativo else cli.get("convite_codigo"),
        "link": "https://www.redesn.com.br/baixar?o=convite",
        "disponiveis": sum(1 for b in bonus if b["status"] == "disponivel"),
        "reservado": any(b["status"] == "reservado" for b in bonus),
        "usados": sum(1 for b in bonus if b["status"] == "usado"),
        "ganhou_como_amigo": any(b.get("origem") == "indicado" and b["status"] != "usado" for b in bonus),
        "amigos": [{"nome": a.get("indicado_nome") or "Amigo", "status": a["status"], "quando": a.get("criado_em"),
                    "abasteceu_em": a.get("abasteceu_em"),
                    "bonus": (por_ind.get(a["id"]) or {}).get("status")} for a in amigos],
        "descontos": [dict(e, dobrado=round(min(e["desconto"] * mult, cp.DESCONTO_APP_MAX), 3)) for e in ex],
        "pode_digitar": ativo and _pode_digitar(cli),
    })


@bp_indicacao.route("/cashback/api/indicacao/codigo", methods=["POST"])
def api_usar_codigo():
    """Quem criou a conta sem o código ainda pode digitar (até dias_codigo dias, sem ter abastecido pelo app)."""
    cli = cp._cliente_do_token()
    if not cli:
        return jsonify({"erro": "sessão expirada"}), 401
    if not cp._limite_ok("convite:" + cp._ip(), 20, 3600):
        return cp._muitas()
    if not ligada(cli["cpf"]):
        return jsonify({"erro": "A campanha de indicação não está valendo agora."}), 409
    if not _pode_digitar(cli):
        return jsonify({"erro": "O código de convite vale só para conta nova que ainda não abasteceu pelo app."}), 409
    cod = normalizar((request.get_json(silent=True) or {}).get("codigo"))
    ind, erro = _conferir(cod, cli["cpf"], cli.get("telefone"), cli.get("chave_pix"))
    if not ind:
        return jsonify({"erro": erro or "Digite o código de convite."}), 400
    _registrar(cli, cod, ind)
    return jsonify({"ok": True, "quem": _primeiro_nome(ind.get("nome"))})


def ao_excluir(cpf):
    """Conta excluída: some o bônus que não foi usado e o nome dela na lista de quem indicou."""
    try:
        url, _ = cp._supa()
        rq.delete(f"{url}/rest/v1/oct_app_bonus?cliente_cpf=eq.{cpf}&status=neq.usado", headers=cp._sh(), timeout=20)
        cp._spatch(f"oct_app_indicacoes?indicado_cpf=eq.{cpf}", {"indicado_nome": None})
    except Exception:
        pass


# ------------------------------------------------------------------ rotina
def rodada():
    # roda mesmo com a campanha desligada: bônus já prometido e amigo que se cadastrou durante a campanha valem
    return resolver() + liberar()


def iniciar():
    global _on
    if _on or os.environ.get("APP_INDICACAO", "1").strip().lower() in ("0", "false", "nao", "off"):
        return
    _on = True

    def loop():
        time.sleep(30)
        while True:
            try:
                rodada()
            except Exception as e:
                msg = str(e)
                if "Could not find" not in msg and "does not exist" not in msg:   # SQL ainda não rodou: quieto
                    _log(f"erro na rodada: {e}")
            time.sleep(CICLO_SEG)

    threading.Thread(target=loop, daemon=True).start()
    _log(f"indicações ativas (a cada {CICLO_SEG} s)")
