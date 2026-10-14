# -*- coding: utf-8 -*-
"""
sefaz/bloqueio_prazo.py — BLOQUEIO AUTOMÁTICO do crédito a prazo por FATURA VENCIDA (14/10/2026, pedido do Ronan:
"pode bloquear no dia posterior ao vencimento da fatura, para todos os postos").

Regra (a cada BLOQUEIO_PRAZO_MIN minutos, padrão 10):
  * cliente com ao menos uma fatura ABERTA cujo vencimento é ANTERIOR a hoje (ou seja: no dia seguinte ao vencimento)
    -> oct_pessoas.credito_bloqueado = true, origem 'fatura', com o motivo e a hora;
  * cliente que foi bloqueado POR ESTA REGRA (origem 'fatura') e não tem mais fatura vencida (pagou, a fatura foi
    cancelada ou o vencimento mudou) -> desbloqueia sozinho;
  * NUNCA toca em bloqueio manual (credito_bloqueado = true com origem nula/'manual': protestados etc.) — nem para
    bloquear de novo, nem para liberar;
  * LIBERAÇÃO MANUAL "COLA": se alguém destrava na retaguarda um cliente que esta regra bloqueou, a regra percebe
    (credito_bloqueado=false com origem ainda 'fatura'), marca origem 'fatura_liberada' guardando as faturas que
    estavam vencidas e NÃO rebloqueia por elas; só volta a bloquear se aparecer fatura vencida NOVA. Quando o cliente
    quita tudo, a marca some.

O PDV, o app e o portal já respeitam oct_pessoas.credito_bloqueado (pdvAnalisarCredito / _prazo_liberado).

LIGA/DESLIGA (env no Railway, octano-sefaz): BLOQUEIO_PRAZO_FATURA = lista de empresa_id separados por vírgula, ou
`all`. Vazio = DESLIGADO. BLOQUEIO_PRAZO_SIMULAR=1 só registra no log o que faria. Trava de segurança: uma rodada
que bloquearia mais de BLOQUEIO_PRAZO_MAX (padrão 150) clientes numa empresa não grava nada (provável erro de dado).
Pré-requisito: repo/sql/SQL-BLOQUEIO-PRAZO-FATURA.sql (colunas credito_bloqueio_origem/_motivo/credito_bloqueado_em).
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests as rq

CICLO_SEG = max(1, int(os.environ.get("BLOQUEIO_PRAZO_MIN", "10") or 10)) * 60
ORIGEM = "fatura"
LIBERADA = "fatura_liberada"
_on = False


def _log(msg):
    print(f"[bloqueio-prazo] {msg}", flush=True)


def _supa():
    return (os.environ.get("SUPABASE_URL", "").strip().rstrip("/"),
            os.environ.get("SUPABASE_SERVICE_KEY", "").strip())


def _h(extra=None):
    _, key = _supa()
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def _get(q, passo=1000):
    out, ini = [], 0
    url, _ = _supa()
    while True:
        r = rq.get(f"{url}/rest/v1/{q}", headers=_h({"Range-Unit": "items", "Range": f"{ini}-{ini + passo - 1}"}), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        lote = r.json()
        out.extend(lote)
        if len(lote) < passo:
            return out
        ini += passo


def _patch(q, corpo):
    url, _ = _supa()
    r = rq.patch(f"{url}/rest/v1/{q}", headers=_h({"Prefer": "return=minimal"}), json=corpo, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")


def ligados():
    v = os.environ.get("BLOQUEIO_PRAZO_FATURA", "").strip().lower()
    if not v:
        return set()
    if v in ("all", "todos", "1", "true"):
        return {"*"}
    return {x.strip() for x in v.split(",") if x.strip()}


def _simular():
    return os.environ.get("BLOQUEIO_PRAZO_SIMULAR", "").strip().lower() in ("1", "true", "sim", "on")


def hoje_brasil():
    return datetime.now(timezone(timedelta(hours=-3))).date().isoformat()   # sem horário de verão desde 2019


def _valor(f):
    v = f.get("valor_liquido")
    return float(v if v is not None else (f.get("valor") or 0))


def _fmt_data(iso):
    return f"{iso[8:10]}/{iso[5:7]}/{iso[0:4]}"


def _ids_liberados(motivo):
    t = str(motivo or "")
    if "|ids:" not in t:
        return set()
    return {x for x in t.split("|ids:", 1)[1].split("|")[0].strip().split(",") if x}


def planejar(faturas, pessoas, hoje):
    """Função PURA. faturas: abertas da empresa; pessoas: {id: {credito_bloqueado, credito_bloqueio_origem,
    credito_bloqueio_motivo, ativo}} (as envolvidas + as já bloqueadas por esta regra).
    -> {"bloquear": [(id, motivo)], "atualizar": [(id, motivo)], "liberar": [id], "vencidas": {id: {...}},
        "liberada": [(id, motivo)] (liberação manual detectada: grava a marca), "limpar": [id] (marca sem fatura vencida)}"""
    venc = {}
    for f in faturas:
        v = str(f.get("vencimento") or "")[:10]
        cid = f.get("cliente_id")
        if not cid or not v or v >= hoje:
            continue
        d = venc.setdefault(cid, {"qtd": 0, "valor": 0.0, "mais_antiga": v, "ids": set()})
        d["ids"].add(str(f.get("id")))
        d["qtd"] += 1
        d["valor"] += _valor(f)
        d["mais_antiga"] = min(d["mais_antiga"], v)

    def motivo(d):
        return (f"Fatura vencida: {d['qtd']} fatura(s), R$ {d['valor']:.2f}; a mais antiga venceu em "
                f"{_fmt_data(d['mais_antiga'])}")

    def motivo_liberado(d):
        return "Liberado manualmente (fatura vencida) |ids:" + ",".join(sorted(d["ids"]))

    bloquear, atualizar, liberar, liberada, limpar = [], [], [], [], []
    for cid, d in venc.items():
        p = pessoas.get(cid)
        if not p or p.get("ativo") is False:
            continue
        origem = p.get("credito_bloqueio_origem")
        if p.get("credito_bloqueado") is True:
            if origem == ORIGEM and p.get("credito_bloqueio_motivo") != motivo(d):
                atualizar.append((cid, motivo(d)))          # já é nosso: só mantém o texto/valor em dia
            continue                                         # bloqueio manual: não toca
        if origem == ORIGEM:                                 # destravado à mão: a liberação cola
            liberada.append((cid, motivo_liberado(d)))
            continue
        if origem == LIBERADA:
            if d["ids"] - _ids_liberados(p.get("credito_bloqueio_motivo")):
                bloquear.append((cid, motivo(d)))            # fatura vencida NOVA depois da liberação
            continue
        bloquear.append((cid, motivo(d)))
    for pid, p in pessoas.items():
        if pid in venc:
            continue
        origem = p.get("credito_bloqueio_origem")
        if p.get("credito_bloqueado") is True and origem == ORIGEM:
            liberar.append(pid)
        elif p.get("credito_bloqueado") is not True and origem in (ORIGEM, LIBERADA):
            limpar.append(pid)                               # quitou depois de liberado: some a marca
    return {"bloquear": bloquear, "atualizar": atualizar, "liberar": liberar, "vencidas": venc,
            "liberada": liberada, "limpar": limpar}


def _lotes(lst, n=40):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def rodada_empresa(emp, aplicar=True, hoje=None):
    hoje = hoje or hoje_brasil()
    faturas = _get(f"oct_faturas?empresa_id=eq.{emp}&status=eq.aberta&select=id,cliente_id,numero,vencimento,valor,valor_liquido")
    ids = sorted({f["cliente_id"] for f in faturas if f.get("cliente_id")})
    cols = "id,credito_bloqueado,credito_bloqueio_origem,credito_bloqueio_motivo,ativo"
    pessoas = {}
    for lote in _lotes(ids):
        for p in _get(f"oct_pessoas?id=in.({','.join(lote)})&select={cols}"):
            pessoas[p["id"]] = p
    # os que ESTA regra bloqueou e talvez já possam sair (não têm mais fatura aberta alguma)
    for p in _get(f"oct_pessoas?empresa_id=eq.{emp}&credito_bloqueio_origem=in.({ORIGEM},{LIBERADA})&select={cols}"):
        pessoas[p["id"]] = p
    plano = planejar(faturas, pessoas, hoje)
    maximo = int(os.environ.get("BLOQUEIO_PRAZO_MAX", "150") or 150)
    if len(plano["bloquear"]) > maximo:
        _log(f"{emp}: a rodada bloquearia {len(plano['bloquear'])} clientes (> {maximo}): NADA gravado (confira os dados)")
        plano["recusado"] = True
        return plano
    if not aplicar or _simular():
        if plano["bloquear"] or plano["liberar"] or plano["atualizar"] or plano["liberada"] or plano["limpar"]:
            _log(f"{emp}: [simulacao] bloquearia {len(plano['bloquear'])}, liberaria {len(plano['liberar'])}, "
                 f"atualizaria o motivo de {len(plano['atualizar'])}, marcaria {len(plano['liberada'])} liberacao(oes) manual(is)")
        return plano
    agora = datetime.now(timezone.utc).isoformat()
    for cid, mot in plano["bloquear"]:
        _patch(f"oct_pessoas?id=eq.{cid}", {"credito_bloqueado": True, "credito_bloqueio_origem": ORIGEM,
                                            "credito_bloqueio_motivo": mot, "credito_bloqueado_em": agora})
    for cid, mot in plano["atualizar"]:
        _patch(f"oct_pessoas?id=eq.{cid}", {"credito_bloqueio_motivo": mot})
    for pid in plano["liberar"] + plano["limpar"]:
        _patch(f"oct_pessoas?id=eq.{pid}", {"credito_bloqueado": False, "credito_bloqueio_origem": None,
                                            "credito_bloqueio_motivo": None, "credito_bloqueado_em": None})
    for cid, mot in plano["liberada"]:
        _patch(f"oct_pessoas?id=eq.{cid}", {"credito_bloqueio_origem": LIBERADA, "credito_bloqueio_motivo": mot,
                                            "credito_bloqueado_em": None})
    if plano["bloquear"] or plano["liberar"] or plano["liberada"]:
        _log(f"{emp}: bloqueou {len(plano['bloquear'])}, liberou {len(plano['liberar'])}, "
             f"liberacao manual respeitada {len(plano['liberada'])}")
    return plano


def rodada():
    lig = ligados()
    if not lig:
        return
    if "*" in lig:
        empresas = [e["id"] for e in _get("oct_empresas?select=id")]
    else:
        empresas = sorted(lig)
    for emp in empresas:
        try:
            rodada_empresa(emp)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "credito_bloqueio_origem" in msg or "42703" in msg:
                _log("colunas do bloqueio ainda nao existem: rode repo/sql/SQL-BLOQUEIO-PRAZO-FATURA.sql")
                return
            _log(f"{emp}: erro: {msg[:200]}")


def iniciar():
    global _on
    if _on or not ligados():
        return
    _on = True

    def loop():
        time.sleep(90)
        while True:
            try:
                rodada()
            except Exception as e:  # noqa: BLE001
                _log(f"erro na rodada: {e}")
            time.sleep(CICLO_SEG)

    threading.Thread(target=loop, daemon=True).start()
    _log(f"ativo (a cada {CICLO_SEG // 60} min){' [SIMULACAO]' if _simular() else ''}")
