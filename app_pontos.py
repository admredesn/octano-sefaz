# -*- coding: utf-8 -*-
"""
app_pontos.py — pontos do app Postos SN (05/10/2026).

Regras do Ronan: 1 ponto por R$ 1 (combustível e loja), cada ponto vale 6 meses, só ganha
quem tem conta CONFIRMADA no app e foi identificado na venda (CPF na venda do PDV).

A cada 5 min:
  1. vendas do PDV dos últimos 3 dias (oct_pdv_vendas status 'concluida' com CPF de 11
     dígitos) cujo CPF tem conta confirmada no app → crédito (função
     oct_app_pontos_creditar; a ref 'venda:<id>' impede crédito em dobro, inclusive com os
     dois workers do gunicorn). Crédito novo = aviso no celular ("você ganhou X pontos").
  2. vendas canceladas dos últimos 30 dias → tira o que ainda resta daquele crédito.
  3. rotina do banco: vence pontos com mais de 6 meses e devolve resgates não retirados.

As vendas espelhadas do TecnoX (status 'tecnox') chegam sem CPF: não pontuam.
O que é conta (saldo, troca, vencimento) mora nas funções do banco (SQL-APP-PONTOS.sql),
que travam o CPF durante a operação.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import requests as rq

CICLO_SEG = 300
JANELA_DIAS = 3
_on = False
_nomes = {"ts": 0.0, "m": {}}


def _log(msg):
    print(f"[app-pontos] {msg}", flush=True)


def _supa():
    return (os.environ.get("SUPABASE_URL", "").strip().rstrip("/"),
            os.environ.get("SUPABASE_SERVICE_KEY", "").strip())


def _h(extra=None):
    _, key = _supa()
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def _sget_tudo(q, passo=1000):
    out, ini = [], 0
    url, _ = _supa()
    while True:
        r = rq.get(f"{url}/rest/v1/{q}", headers=_h({"Range-Unit": "items", "Range": f"{ini}-{ini + passo - 1}"}),
                   timeout=30)
        r.raise_for_status()
        lote = r.json()
        out.extend(lote)
        if len(lote) < passo:
            return out
        ini += passo


def rpc(nome, args):
    url, _ = _supa()
    r = rq.post(f"{url}/rest/v1/rpc/{nome}", headers=_h(), json=args, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"{nome}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def _so_digitos(s):
    return "".join(c for c in str(s or "") if c.isdigit())


def _nome_bonito(nome):
    """'POSTO SEVEN BH' -> 'Posto Seven BH' (igual ao cashback_portal._nome_bonito: é o texto
    que o cliente lê no extrato e no aviso do celular)."""
    nome = str(nome or "").strip()
    if not nome or not nome.isupper():
        return nome
    out = []
    for i, p in enumerate(nome.split()):
        if i and p.lower() in ("de", "da", "do", "das", "dos", "e"):
            out.append(p.lower())
        elif len(p) <= 2 and p.isalpha():
            out.append(p)
        else:
            out.append(p.capitalize())
    return " ".join(out)


def _nome_posto(eid):
    if time.time() - _nomes["ts"] > 3600:
        try:
            _nomes["m"] = {e["id"]: _nome_bonito(e.get("nome_fantasia") or e.get("nome") or "posto")
                           for e in _sget_tudo("oct_empresas?select=id,nome,nome_fantasia&order=id")}
            _nomes["ts"] = time.time()
        except Exception:
            pass
    return _nomes["m"].get(eid, "posto")


def _brl(v):
    return ("R$ %.2f" % float(v or 0)).replace(".", ",")


def _contas_confirmadas(cpfs):
    ok = set()
    lista = sorted(cpfs)
    for i in range(0, len(lista), 100):
        lote = ",".join(lista[i:i + 100])
        for c in _sget_tudo(f"oct_cashback_clientes?cpf=in.({lote})&verificado_em=not.is.null&select=cpf&order=cpf"):
            ok.add(c["cpf"])
    return ok


def rodada():
    agora = datetime.now(timezone.utc)
    desde = (agora - timedelta(days=JANELA_DIAS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    vendas = _sget_tudo(f"oct_pdv_vendas?status=eq.concluida&cliente_cpf=not.is.null&created_at=gte.{desde}"
                        "&select=id,empresa_id,cliente_cpf,valor_total,created_at,numero&order=created_at")
    vendas = [v for v in vendas if len(_so_digitos(v.get("cliente_cpf"))) == 11]
    contas = _contas_confirmadas({_so_digitos(v["cliente_cpf"]) for v in vendas}) if vendas else set()
    creditadas = 0
    for v in vendas:
        cpf = _so_digitos(v["cliente_cpf"])
        pts = int(float(v.get("valor_total") or 0))          # 1 ponto por R$ 1, sem arredondar para cima
        if cpf not in contas or pts <= 0:
            continue
        posto = _nome_posto(v.get("empresa_id"))
        try:
            novo = rpc("oct_app_pontos_creditar", {
                "p_cpf": cpf, "p_empresa": v.get("empresa_id"), "p_pontos": pts, "p_ref": f"venda:{v['id']}",
                "p_obs": f"compra de {_brl(v.get('valor_total'))} no {posto}" + (f" (cupom {v['numero']})" if v.get("numero") else ""),
                "p_quando": v.get("created_at")})
        except Exception as e:
            _log(f"venda {v['id']}: {e}")
            continue
        if novo is True:
            creditadas += 1
            try:
                import app_push
                saldo = rpc("oct_app_pontos_saldo", {"p_cpf": cpf})
                app_push.avisar_cliente(cpf, f"⭐ Você ganhou {pts} pontos",
                                        f"Compra de {_brl(v.get('valor_total'))} no {posto}. Seu saldo: {saldo} pontos.",
                                        {"tela": "pontos"})
            except Exception:
                pass
    desde30 = (agora - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    canceladas = 0
    for v in _sget_tudo(f"oct_pdv_vendas?status=eq.cancelada&cliente_cpf=not.is.null&created_at=gte.{desde30}"
                        "&select=id&order=id"):
        try:
            canceladas += 1 if rpc("oct_app_pontos_cancelar", {"p_ref": f"venda:{v['id']}"}) else 0
        except Exception:
            pass
    rot = rpc("oct_app_pontos_rotina", {})
    if creditadas or canceladas or (rot or {}).get("pontos_vencidos") or (rot or {}).get("resgates_expirados"):
        _log(f"{creditadas} venda(s) creditada(s), {canceladas} estorno(s) de cancelamento, rotina {rot}")
    return creditadas


def iniciar():
    global _on
    if _on or os.environ.get("APP_PONTOS", "1").strip().lower() in ("0", "false", "nao", "off"):
        return
    _on = True

    def loop():
        time.sleep(60)
        while True:
            try:
                rodada()
            except Exception as e:
                _log(f"erro na rodada: {e}")
            time.sleep(CICLO_SEG)

    threading.Thread(target=loop, daemon=True).start()
    _log(f"crédito de pontos ativo (a cada {CICLO_SEG // 60} min)")
