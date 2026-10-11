"""
sefaz/cte_auto.py  -  Busca automatica dos CT-e de cada posto (Distribuicao DF-e do CT-e).

Pedido do Ronan (11/10/2026): saber o custo do frete, conferir as transportadoras
e ter o CT-e guardado para compor o SPED (registros D100/D190).

Roda dentro do mesmo ciclo do dfe_auto (NF-e), uma empresa por vez:
  - controle em oct_cte_controle (ultimo_nsu, ultima_consulta_em, bloqueado_ate):
    o "claim" e' um PATCH condicional -> so' 1 dos 2 workers do gunicorn consulta
    cada empresa por intervalo (a SEFAZ devolve 656 "consumo indevido" a quem
    insiste, e bloqueia por 1 h);
  - cada documento vira uma linha de oct_cte_recebidos: o CT-e com os campos ja'
    separados (transportadora, tomador, valores, ICMS, municipios, chaves das
    NF-e transportadas) + o XML inteiro; os eventos entram como tipo='evento' e
    o cancelamento (110111) marca o CT-e como cancelado.

Ligado junto com o DFE_AUTO. Desliga so' o CT-e com CTE_AUTO=0. Ajustes:
CTE_INTERVALO_MIN (120), CTE_BLOQUEIO_MIN (65), CTE_AMBIENTE (o do DFE_AMBIENTE).
Sem as tabelas (SQL-CTE-RECEBIDOS.sql ainda nao rodado) o robo so' avisa e segue.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .empresa_cert import _rest_get, _supabase_conf, carregar_empresa
from .cte_distdfe import consultar_cte

MIN_INTERVALO_MIN = int(os.environ.get("CTE_INTERVALO_MIN", "120"))
BLOQUEIO_656_MIN = int(os.environ.get("CTE_BLOQUEIO_MIN", "65"))
MAX_PAGINAS = 40
_SEM_TABELA = {"ate": 0.0}      # tabelas ausentes: nao tenta de novo por 30 min


def ligado():
    return os.environ.get("CTE_AUTO", "1").strip().lower() not in ("0", "false", "nao", "não", "off")


def _agora():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def _so_digitos(s):
    return "".join(ch for ch in str(s or "") if ch.isdigit())


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


def _tabelas_existem():
    """False (e um aviso) enquanto o SQL-CTE-RECEBIDOS.sql nao foi rodado."""
    if time.time() < _SEM_TABELA["ate"]:
        return False
    st, r = _rest("GET", "oct_cte_controle?select=empresa_id&limit=1")
    if st == 200:
        return True
    _SEM_TABELA["ate"] = time.time() + 1800
    print(f"[cte-auto] tabelas do CT-e ainda nao existem (rode o SQL-CTE-RECEBIDOS.sql): HTTP {st} {str(r)[:120]}")
    return False


def _controle(emp_id):
    try:
        rows = _rest_get("oct_cte_controle", f"?empresa_id=eq.{emp_id}&select=*")
        return rows[0] if rows else {}
    except Exception:
        return {}


def _set_controle(emp_id, patch):
    patch = dict(patch)
    patch["atualizado_em"] = _iso(_agora())
    _rest("PATCH", f"oct_cte_controle?empresa_id=eq.{emp_id}", body=patch, prefer="return=minimal")


def _claim(emp_id):
    """True se ESTE worker ganhou o direito de consultar a empresa agora (atomico no banco)."""
    _rest("POST", "oct_cte_controle?on_conflict=empresa_id",
          body={"empresa_id": emp_id, "ultimo_nsu": "0", "atualizado_em": _iso(_agora())},
          prefer="resolution=ignore-duplicates,return=minimal")
    agora = _agora()
    corte = agora - timedelta(minutes=MIN_INTERVALO_MIN)
    a, c = urllib.parse.quote(_iso(agora)), urllib.parse.quote(_iso(corte))
    filtro = (f"?and=(empresa_id.eq.{emp_id},"
              f"or(ultima_consulta_em.is.null,ultima_consulta_em.lt.{c}),"
              f"or(bloqueado_ate.is.null,bloqueado_ate.lt.{a}))")
    st, rows = _rest("PATCH", f"oct_cte_controle{filtro}",
                     body={"ultima_consulta_em": _iso(agora), "atualizado_em": _iso(agora)},
                     prefer="return=representation")
    return isinstance(rows, list) and len(rows) > 0


def papel_do_posto(cnpj, d):
    """Como o posto aparece no CT-e: tomador (paga o frete), destinatario, remetente...
    Compara o CNPJ inteiro; sem achar, a raiz (8 digitos) cobre matriz/filial."""
    cnpj = _so_digitos(cnpj)
    partes = (("tomador", d.get("tomador_cnpj")), ("destinatario", d.get("dest_cnpj")), ("remetente", d.get("rem_cnpj")),
              ("recebedor", d.get("receb_cnpj")), ("expedidor", d.get("exped_cnpj")), ("emitente", d.get("emit_cnpj")))
    for nome, doc in partes:
        if cnpj and _so_digitos(doc) == cnpj:
            return nome
    for nome, doc in partes:
        if len(cnpj) == 14 and len(_so_digitos(doc)) == 14 and _so_digitos(doc)[:8] == cnpj[:8]:
            return nome
    return "outro"


def litros_da_carga(qtds):
    """Litros informados no CT-e (cUnid 04 = litros; 00 = m3). None se a transportadora nao informou volume."""
    tot = 0.0
    for q in qtds or []:
        v = q.get("qtd")
        if v is None:
            continue
        if q.get("unid") == "04":
            tot += float(v)
        elif q.get("unid") == "00":
            tot += float(v) * 1000.0
    return round(tot, 3) if tot > 0 else None


def linha_do_cte(emp_id, cnpj, d, ult_nsu_consulta=None):
    """dict de extrair_cte() -> linha de oct_cte_recebidos. Pura (testavel)."""
    papel = papel_do_posto(cnpj, d)
    cstat = d.get("prot_cstat")
    return {
        "empresa_id": emp_id, "nsu": d.get("nsu"), "schema": d.get("schema"), "tipo": "cte",
        "chave_cte": d.get("chave") or None,
        "situacao": "autorizado" if cstat in (None, "", "100") else f"cStat {cstat}",
        "modelo": d.get("modelo") or None, "serie": d.get("serie") or None, "numero": d.get("numero") or None,
        "emissao": d.get("emissao"), "tp_cte": d.get("tp_cte") or None, "tp_serv": d.get("tp_serv") or None,
        "modal": d.get("modal") or None, "cfop": d.get("cfop") or None, "nat_op": d.get("nat_op") or None,
        "emit_cnpj": d.get("emit_cnpj") or None, "emit_nome": d.get("emit_nome") or None,
        "emit_ie": d.get("emit_ie") or None, "emit_uf": d.get("emit_uf") or None, "emit_mun": d.get("emit_mun") or None,
        "tomador_tipo": d.get("tomador_tipo") or None, "tomador_cnpj": d.get("tomador_cnpj") or None,
        "tomador_nome": d.get("tomador_nome") or None,
        "rem_cnpj": d.get("rem_cnpj") or None, "rem_nome": d.get("rem_nome") or None,
        "dest_cnpj": d.get("dest_cnpj") or None, "dest_nome": d.get("dest_nome") or None,
        "exped_cnpj": d.get("exped_cnpj") or None, "receb_cnpj": d.get("receb_cnpj") or None,
        "papel": papel, "sou_tomador": papel == "tomador",
        "mun_ini": d.get("mun_ini") or None, "mun_ini_nome": d.get("mun_ini_nome") or None, "uf_ini": d.get("uf_ini") or None,
        "mun_fim": d.get("mun_fim") or None, "mun_fim_nome": d.get("mun_fim_nome") or None, "uf_fim": d.get("uf_fim") or None,
        "valor_prestacao": d.get("valor_prestacao"), "valor_receber": d.get("valor_receber"),
        "componentes": d.get("componentes") or [],
        "icms_grupo": d.get("icms_grupo") or None, "icms_cst": d.get("icms_cst") or None, "icms_bc": d.get("icms_bc"),
        "icms_aliq": d.get("icms_aliq"), "icms_valor": d.get("icms_valor"), "icms_red_bc": d.get("icms_red_bc"),
        "valor_carga": d.get("valor_carga"), "produto_pred": d.get("produto_pred") or None,
        "qtd_carga": d.get("qtd_carga") or [], "litros_carga": litros_da_carga(d.get("qtd_carga")),
        "chaves_nfe": d.get("chaves_nfe") or [], "chave_cte_ref": d.get("chave_cte_ref"),
        "rntrc": d.get("rntrc"), "obs": d.get("obs"), "placa": d.get("placa"), "motorista": d.get("motorista"),
        "protocolo": d.get("protocolo"), "autorizado_em": d.get("autorizado_em"),
        "xml": d.get("xml"), "ultimo_nsu_consulta": ult_nsu_consulta,
    }


def linha_do_evento(emp_id, d, ult_nsu_consulta=None):
    return {
        "empresa_id": emp_id, "nsu": d.get("nsu"), "schema": d.get("schema"), "tipo": "evento",
        "chave_cte": d.get("chave") or None, "situacao": "evento",
        "emit_cnpj": d.get("emit_cnpj") or None,
        "evento_tipo": d.get("evento_tipo") or None, "evento_descr": d.get("evento_descr") or None,
        "evento_seq": d.get("evento_seq"), "evento_em": d.get("evento_em"),
        "obs": d.get("evento_just"),
        "xml": d.get("xml"), "ultimo_nsu_consulta": ult_nsu_consulta,
    }


def _salvar(emp_id, cnpj, d, ult_nsu_consulta):
    """Grava um documento do lote. Devolve 'cte', 'evento' ou 'outro'."""
    tipo = d.get("tipo")
    if tipo == "cte":
        linha = linha_do_cte(emp_id, cnpj, d, ult_nsu_consulta)
    elif tipo == "evento":
        linha = linha_do_evento(emp_id, d, ult_nsu_consulta)
    else:   # schema desconhecido: guarda o XML cru para nao perder o NSU
        linha = {"empresa_id": emp_id, "nsu": d.get("nsu"), "schema": d.get("schema"), "tipo": "outro",
                 "situacao": "outro", "obs": d.get("erro_leitura"), "xml": d.get("xml"),
                 "ultimo_nsu_consulta": ult_nsu_consulta}
        tipo = "outro"
    st, r = _rest("POST", "oct_cte_recebidos?on_conflict=empresa_id,nsu", body=linha,
                  prefer="resolution=ignore-duplicates,return=minimal")
    if st not in (200, 201, 204):
        print(f"[cte-auto] {emp_id}: nao gravou NSU {d.get('nsu')}: HTTP {st} {str(r)[:160]}")
        return "erro"
    # evento que muda a situacao do CT-e (so' o que a SEFAZ registrou)
    if tipo == "evento" and d.get("chave") and d.get("evento_registrado"):
        alvo = f"oct_cte_recebidos?empresa_id=eq.{emp_id}&tipo=eq.cte&chave_cte=eq.{d['chave']}"
        ev = d.get("evento_tipo")
        if ev == "110111":
            _rest("PATCH", alvo, body={"situacao": "cancelado", "cancelado_em": d.get("evento_em")}, prefer="return=minimal")
        elif ev == "610110":
            _rest("PATCH", alvo, body={"desacordo": True}, prefer="return=minimal")
        elif ev == "610111":
            _rest("PATCH", alvo, body={"desacordo": False}, prefer="return=minimal")
    return tipo


def processar_empresa(emp):
    """Consulta a SEFAZ (se for a hora) e grava os CT-e novos da empresa. Devolve um resumo."""
    emp_id = emp["id"]
    if not ligado():
        return {"ok": False, "motivo": "CTE_AUTO desligado"}
    if not _tabelas_existem():
        return {"ok": False, "motivo": "tabelas do CT-e nao existem (rode o SQL-CTE-RECEBIDOS.sql)"}
    cnpj = _so_digitos(emp.get("cnpj"))
    if len(cnpj) != 14:
        return {"ok": False, "motivo": "empresa sem CNPJ"}
    if not _claim(emp_id):
        return {"ok": False, "motivo": "ainda nao e' hora (ou bloqueio 656 em vigor)", "controle": _controle(emp_id)}
    ambiente = (os.environ.get("CTE_AMBIENTE") or os.environ.get("DFE_AMBIENTE") or "producao").strip().lower()
    try:
        dados = carregar_empresa(emp_id)
    except Exception as e:
        _set_controle(emp_id, {"ultimo_status": f"cert nao carregou: {str(e)[:160]}"})
        return {"ok": False, "motivo": f"certificado nao carregou: {str(e)[:120]}"}
    ult_nsu = str(_controle(emp_id).get("ultimo_nsu") or "0")
    cont = {"cte": 0, "evento": 0, "outro": 0, "erro": 0}
    status = ""
    for _ in range(MAX_PAGINAS):
        try:
            resp = consultar_cte(cnpj, dados["cert_base64"], dados["cert_senha"], ambiente, ult_nsu)
        except Exception as e:
            status = f"erro na consulta: {str(e)[:160]}"
            _set_controle(emp_id, {"ultimo_status": status})
            break
        if resp.get("erro"):
            status = f"amb={ambiente} erro: {str(resp['erro'])[:150]}"
            _set_controle(emp_id, {"ultimo_status": status})
            break
        cstat, xmot = str(resp.get("cstat") or ""), str(resp.get("xmotivo") or "")
        if cstat == "656":
            status = f"656 consumo indevido -> pausa {BLOQUEIO_656_MIN}min"
            _set_controle(emp_id, {"ultimo_nsu": ult_nsu, "ultimo_status": status,
                                   "bloqueado_ate": _iso(_agora() + timedelta(minutes=BLOQUEIO_656_MIN))})
            break
        for d in (resp.get("docs") or []):
            cont[_salvar(emp_id, cnpj, d, resp.get("ultimo_nsu"))] += 1
        novo = str(resp.get("ultimo_nsu") or ult_nsu)
        # so' avanca o NSU se nada falhou ao gravar: lote com erro e' pedido de novo na proxima consulta
        if cont["erro"]:
            status = f"amb={ambiente} cStat {cstat}: {cont['erro']} documento(s) nao gravaram; NSU mantido em {ult_nsu}"
            _set_controle(emp_id, {"ultimo_status": status})
            break
        status = (f"amb={ambiente} cStat {cstat} {xmot} | ult {novo}/max {resp.get('max_nsu')} | "
                  f"+{cont['cte']} CT-e, +{cont['evento']} evento(s)")
        _set_controle(emp_id, {"ultimo_nsu": novo, "ultimo_status": status})
        if cstat == "137" or novo == ult_nsu:
            break
        try:
            if int(novo) >= int(str(resp.get("max_nsu") or novo)):
                break
        except (TypeError, ValueError):
            pass
        ult_nsu = novo
        time.sleep(1.5)
    if cont["cte"] or cont["evento"]:
        print(f"[cte-auto] {emp_id}: {cont['cte']} CT-e e {cont['evento']} evento(s) gravados")
    return {"ok": True, "status": status, **cont}


def consultar_agora(empresa_id):
    """Botao 'Consultar agora' do retaguarda: respeita o mesmo intervalo/bloqueio do robo."""
    rows = _rest_get("oct_empresas", f"?id=eq.{empresa_id}&select=id,cnpj,ativo")
    if not rows:
        return {"ok": False, "motivo": "empresa nao encontrada"}
    r = processar_empresa(rows[0])
    r["controle"] = _controle(empresa_id) if _tabelas_existem() else {}
    return r
