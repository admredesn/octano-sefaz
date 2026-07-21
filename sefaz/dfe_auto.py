"""
sefaz/dfe_auto.py  -  Consulta automatica de NF-e (Distribuicao DFe), ~1x/h por empresa.

Roda NO SERVIDOR SEFAZ (unico lugar com a CHAVE_MESTRA p/ decifrar a senha do cert).
Para cada empresa com certificado: pagina o DistDFe a partir do ultimo NSU, grava as
notas em oct_nfe_manifestadas (a MESMA tabela do botao manual) e respeita o limite da
SEFAZ: cStat 656 = "Consumo Indevido" -> pausa ~1h.

Controle em oct_dfe_controle (empresa_id, ultimo_nsu, ultima_consulta_em, bloqueado_ate).
O servidor roda com gunicorn --workers 2, entao o "claim" da consulta e ATOMICO no banco
(PATCH condicional): so 1 worker consulta cada empresa por intervalo -> evita 656.

Ativado por env DFE_AUTO=1. Ajustes: DFE_INTERVALO_MIN (60), DFE_CICLO_SEG (600),
DFE_BLOQUEIO_MIN (65), DFE_AMBIENTE (producao).
"""

import os
import json
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

from .empresa_cert import carregar_empresa, _rest_get, _supabase_conf
from .distdfe import consultar_distdfe

MIN_INTERVALO_MIN = int(os.environ.get("DFE_INTERVALO_MIN", "60"))
CICLO_SEG = int(os.environ.get("DFE_CICLO_SEG", "600"))
BLOQUEIO_656_MIN = int(os.environ.get("DFE_BLOQUEIO_MIN", "65"))
MAX_PAGINAS = 40


def _agora():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def _rest(method, path, body=None, prefer=None):
    url, key = _supabase_conf()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    if prefer:
        req.add_header("Prefer", prefer)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            t = r.read().decode("utf-8", "ignore")
            return r.status, (json.loads(t) if t.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:300]
    except Exception as e:
        return 0, str(e)


def _empresas_alvo():
    try:
        # obs: oct_empresas NAO tem coluna 'ambiente' -> o ambiente vem de DFE_AMBIENTE (default producao)
        rows = _rest_get("oct_empresas", "?cert_path=not.is.null&select=id,cnpj,ativo")
    except Exception as e:
        print("[dfe-auto] erro listando empresas:", e)
        return []
    return [r for r in rows if r.get("cnpj") and (r.get("ativo") in (None, True, 1))]


def _controle(emp_id):
    try:
        rows = _rest_get("oct_dfe_controle", f"?empresa_id=eq.{emp_id}&select=*")
        return rows[0] if rows else {}
    except Exception:
        return {}


def _garantir_controle(emp_id):
    _rest("POST", "oct_dfe_controle?on_conflict=empresa_id",
          body={"empresa_id": emp_id, "ultimo_nsu": "0", "atualizado_em": _iso(_agora())},
          prefer="resolution=ignore-duplicates,return=minimal")


def _claim(emp_id):
    """Marca ultima_consulta_em=agora SE 'due' (atomico entre os workers).
    Retorna True se ESTE worker ganhou o direito de consultar a empresa agora."""
    _garantir_controle(emp_id)
    agora = _agora()
    corte = agora - timedelta(minutes=MIN_INTERVALO_MIN)
    a = urllib.parse.quote(_iso(agora))
    c = urllib.parse.quote(_iso(corte))
    filtro = (f"?and=(empresa_id.eq.{emp_id},"
              f"or(ultima_consulta_em.is.null,ultima_consulta_em.lt.{c}),"
              f"or(bloqueado_ate.is.null,bloqueado_ate.lt.{a}))")
    st, rows = _rest("PATCH", f"oct_dfe_controle{filtro}",
                     body={"ultima_consulta_em": _iso(agora), "atualizado_em": _iso(agora)},
                     prefer="return=representation")
    return isinstance(rows, list) and len(rows) > 0


def _set_controle(emp_id, patch):
    patch = dict(patch)
    patch["atualizado_em"] = _iso(_agora())
    _rest("PATCH", f"oct_dfe_controle?empresa_id=eq.{emp_id}", body=patch, prefer="return=minimal")


def _salvar_nota(emp_id, n, ult_nsu_consulta):
    """Grava/atualiza uma nota em oct_nfe_manifestadas (mesma logica do botao manual):
    NF-e com chave -> completa a existente sem duplicar; evento/nova -> insere por nsu."""
    schema = n.get("schema", "") or ""
    eh_evento = any(x in schema for x in ("resEvento", "procEvento", "evento"))
    chave = n.get("chave") or None
    if chave and not eh_evento:
        try:
            ex = _rest_get("oct_nfe_manifestadas",
                           f"?empresa_id=eq.{emp_id}&chave_nfe=eq.{chave}"
                           f"&select=id,numero,xml,emissao,emitente,valor&limit=1")
        except Exception:
            ex = []
        if ex:
            row = ex[0]
            patch = {}
            if not row.get("numero") and n.get("numero"):
                patch["numero"] = n["numero"]
            if not row.get("xml") and n.get("xml"):
                patch["xml"] = n["xml"]
            if not row.get("emissao") and n.get("emissao"):
                patch["emissao"] = n["emissao"]
            if not row.get("emitente") and n.get("emitente"):
                patch["emitente"] = n["emitente"]
            if row.get("valor") is None and n.get("valor"):
                try:
                    patch["valor"] = float(n["valor"])
                except (TypeError, ValueError):
                    pass
            if patch:
                _rest("PATCH", f"oct_nfe_manifestadas?id=eq.{row['id']}", body=patch, prefer="return=minimal")
            return
    try:
        valor = float(n["valor"]) if n.get("valor") else None
    except (TypeError, ValueError):
        valor = None
    reg = {
        "empresa_id": emp_id, "nsu": n.get("nsu"), "schema": schema,
        "chave_nfe": chave, "numero": n.get("numero"), "serie": n.get("serie"),
        "emissao": n.get("emissao"), "emitente": n.get("emitente"), "emit_cnpj": n.get("emitCnpj"),
        "valor": valor, "nat_op": n.get("natOp"), "xml": n.get("xml"), "tipo": n.get("tipo", "resumo"),
        "status": "evento" if eh_evento else "sem_manifestacao",
        "ultimo_nsu_consulta": ult_nsu_consulta,
    }
    _rest("POST", "oct_nfe_manifestadas?on_conflict=empresa_id,nsu", body=reg,
          prefer="resolution=ignore-duplicates,return=minimal")


def _consultar_empresa(emp):
    emp_id = emp["id"]
    cnpj = str(emp.get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
    ambiente = (emp.get("ambiente") or os.environ.get("DFE_AMBIENTE", "producao")).strip().lower()
    try:
        dados = carregar_empresa(emp_id)
    except Exception as e:
        print(f"[dfe-auto] {emp_id}: cert nao carregou: {e}")
        return
    cert_b64, senha = dados["cert_base64"], dados["cert_senha"]
    ult_nsu = str(_controle(emp_id).get("ultimo_nsu") or "0")
    total = 0
    for _ in range(MAX_PAGINAS):
        try:
            resp = consultar_distdfe(cnpj, cert_b64, senha, ambiente, ult_nsu)
        except Exception as e:
            print(f"[dfe-auto] {emp_id}: erro distdfe: {e}")
            break
        if resp.get("erro"):
            print(f"[dfe-auto] {emp_id}: {resp['erro']}")
            break
        cstat = str(resp.get("cstat") or "")
        if cstat == "656":
            _set_controle(emp_id, {"ultimo_nsu": ult_nsu,
                                   "bloqueado_ate": _iso(_agora() + timedelta(minutes=BLOQUEIO_656_MIN))})
            print(f"[dfe-auto] {emp_id}: 656 consumo indevido -> pausa {BLOQUEIO_656_MIN}min")
            return
        for n in (resp.get("nfes") or []):
            _salvar_nota(emp_id, n, resp.get("ultimo_nsu"))
            total += 1
        novo = str(resp.get("ultimo_nsu") or ult_nsu)
        _set_controle(emp_id, {"ultimo_nsu": novo})
        if cstat == "137" or novo == ult_nsu:
            break
        try:
            if int(novo) >= int(str(resp.get("max_nsu") or novo)):
                break
        except (TypeError, ValueError):
            pass
        ult_nsu = novo
        time.sleep(1.5)
    if total:
        print(f"[dfe-auto] {emp_id}: {total} documento(s) processado(s)")


def _ciclo():
    for emp in _empresas_alvo():
        try:
            if _claim(emp["id"]):
                _consultar_empresa(emp)
        except Exception as e:
            print(f"[dfe-auto] ciclo erro {emp.get('id')}: {e}")


_on = False


def iniciar_agendador():
    global _on
    if _on:
        return
    if os.environ.get("DFE_AUTO", "").strip().lower() not in ("1", "true", "sim", "on"):
        print("[dfe-auto] desligado (defina DFE_AUTO=1 no Railway p/ ativar)")
        return
    _on = True

    def loop():
        time.sleep(40)  # espera o boot estabilizar
        while True:
            try:
                _ciclo()
            except Exception as e:
                print("[dfe-auto] loop:", e)
            time.sleep(CICLO_SEG)

    threading.Thread(target=loop, daemon=True).start()
    print(f"[dfe-auto] agendador ativo (ciclo {CICLO_SEG}s, intervalo {MIN_INTERVALO_MIN}min/empresa)")
