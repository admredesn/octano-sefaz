# -*- coding: utf-8 -*-
"""
sefaz/conciliador_pagar.py  -  Baixa AUTOMATICA de contas a pagar (compra de
combustivel) so' quando as TRES pontas coincidem (regra do Ronan, 2026-09-23):

  1) NOTA FISCAL  -> o titulo nasce da NF-e de entrada (n_documento = numero da NF).
  2) DESCARGA     -> oct_nfe_descarga status='entrada_feita' pra aquela NF
                     (a sonda/medidor confirmou o volume que entrou no tanque).
  3) PAGAMENTO    -> debito em oct_banco_movimentos que casa por VALOR + janela
                     de data, com PAR UNICO (um debito <-> um titulo).

So' baixa com as 3 ✓ e par inequivoco. Encargo (juros/tarifa/desconto) igual a'
tela (CB_TARIFA_BOLETO=3,72; tarifa se ~3,72 ou pago em dia; senao juros).

TRAVA: env CONCILIAR_PAGAR = lista de empresa_id habilitados (ou 'all', ou
vazio=DESLIGADO) -- mesmo padrao do ENTRADA_AUTO. Liga primeiro num posto de
teste antes de soltar em todos. Idempotente: so' toca titulo 'aberto' e debito
com conta_pagar_id nulo; a baixa em si e' condicionada (status=eq.aberto no PATCH).

Roda dentro do ciclo do dfe_auto (a cada CICLO_SEG), depois de descarga/entrada,
sobre a janela dos ultimos DIAS_JANELA dias.
"""

import os
import re
import json
import datetime as dt
import urllib.request
import urllib.error

from .empresa_cert import _rest_get, _supabase_conf

TARIFA = 3.72
JUROS_DIA = 0.002167   # ALE ~0,2167%/dia
DIAS_JANELA = 45       # olha os debitos dos ultimos N dias


def _habilitado(emp_id):
    v = os.environ.get("CONCILIAR_PAGAR", "").strip().lower()
    if not v:
        return False
    if v in ("all", "todos", "1", "true"):
        return True
    return str(emp_id).lower() in [x.strip() for x in v.split(",")]


def _rest(method, path, body=None, prefer="return=minimal"):
    url, key = _supabase_conf()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            t = r.read().decode("utf-8", "ignore")
            return r.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:300]


def _q(tabela, params):
    """GET paginado (limit/offset) -- as tabelas por empresa cabem, mas as
    descargas acumulam, entao pagina por seguranca."""
    out, off = [], 0
    while True:
        sep = "&" if params else "?"
        rows = _rest_get(tabela, f"{params}{sep}limit=1000&offset={off}")
        if not rows:
            break
        out += rows
        if len(rows) < 1000:
            break
        off += 1000
    return out


def _nf_num(n_doc):
    m = re.match(r"\s*(\d+)", str(n_doc or ""))
    return m.group(1) if m else None


def _classifica(dif, venc, pgto):
    """-> (juros, tarifa, desconto) igual a' tela."""
    if dif <= 0.004:
        return 0.0, 0.0, (-dif if dif < -0.004 else 0.0)
    d = round(dif, 2)
    if abs(d - TARIFA) < 0.015:
        return 0.0, d, 0.0
    try:
        v = dt.date.fromisoformat(str(venc)[:10])
        p = dt.date.fromisoformat(str(pgto)[:10])
        if p <= v:
            return 0.0, d, 0.0
    except Exception:
        pass
    return d, 0.0, 0.0


def _casa(t, d):
    val = round(float(t["valor"] or 0), 2)
    try:
        venc = dt.date.fromisoformat(str(t["vencimento"])[:10])
        dd = dt.date.fromisoformat(d["data"])
    except Exception:
        return False
    if dd < venc - dt.timedelta(days=5) or dd > venc + dt.timedelta(days=25):
        return False
    dv = round(d["v"] - val, 2)
    if abs(dv) <= 0.02:
        return True                       # valor EXATO (pago em dia)
    if abs(dv - TARIFA) <= 0.02:
        return True                       # + tarifa do boleto
    if dv > 0:                            # pago com JUROS: amarra aos dias de atraso
        dias = (dd - venc).days
        if dias > 0:
            for extra in (0.0, TARIFA):
                esp = val * JUROS_DIA * dias + extra
                if abs(dv - esp) <= max(0.15, val * 0.0006):
                    return True
    return False


def processar_empresa(emp_id):
    if not _habilitado(emp_id):
        return
    hoje = dt.date.today()
    de = (hoje - dt.timedelta(days=DIAS_JANELA)).isoformat()
    ate = hoje.isoformat()

    # 1 (NOTA): titulos ABERTOS que vieram de NF (n_documento tem numero).
    tit = _q("oct_contas_pagar",
             f"?empresa_id=eq.{emp_id}&status=eq.aberto"
             "&select=id,n_documento,valor,vencimento,descricao")
    tit = [t for t in tit if _nf_num(t.get("n_documento"))]
    if not tit:
        return
    # 2 (DESCARGA): numeros de NF com entrada confirmada.
    desc = _q("oct_nfe_descarga",
              f"?empresa_id=eq.{emp_id}&status=eq.entrada_feita&select=nf_numeros")
    conf = set()
    for d in desc:
        for n in re.findall(r"\d+", str(d.get("nf_numeros") or "")):
            conf.add(n)
    if not conf:
        return
    # 3 (PAGAMENTO): debitos sem vinculo na janela.
    mv = _q("oct_banco_movimentos",
            f"?empresa_id=eq.{emp_id}&tipo=eq.debito&conta_pagar_id=is.null"
            f"&data=gte.{de}&data=lte.{ate}&select=id,data,valor,descricao")
    debs = [{"id": m["id"], "data": str(m["data"])[:10],
             "v": round(float(m["valor"] or 0), 2)} for m in mv]
    if not debs:
        return

    elegiveis = [t for t in tit if _nf_num(t["n_documento"]) in conf]   # 1+2 ok
    # matches mutuos (par tem de ser UNICO dos dois lados)
    m_t = {t["id"]: [d for d in debs if _casa(t, d)] for t in elegiveis}
    m_d = {}
    for t in elegiveis:
        for d in m_t[t["id"]]:
            m_d.setdefault(d["id"], []).append(t["id"])

    baixados = 0
    for t in elegiveis:
        cand = m_t[t["id"]]
        if len(cand) != 1:
            continue
        d = cand[0]
        if len(m_d[d["id"]]) != 1:        # o debito casa mais de um titulo -> ambiguo
            continue
        val = round(float(t["valor"] or 0), 2)
        dif = round(d["v"] - val, 2)
        juros, tarifa, desc_v = _classifica(dif, t["vencimento"], d["data"])
        obs = "conciliador 3-pontas (nota+descarga+pagamento) mov %s" % d["id"]
        st, _ = _rest("PATCH", f"oct_contas_pagar?id=eq.{t['id']}&status=eq.aberto",
                      {"status": "pago", "data_pagamento": d["data"], "valor_pago": d["v"],
                       "juros": juros, "tarifa": tarifa, "desconto": desc_v,
                       "forma_pagamento": "Sicoob", "observacoes": obs})
        if st in (200, 204):
            _rest("PATCH", f"oct_banco_movimentos?id=eq.{d['id']}",
                  {"conciliado": True, "conta_pagar_id": t["id"],
                   "dif_encargos": (dif if dif else None)})
            baixados += 1
            print(f"[conciliar-pagar] {emp_id}: NF {_nf_num(t['n_documento'])} "
                  f"R$ {val:.2f} <= debito {d['data']} R$ {d['v']:.2f} (mov {d['id']})")
    if baixados:
        print(f"[conciliar-pagar] {emp_id}: {baixados} titulo(s) baixado(s)")
