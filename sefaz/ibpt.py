# -*- coding: utf-8 -*-
"""
Valor aproximado dos tributos (Lei 12.741 / Decreto 8.264) para a NFC-e/NF-e.

ARQUIVO IDÊNTICO no servidor SEFAZ (sefaz/ibpt.py) e no núcleo do posto
(nucleo/fiscal/ibpt.py) — mudou aqui, copia lá.

De onde vêm os percentuais (03/10/2026, pedido do Ronan):
  1. Tabela IBPT da NUVEM (oct_ibpt), gravada pelo servidor SEFAZ todo dia
     (sefaz/ibpt_sync.py): API oficial do IBPT quando há token, senão o
     espelho público das tabelas. Cópia local em ibpt_cache_mg.json.
  2. Se a nuvem não respondeu nunca: a tabela embutida no pacote (ibpt_mg.csv).
  3. IMPOSTO REAL dos combustíveis, como o TecnoX (oct_ibpt_ajuste): o estadual
     é o ICMS monofásico de verdade -- ad rem do produto × litros (diesel
     R$ 1,17/L) -- e o federal é o percentual do ajuste (diesel 5,50%). O
     TecnoX imprime exatamente isso; a média do IBPT para o diesel (35,80%)
     dava quase o dobro do imposto real (22,31%).

Puramente informativo: não altera imposto devido. À prova de falha: sem
tabela ou sem NCM, devolve 0 e a emissão segue.
"""
import csv
import json
import os
import re
import threading
import time
import urllib.request

UF = "MG"
TTL_SEG = 6 * 3600                      # de quanto em quanto confere a nuvem
_DIR = os.path.dirname(os.path.abspath(__file__))
_CSV = os.path.join(_DIR, "ibpt_mg.csv")
_CACHE = os.path.join(_DIR, "ibpt_cache_mg.json")

# origens de mercadoria importada (NF-e/ICMS) -> usa a coluna federal "importado"
_ORIGEM_IMPORTADA = {"1", "2", "3", "6", "7", "8"}

# Ajuste "imposto real" que vale mesmo sem a nuvem (igual ao seed do
# SQL-IBPT.sql, valores do cadastro do TecnoX do Florestal/AC). A tabela
# oct_ibpt_ajuste, quando lida, substitui este.
_AJUSTE_PADRAO = {
    "27101921": {"federal_pct": 5.50, "estadual_pct": 14.00, "usa_ad_rem": True},   # diesel
    "27101259": {"federal_pct": 13.45, "estadual_pct": 18.00, "usa_ad_rem": True},  # gasolina
    "22071090": {"federal_pct": 13.45, "estadual_pct": 9.29, "usa_ad_rem": False},  # etanol hidratado
}

_CSV_TAB = None                         # ncm -> (nac, imp, est, mun) do pacote
_NUVEM = {"tab": None, "ajuste": None, "versao": "", "fonte": "", "lido_em": 0.0}
_trava = threading.Lock()
_buscando = [False]


# ---------------- carga ----------------
def _carregar_csv():
    global _CSV_TAB
    if _CSV_TAB is not None:
        return _CSV_TAB
    tab = {}
    try:
        with open(_CSV, "r", encoding="utf-8", newline="") as f:
            rd = csv.reader(f, delimiter=";")
            next(rd, None)
            for row in rd:
                if len(row) < 5 or not (row[0] or "").strip():
                    continue
                try:
                    tab[row[0].strip()] = tuple(float(x or 0) for x in row[1:5])
                except ValueError:
                    continue
    except Exception:
        pass
    _CSV_TAB = tab
    return tab


def _ler_cache():
    """Cópia local da última leitura da nuvem (vale com o posto sem internet)."""
    try:
        with open(_CACHE, "r", encoding="utf-8") as f:
            c = json.load(f)
        tab = {k: tuple(v) for k, v in (c.get("tab") or {}).items()}
        with _trava:
            if _NUVEM["tab"] is None and tab:
                _NUVEM.update(tab=tab, ajuste=c.get("ajuste"), versao=c.get("versao") or "",
                              fonte=c.get("fonte") or "", lido_em=float(c.get("lido_em") or 0))
    except Exception:
        pass


def _supabase():
    """Servidor SEFAZ: variáveis de ambiente. Núcleo: a config da nuvem do posto."""
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_KEY", "") or os.environ.get("SUPABASE_KEY", "")).strip()
    if url and key:
        return url, key
    try:
        from nucleo import sync_nuvem   # só existe no núcleo
        u, k = sync_nuvem._cfg()
        if u and k:
            return str(u).rstrip("/"), k
    except Exception:
        pass
    try:
        from nucleo import contexto     # config lida do disco (vale antes do boot terminar)
        sb = contexto.secao_config("supabase") or {}
        u, k = sb.get("url"), sb.get("service_key") or sb.get("key")
        if u and k and not str(k).startswith("COLE_AQUI"):
            return str(u).rstrip("/"), k
    except Exception:
        pass
    return None, None


def _get(url, key, caminho):
    req = urllib.request.Request(f"{url}/rest/v1/{caminho}",
                                 headers={"apikey": key, "Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8") or "[]")


def _buscar_nuvem():
    """Roda em segundo plano: nunca segura uma emissão esperando a internet."""
    try:
        url, key = _supabase()
        if not url:
            return
        ver = _get(url, key, f"oct_ibpt_versao?uf=eq.{UF}&select=versao,fonte")
        aj = _get(url, key, f"oct_ibpt_ajuste?uf=eq.{UF}&select=ncm,federal_pct,estadual_pct,usa_ad_rem")
        ajuste = {a["ncm"]: {"federal_pct": a.get("federal_pct"), "estadual_pct": a.get("estadual_pct"),
                             "usa_ad_rem": a.get("usa_ad_rem", True)} for a in aj}
        versao = (ver[0].get("versao") if ver else "") or ""
        fonte = (ver[0].get("fonte") if ver else "") or ""
        tab = None
        if versao and versao != _NUVEM.get("versao"):
            tab, off = {}, 0
            while True:     # PostgREST corta em 1000: pagina até acabar
                d = _get(url, key, f"oct_ibpt?uf=eq.{UF}&tipo=eq.0&ex=eq.&select=codigo,nacional,importado,estadual,municipal"
                                   f"&order=codigo&limit=1000&offset={off}")
                for r in d:
                    tab[r["codigo"]] = (float(r.get("nacional") or 0), float(r.get("importado") or 0),
                                        float(r.get("estadual") or 0), float(r.get("municipal") or 0))
                if len(d) < 1000:
                    break
                off += 1000
            if len(tab) < 1000:          # tabela incompleta: não troca a boa pela ruim
                tab = None
        with _trava:
            if tab is not None:
                _NUVEM.update(tab=tab, versao=versao, fonte=fonte)
            _NUVEM["ajuste"] = ajuste
            _NUVEM["lido_em"] = time.time()
            copia = {"versao": _NUVEM["versao"], "fonte": _NUVEM["fonte"], "lido_em": _NUVEM["lido_em"],
                     "ajuste": _NUVEM["ajuste"], "tab": {k: list(v) for k, v in (_NUVEM["tab"] or {}).items()}}
        try:
            tmp = _CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(copia, f)
            os.replace(tmp, _CACHE)
        except Exception:
            pass
    except Exception:
        pass        # sem nuvem: segue com o cache/pacote
    finally:
        _buscando[0] = False


def _garantir():
    if _NUVEM["tab"] is None and _NUVEM["lido_em"] == 0:
        _ler_cache()
    if time.time() - _NUVEM["lido_em"] > TTL_SEG and not _buscando[0]:
        _buscando[0] = True
        threading.Thread(target=_buscar_nuvem, daemon=True).start()


def atualizar_agora(esperar=True):
    """Força a leitura da nuvem (tarefa do servidor depois de gravar versão nova)."""
    _buscando[0] = True
    if esperar:
        _buscar_nuvem()
    else:
        threading.Thread(target=_buscar_nuvem, daemon=True).start()


def versao():
    _garantir()
    return _NUVEM.get("versao") or "pacote"


# ---------------- cálculo ----------------
def _linha(ncm):
    _garantir()
    ncm = re.sub(r"\D", "", str(ncm or ""))[:8]
    tab = _NUVEM.get("tab")
    if tab and ncm in tab:
        return tab[ncm]
    return _carregar_csv().get(ncm)


def _ajuste(ncm):
    ncm = re.sub(r"\D", "", str(ncm or ""))[:8]
    aj = _NUVEM.get("ajuste")
    if aj is None:
        aj = _AJUSTE_PADRAO
    return aj.get(ncm)


def partes_item(vprod, ncm, origem="0", item=None):
    """(federal, estadual, municipal) em R$ do item, sem arredondar."""
    try:
        v = float(vprod or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0
    lin = _linha(ncm)
    nac, imp, est, mun = lin if lin else (0.0, 0.0, 0.0, 0.0)
    fed_pct = imp if str(origem or "0") in _ORIGEM_IMPORTADA else nac
    est_pct = est
    aj = _ajuste(ncm)
    federal = estadual = None
    if aj:
        if aj.get("federal_pct") is not None:
            fed_pct = float(aj["federal_pct"])
        if aj.get("usa_ad_rem", True) and item:
            # imposto REAL: ICMS monofásico do produto (R$/litro) × quantidade
            try:
                adrem = float(item.get("aliq_icms_ad_rem") or 0)
                qtd = float(item.get("qCom") or 0)
            except (TypeError, ValueError):
                adrem = qtd = 0.0
            if adrem > 0 and qtd > 0:
                estadual = adrem * qtd
        if estadual is None and aj.get("estadual_pct") is not None:
            est_pct = float(aj["estadual_pct"])
        if not lin:
            mun = 0.0
    federal = v * fed_pct / 100.0
    if estadual is None:
        estadual = v * est_pct / 100.0
    return federal, estadual, v * mun / 100.0


def aliquota_pct(ncm, origem="0"):
    """% aproximado (federal + estadual + municipal) pela tabela, sem o ajuste por litro."""
    lin = _linha(ncm)
    if not lin:
        return 0.0
    nac, imp, est, mun = lin
    return (imp if str(origem or "0") in _ORIGEM_IMPORTADA else nac) + est + mun


def v_item_trib(vprod, ncm, origem="0", item=None):
    """Tributo aproximado (R$) de um item -- o que vai no <vTotTrib> do item."""
    f, e, m = partes_item(vprod, ncm, origem, item)
    return round(f + e + m, 2)


def v_tot_trib(itens):
    """Soma do tributo aproximado dos itens (tem de bater com a soma dos itens: rejeição 685)."""
    return round(sum(v_item_trib(it.get("vProd"), it.get("ncm"), it.get("origem", "0"), it) for it in (itens or [])), 2)


def resumo(itens):
    """Totais por ente para imprimir, como o TecnoX:
    {total, federal, estadual, municipal, base, versao}."""
    fed = est = mun = base = 0.0
    total = 0.0
    for it in (itens or []):
        f, e, m = partes_item(it.get("vProd"), it.get("ncm"), it.get("origem", "0"), it)
        fed += f; est += e; mun += m
        total += round(f + e + m, 2)
        try:
            base += float(it.get("vProd") or 0)
        except (TypeError, ValueError):
            pass
    return {"total": round(total, 2), "federal": round(fed, 2), "estadual": round(est, 2),
            "municipal": round(mun, 2), "base": round(base, 2), "versao": versao()}


def _br(v):
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def texto_cupom(itens):
    """Linha das informações adicionais, no formato do TecnoX:
    'Val. Aprox. Tributos: R$ 96,57 (22,31%), Federal: R$ 23,81 (5,50%), ...'."""
    r = resumo(itens)
    if r["total"] <= 0 or r["base"] <= 0:
        return ""
    p = lambda x: _br(x / r["base"] * 100.0)
    return (f"Val. Aprox. Tributos: R$ {_br(r['total'])} ({p(r['total'])}%), "
            f"Federal: R$ {_br(r['federal'])} ({p(r['federal'])}%), "
            f"Estadual: R$ {_br(r['estadual'])} ({p(r['estadual'])}%), "
            f"Municipal: R$ {_br(r['municipal'])} ({p(r['municipal'])}%) Fonte: IBPT")
