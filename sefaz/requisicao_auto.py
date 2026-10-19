# -*- coding: utf-8 -*-
"""
sefaz/requisicao_auto.py  -  REQUISICAO DE COMPRA, fase 2 (13/10/2026, Ronan).

A requisicao (retaguarda, fase 1) registra o que foi comprado e so' libera o titulo quando a
mercadoria chega ao posto. Aqui o servidor faz sozinho o que o operador faria a' mao:

  A) VINCULA a NF-e a' requisicao: fornecedor pelo CNPJ da CHAVE (posicoes 7-20) + (mesmo
     documento OU mesmo valor, +-0,02), com PAR UNICO dos dois lados. Liga tambem os titulos
     da nota (n_documento = numero da NF) a' requisicao.
  B) COMBUSTIVEL: a descarga confirmada pela sonda (oct_nfe_descarga.status='entrada_feita'
     para a NF da requisicao) = a mercadoria CHEGOU -> requisicao 'recebida' (ou 'paga', se ja'
     estava paga). Se os litros da nota diferem do pedido (>1%), vira 'divergente' e espera
     um humano.
  C) (em conciliador_pagar.py) a baixa automatica NAO paga titulo de requisicao pendente/
     divergente e, depois de baixar, atualiza a requisicao (registrar_baixa).

TRAVA: env REQUISICAO_AUTO = lista de empresa_id (ou 'all'; vazio = DESLIGADO) — mesmo padrao do
ENTRADA_AUTO / CONCILIAR_PAGAR. REQUISICAO_AUTO_SIMULAR=1: so' registra no log o que faria.
Idempotente: cada gravacao e' condicionada ao estado atual (status / nfe_chave nulo).
"""

import os
import re
import json
import datetime as dt
import urllib.request
import urllib.error

from .empresa_cert import _rest_get, _supabase_conf

PENDENTES = ("pedida", "parcial", "divergente")
DIAS_NF = 60            # olha notas emitidas nos ultimos N dias
TOL_VALOR = 0.02
# COMBUSTIVEL (19/10/2026): o pedido e' estimado e a nota vem com o preco do dia — no Florestal a
# REQ-000006 (25.730,00) virou a NF 381401 (26.022,57, +1,1%). Com a tolerancia de centavos nada
# ligava. Para combustivel vale valor ate' 3% de diferenca SE a nota sair perto da chegada prevista.
TOL_VALOR_COMB = 0.03
DIAS_COMB = 3
TOL_LITROS_PERC = 0.01  # 1% do pedido
TOL_LITROS_MIN = 10.0   # ... ou 10 L
UNIDADES_LITRO = ("L", "LT", "LTS", "LITRO", "LITROS")


def _habilitado(emp_id):
    v = os.environ.get("REQUISICAO_AUTO", "").strip().lower()
    if not v:
        return False
    if v in ("all", "todos", "1", "true"):
        return True
    return str(emp_id).lower() in [x.strip() for x in v.split(",")]


def _simular():
    return os.environ.get("REQUISICAO_AUTO_SIMULAR", "").strip().lower() in ("1", "true", "sim", "on")


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


def _lote(ids, n=60):
    ids = [str(i) for i in ids if i]
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


# ------------------------------------------------------------------
# regras puras (testadas sem rede)
# ------------------------------------------------------------------
def so_digitos(s):
    return re.sub(r"\D", "", str(s or ""))


def cnpj_da_chave(chave):
    """A chave da NF-e (44 digitos) traz o CNPJ do emitente nas posicoes 7-20."""
    c = so_digitos(chave)
    return c[6:20] if len(c) == 44 else ""


def numero_nf(n_documento):
    m = re.match(r"\s*(\d+)", str(n_documento or ""))
    return str(int(m.group(1))) if m else None


def candidatas_da_requisicao(req, doc_forn, nfs, usadas):
    """NF-es que podem ser desta requisicao. nfs: [{chave_nfe, numero, emissao, valor}]; usadas: chaves ja' ligadas."""
    out = []
    forn = so_digitos(doc_forn)
    if not forn:
        return out
    doc = so_digitos(req.get("documento"))
    doc_n = str(int(doc)) if doc and len(doc) <= 9 else None
    v = round(float(req.get("valor_total") or 0), 2)
    criada = str(req.get("criado_em") or "")[:10]
    comb = req.get("categoria") == "combustivel"
    ref = str(req.get("previsao_chegada") or "")[:10] or criada     # dia em que a nota deveria sair
    for n in nfs:
        ch = so_digitos(n.get("chave_nfe"))
        if len(ch) != 44 or ch in usadas or cnpj_da_chave(ch) != forn:
            continue
        mesmo_doc = bool(doc_n) and numero_nf(n.get("numero")) == doc_n
        vn = round(float(n["valor"]), 2) if n.get("valor") is not None else None
        mesmo_valor = vn is not None and abs(vn - v) <= TOL_VALOR
        em = str(n.get("emissao") or "")[:10]
        dist = None
        try:
            dist = abs((dt.date.fromisoformat(em) - dt.date.fromisoformat(ref)).days) if em and ref else None
        except ValueError:
            pass
        valor_prox = (comb and vn is not None and v > 0 and abs(vn / v - 1) <= TOL_VALOR_COMB
                      and dist is not None and dist <= DIAS_COMB)
        if not (mesmo_doc or mesmo_valor or valor_prox):
            continue
        # a nota so' pode ser de DEPOIS do pedido (3 dias de folga: pedido lancado depois de a nota sair)
        if criada and em:
            try:
                if dt.date.fromisoformat(em) < dt.date.fromisoformat(criada) - dt.timedelta(days=3):
                    continue
            except ValueError:
                pass
        out.append({"nf": n, "mesmo_doc": mesmo_doc, "mesmo_valor": mesmo_valor, "valor_prox": valor_prox,
                    "dist": dist if dist is not None else 99, "dv": abs((vn or 0) - v)})
    return out


def _ordem(c):
    """quem e' o par mais provavel: mesmo documento > nota mais perto da chegada > valor mais perto"""
    return (0 if c["mesmo_doc"] else 1, c.get("dist", 99), round(c.get("dv", 0), 2))


def pares_unicos(requisicoes, nfs, forn_doc, usadas):
    """{requisicao_id: candidata} so' quando o par e' INEQUIVOCO: a NF e' a MELHOR candidata da requisicao
    E a requisicao e' a MELHOR candidata da NF, sem empate dos dois lados (ordem: mesmo documento, nota
    mais perto da chegada prevista, valor mais perto). Empate = ninguem e' ligado (fica para o operador).
    19/10/2026: antes so' o documento desempatava; duas cargas de mesmo valor pedidas em dias seguidos
    (REQ-000006/000007 do Florestal) ficavam sem ligar."""
    cand = {r["id"]: candidatas_da_requisicao(r, forn_doc.get(r.get("fornecedor_id")), nfs, usadas) for r in requisicoes}
    melhor = {}
    for rid, cs in cand.items():
        o = sorted(cs, key=_ordem)
        if o and (len(o) == 1 or _ordem(o[0]) < _ordem(o[1])):
            melhor[rid] = o[0]
    por_nf = {}
    for rid, cs in cand.items():
        for c in cs:
            por_nf.setdefault(c["nf"]["chave_nfe"], []).append((rid, c))
    pares = {}
    for rid, c in melhor.items():
        rivais = sorted(por_nf[c["nf"]["chave_nfe"]], key=lambda x: _ordem(x[1]))
        if rivais[0][0] == rid and (len(rivais) == 1 or _ordem(rivais[0][1]) < _ordem(rivais[1][1])):
            pares[rid] = c
    return pares


def litros_do_pedido(itens):
    t = 0.0
    for i in itens or []:
        if str(i.get("unidade") or "").strip().upper() in UNIDADES_LITRO:
            t += float(i.get("qtd_pedida") or 0)
    return t


def decidir_chegada(req, itens, litros_nf):
    """-> ('recebida'|'paga'|'divergente', detalhe). Chamada so' quando a descarga da NF esta' confirmada."""
    ped = litros_do_pedido(itens)
    if ped > 0 and litros_nf is not None and litros_nf > 0:
        dif = abs(litros_nf - ped)
        if dif > max(ped * TOL_LITROS_PERC, TOL_LITROS_MIN):
            return "divergente", ("descarga confirmada pela sonda, mas a nota traz %.0f L e o pedido era de %.0f L (diferenca %.0f L)"
                                  % (litros_nf, ped, dif))
    return ("paga" if req.get("pago_em") else "recebida"), "descarga confirmada pela sonda"


def segurados_pelo_conciliador(titulos, status_por_requisicao):
    """Ids dos titulos que o conciliador NAO deve baixar: requisicao pendente ou divergente."""
    return {t["id"] for t in titulos if t.get("requisicao_id") and status_por_requisicao.get(t["requisicao_id"]) in PENDENTES}


# ------------------------------------------------------------------
# E/S
# ------------------------------------------------------------------
def _evento(emp_id, req_id, tipo, detalhe, simular):
    if simular:
        print(f"[requisicao-auto] (simulacao) evento {tipo} req {req_id}: {detalhe}")
        return
    _rest("POST", "oct_requisicoes_compra_eventos",
          {"requisicao_id": req_id, "empresa_id": emp_id, "tipo": tipo, "detalhe": detalhe, "por_nome": "Automático (servidor)"})


def processar_empresa(emp_id):
    if not _habilitado(emp_id):
        return
    simular = _simular()
    hoje = dt.date.today()
    abertas = _q("oct_requisicoes_compra",
                 f"?empresa_id=eq.{emp_id}&status=in.({','.join(PENDENTES)},recebida)"
                 "&select=id,numero,fornecedor_id,valor_total,documento,categoria,status,nfe_chave,pago_em,criado_em,previsao_chegada")
    if not abertas:
        return
    # fornecedores (CNPJ) das requisicoes abertas
    fids = sorted({r["fornecedor_id"] for r in abertas if r.get("fornecedor_id")})
    forn_doc = {}
    for lote in _lote(fids):
        for p in _rest_get("oct_pessoas", f"?id=in.({','.join(lote)})&select=id,documento") or []:
            forn_doc[p["id"]] = p.get("documento")

    # ---- A) vincula NF-e <-> requisicao ----
    sem_nf = [r for r in abertas if not r.get("nfe_chave")]
    if sem_nf:
        de = (hoje - dt.timedelta(days=DIAS_NF)).isoformat()
        nfs = _q("oct_nfe_manifestadas",
                 f"?empresa_id=eq.{emp_id}&chave_nfe=not.is.null&emissao=gte.{de}&select=chave_nfe,numero,emissao,valor")
        usadas = {x["nfe_chave"] for x in _q("oct_requisicoes_compra", f"?empresa_id=eq.{emp_id}&nfe_chave=not.is.null&select=nfe_chave")}
        for rid, c in pares_unicos(sem_nf, nfs, forn_doc, usadas).items():
            r = next(x for x in sem_nf if x["id"] == rid)
            n = c["nf"]
            motivo = ("mesmo documento" if c["mesmo_doc"] else "") + (" e " if c["mesmo_doc"] and c["mesmo_valor"] else "") + ("mesmo valor" if c["mesmo_valor"] else "")
            if not motivo and c.get("valor_prox"):
                motivo = "combustivel: valor ate' 3%% e nota %d dia(s) da chegada prevista" % c["dist"]
            dif = ""
            if n.get("valor") is not None and abs(round(float(n["valor"]), 2) - round(float(r["valor_total"]), 2)) > TOL_VALOR:
                dif = " | ATENCAO: nota R$ %.2f, pedido R$ %.2f" % (float(n["valor"]), float(r["valor_total"]))
            print(f"[requisicao-auto] {emp_id}: REQ {r['numero']} <- NF {n.get('numero')} ({motivo})"
                  + (" [simulacao]" if simular else ""))
            if not simular:
                st, _ = _rest("PATCH", f"oct_requisicoes_compra?id=eq.{rid}&nfe_chave=is.null",
                              {"nfe_chave": n["chave_nfe"], "atualizado_em": dt.datetime.utcnow().isoformat() + "Z"})
                if st not in (200, 204):
                    continue
            _evento(emp_id, rid, "nfe_vinculada", f"NF {n.get('numero')} ({motivo}){dif}", simular)
            r["nfe_chave"] = n["chave_nfe"]

    # ---- A2) titulos da NF -> requisicao ----
    com_nf = [r for r in abertas if r.get("nfe_chave")]
    num_da_chave = {}
    for lote in _lote([r["nfe_chave"] for r in com_nf]):
        for n in _rest_get("oct_nfe_manifestadas", f"?empresa_id=eq.{emp_id}&chave_nfe=in.({','.join(lote)})&select=chave_nfe,numero") or []:
            num_da_chave[n["chave_nfe"]] = numero_nf(n.get("numero"))
    for r in com_nf:
        num = num_da_chave.get(r["nfe_chave"])
        if not num:
            continue
        tits = _rest_get("oct_contas_pagar",
                         f"?empresa_id=eq.{emp_id}&requisicao_id=is.null&status=neq.cancelado&n_documento=like.{num}*"
                         "&select=id,n_documento,fornecedor_id,status") or []
        alvo = [t["id"] for t in tits if numero_nf(t.get("n_documento")) == num
                and (not t.get("fornecedor_id") or t.get("fornecedor_id") == r.get("fornecedor_id"))]
        if not alvo:
            continue
        print(f"[requisicao-auto] {emp_id}: REQ {r['numero']}: {len(alvo)} titulo(s) da NF {num} ligado(s)" + (" [simulacao]" if simular else ""))
        if not simular:
            for tid in alvo:
                _rest("PATCH", f"oct_contas_pagar?id=eq.{tid}&requisicao_id=is.null", {"requisicao_id": r["id"]})
        _evento(emp_id, r["id"], "titulo_vinculado", f"{len(alvo)} titulo(s) da NF {num} ligado(s) automaticamente", simular)

    # ---- B) combustivel: descarga confirmada = chegou ----
    pend = [r for r in com_nf if r.get("categoria") == "combustivel" and r.get("status") in PENDENTES]
    if not pend:
        return
    desc = _q("oct_nfe_descarga", f"?empresa_id=eq.{emp_id}&status=eq.entrada_feita&select=nf_chaves")
    feitas = {c for d in desc for c in str(d.get("nf_chaves") or "").split(",") if c}
    for r in pend:
        if r["nfe_chave"] not in feitas:
            continue
        itens = _rest_get("oct_requisicoes_compra_itens", f"?requisicao_id=eq.{r['id']}&select=id,unidade,qtd_pedida,qtd_recebida") or []
        litros_nf = None
        ent = _rest_get("oct_nfe_entrada", f"?empresa_id=eq.{emp_id}&chave_nfe=eq.{r['nfe_chave']}&select=id&limit=1") or []
        if ent:
            its = _rest_get("oct_nfe_entrada_itens", f"?nfe_id=eq.{ent[0]['id']}&select=quantidade,unidade") or []
            litros_nf = sum(float(i.get("quantidade") or 0) for i in its
                            if str(i.get("unidade") or "").strip().upper() in UNIDADES_LITRO) or None
        status, detalhe = decidir_chegada(r, itens, litros_nf)
        print(f"[requisicao-auto] {emp_id}: REQ {r['numero']} -> {status} ({detalhe})" + (" [simulacao]" if simular else ""))
        if simular:
            _evento(emp_id, r["id"], "recebida" if status != "divergente" else "divergente", detalhe, True)
            continue
        agora = dt.datetime.utcnow().isoformat() + "Z"
        if status != "divergente":
            for i in itens:
                _rest("PATCH", f"oct_requisicoes_compra_itens?id=eq.{i['id']}", {"qtd_recebida": i.get("qtd_pedida")})
        st, _ = _rest("PATCH", f"oct_requisicoes_compra?id=eq.{r['id']}&status=in.({','.join(PENDENTES)})",
                      {"status": status, "recebido_em": agora, "recebido_por_nome": "Descarga automática (sonda)",
                       "recebido_obs": detalhe, "atualizado_em": agora})
        if st in (200, 204):
            _evento(emp_id, r["id"], "divergente" if status == "divergente" else "recebida", detalhe, False)


def registrar_baixa(emp_id, titulo_id, requisicao_id, valor_pago, origem):
    """Chamado pelo conciliador depois de baixar um titulo ligado a requisicao: historico + estado dela."""
    if not requisicao_id or not _habilitado(emp_id):
        return
    try:
        rq = _rest_get("oct_requisicoes_compra", f"?id=eq.{requisicao_id}&select=id,status,pago_em,numero&limit=1") or []
        if not rq:
            return
        req = rq[0]
        tits = _rest_get("oct_contas_pagar", f"?requisicao_id=eq.{requisicao_id}&select=id,status") or []
        todos = bool(tits) and all(t.get("status") == "pago" or t.get("id") == titulo_id for t in tits)
        upd = {"atualizado_em": dt.datetime.utcnow().isoformat() + "Z"}
        pendente = req.get("status") in PENDENTES
        if todos:
            upd["pago_em"] = dt.datetime.utcnow().isoformat() + "Z"
            if pendente:
                upd["pago_antes_do_recebimento"] = True
            elif req.get("status") == "recebida":
                upd["status"] = "paga"
        _rest("PATCH", f"oct_requisicoes_compra?id=eq.{requisicao_id}", upd)
        _evento(emp_id, requisicao_id, "pago_antes" if pendente else "pago",
                f"R$ {float(valor_pago or 0):.2f} — {origem}", False)
    except Exception as e:
        print(f"[requisicao-auto] registrar_baixa: {e}")
