# -*- coding: utf-8 -*-
"""
Atualização automática da tabela IBPT (Lei 12.741) — 03/10/2026, pedido do Ronan.

Roda no servidor SEFAZ (Railway), de 6 em 6 horas. O IBPT publica versão nova
mais ou menos todo mês (ex.: 26.2.B, vigência 20/09 a 31/10/2026). A tarefa:

  1. ESPELHO PÚBLICO (github luizinhoh2o1/tabelas-ibpt, pasta repositorio-ibpt):
     acha o zip mais novo (TabelaIBPTax_26.2.B.zip), e se for mais novo que o
     gravado, baixa e grava a tabela INTEIRA da UF em oct_ibpt.
  2. API OFICIAL DO IBPT (De Olho no Imposto), quando houver IBPT_TOKEN e
     IBPT_CNPJ no Railway (o token sai do cadastro da empresa no portal do
     IBPT): confere os NCMs que a rede vende e, se o valor oficial diferir,
     grava o oficial por cima.
  3. Avisa os emissores: o servidor relê na hora; os núcleos releem sozinhos
     (de 6 em 6 h) pela tabela oct_ibpt_versao.

Liga/desliga: IBPT_SYNC=0 no Railway desliga (padrão: ligado).
Rodar à mão:  python -m sefaz.ibpt_sync   (com SUPABASE_URL/SUPABASE_SERVICE_KEY)
"""
import csv
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

UF = "MG"
CICLO_SEG = 6 * 3600
ESPELHO_LISTA = "https://api.github.com/repos/luizinhoh2o1/tabelas-ibpt/contents/repositorio-ibpt"
API_IBPT = "https://apidoni.ibpt.org.br/api/v1/produtos"


def _log(msg):
    print(f"[ibpt] {msg}", flush=True)


def _supa():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY ausentes")
    return url, key


def _rest(metodo, caminho, corpo=None, prefer=None):
    url, key = _supa()
    h = {"apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if prefer:
        h["Prefer"] = prefer
    req = urllib.request.Request(f"{url}/rest/v1/{caminho}", method=metodo, headers=h,
                                 data=json.dumps(corpo).encode("utf-8") if corpo is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt else None


def _http(url, timeout=60, cabecalhos=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": "octano-ibpt-sync"}, **(cabecalhos or {})))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _versao_tupla(v):
    """'26.2.B' -> (26, 2, 'B') para comparar versões."""
    m = re.match(r"^(\d+)\.(\d+)\.([A-Z]+)$", str(v or "").strip().upper())
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else (0, 0, "")


def _data_iso(s):
    s = str(s or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    return m.group(0) if m else None


def _num(s):
    try:
        return float(str(s or "0").replace(",", "."))
    except ValueError:
        return 0.0


def _versao_gravada():
    d = _rest("GET", f"oct_ibpt_versao?uf=eq.{UF}&select=*")
    return d[0] if d else {}


# ---------------- 1) espelho público ----------------
def _ultima_do_espelho():
    lista = json.loads(_http(ESPELHO_LISTA, cabecalhos={"Accept": "application/vnd.github+json"}).decode("utf-8"))
    melhor = None
    for x in lista:
        m = re.match(r"^TabelaIBPTax_(\d+\.\d+\.[A-Z]+)\.zip$", x.get("name") or "", re.I)
        if m and (melhor is None or _versao_tupla(m.group(1)) > _versao_tupla(melhor[0])):
            melhor = (m.group(1).upper(), x.get("download_url"))
    return melhor          # (versao, url) ou None


def _ler_zip_uf(conteudo, versao):
    z = zipfile.ZipFile(io.BytesIO(conteudo))
    nome = next((n for n in z.namelist() if re.search(rf"IBPTax{UF}", n, re.I)), None)
    if not nome:
        raise RuntimeError(f"zip sem a tabela de {UF}")
    bruto = z.read(nome)
    try:
        txt = bruto.decode("utf-8")
    except UnicodeDecodeError:
        txt = bruto.decode("latin-1")
    linhas = list(csv.reader(io.StringIO(txt), delimiter=";"))
    cab = [c.strip().lower() for c in linhas[0]]
    i = {c: cab.index(c) for c in cab}
    out = []
    for r in linhas[1:]:
        if len(r) < len(cab) or not r[i["codigo"]].strip():
            continue
        out.append({
            "uf": UF, "codigo": r[i["codigo"]].strip(), "ex": r[i["ex"]].strip(), "tipo": r[i["tipo"]].strip() or "0",
            "descricao": r[i["descricao"]][:500], "nacional": _num(r[i["nacionalfederal"]]),
            "importado": _num(r[i["importadosfederal"]]), "estadual": _num(r[i["estadual"]]),
            "municipal": _num(r[i["municipal"]]), "vigencia_ini": _data_iso(r[i["vigenciainicio"]]),
            "vigencia_fim": _data_iso(r[i["vigenciafim"]]), "chave": r[i["chave"]].strip(),
            "versao": r[i["versao"]].strip() or versao, "fonte": r[i["fonte"]].strip(),
        })
    return out


def _gravar_tabela(linhas, versao, origem):
    if len(linhas) < 1000:
        raise RuntimeError(f"tabela {versao} veio com só {len(linhas)} linhas — não gravo")
    # sem duplicata de chave dentro do mesmo lote (o upsert recusa)
    unicas = {}
    for x in linhas:
        unicas[(x["codigo"], x["ex"], x["tipo"])] = x
    linhas = list(unicas.values())
    for i in range(0, len(linhas), 1000):
        _rest("POST", "oct_ibpt?on_conflict=uf,codigo,ex,tipo", linhas[i:i + 1000],
              prefer="resolution=merge-duplicates,return=minimal")
    # o que sumiu da tabela nova sai (fica só a versão vigente)
    _rest("DELETE", f"oct_ibpt?uf=eq.{UF}&versao=neq.{urllib.parse.quote(versao)}", prefer="return=minimal")
    agora = datetime.now(timezone.utc).isoformat()
    x0 = linhas[0]
    _rest("POST", "oct_ibpt_versao?on_conflict=uf", [{
        "uf": UF, "versao": versao, "vigencia_ini": x0.get("vigencia_ini"), "vigencia_fim": x0.get("vigencia_fim"),
        "fonte": x0.get("fonte"), "origem": origem, "linhas": len(linhas),
        "atualizado_em": agora, "conferido_em": agora, "erro": None}],
        prefer="resolution=merge-duplicates,return=minimal")
    _log(f"{UF}: versão {versao} gravada ({len(linhas)} linhas, {origem})")


def _pelo_espelho(gravada):
    ult = _ultima_do_espelho()
    if not ult:
        _log("espelho sem zips — nada a fazer")
        return False
    versao, url = ult
    atual = (gravada or {}).get("versao") or ""
    if _versao_tupla(versao) <= _versao_tupla(atual):
        return False
    _log(f"{UF}: versão nova no espelho {versao} (gravada: {atual or 'nenhuma'})")
    linhas = _ler_zip_uf(_http(url, timeout=180), versao)
    _gravar_tabela(linhas, versao, "espelho")
    return True


# ---------------- 2) API oficial (com token) ----------------
def _ncms_da_rede():
    ncms, off = set(), 0
    while True:
        d = _rest("GET", f"oct_produtos?select=ncm&ncm=not.is.null&limit=1000&offset={off}")
        ncms |= {re.sub(r"\D", "", x["ncm"] or "")[:8] for x in d}
        if len(d) < 1000:
            break
        off += 1000
    return sorted(n for n in ncms if len(n) == 8)


def _pela_api():
    token = os.environ.get("IBPT_TOKEN", "").strip()
    cnpj = re.sub(r"\D", "", os.environ.get("IBPT_CNPJ", ""))
    if not token or not cnpj:
        return 0
    mudou = 0
    for ncm in _ncms_da_rede():
        q = urllib.parse.urlencode({"token": token, "cnpj": cnpj, "codigo": ncm, "uf": UF, "ex": 0,
                                    "descricao": "PRODUTO", "unidadeMedida": "UN", "valor": "1", "gtin": "SEM GTIN"})
        try:
            r = json.loads(_http(f"{API_IBPT}?{q}", timeout=30).decode("utf-8"))
        except Exception as e:
            _log(f"API IBPT {ncm}: {e}")
            continue
        if not isinstance(r, dict) or r.get("Nacional") is None:
            continue
        linha = {"uf": UF, "codigo": ncm, "ex": "", "tipo": "0", "descricao": (r.get("Descricao") or "")[:500],
                 "nacional": _num(r.get("Nacional")), "importado": _num(r.get("Importado")),
                 "estadual": _num(r.get("Estadual")), "municipal": _num(r.get("Municipal")),
                 "vigencia_ini": _data_iso(r.get("VigenciaInicio")), "vigencia_fim": _data_iso(r.get("VigenciaFim")),
                 "chave": r.get("Chave"), "versao": r.get("Versao"), "fonte": (r.get("Fonte") or "IBPT") + " (API)"}
        atual = _rest("GET", f"oct_ibpt?uf=eq.{UF}&codigo=eq.{ncm}&ex=eq.&tipo=eq.0&select=nacional,importado,estadual,municipal")
        if atual and all(abs(_num(atual[0][k]) - linha[k]) < 0.005 for k in ("nacional", "importado", "estadual", "municipal")):
            continue
        _rest("POST", "oct_ibpt?on_conflict=uf,codigo,ex,tipo", [linha], prefer="resolution=merge-duplicates,return=minimal")
        mudou += 1
        time.sleep(0.3)
    if mudou:
        _log(f"API oficial: {mudou} NCM(s) da rede corrigidos com o valor oficial")
    return mudou


# ---------------- ciclo ----------------
def ciclo():
    gravada = {}
    try:
        gravada = _versao_gravada()
        novo = _pelo_espelho(gravada)
        oficiais = _pela_api()
        agora = datetime.now(timezone.utc).isoformat()
        if not novo:
            _rest("PATCH", f"oct_ibpt_versao?uf=eq.{UF}", {"conferido_em": agora, "erro": None}, prefer="return=minimal")
        if novo or oficiais:
            try:
                from . import ibpt
                ibpt.atualizar_agora(esperar=False)    # o emissor deste servidor relê já
            except Exception:
                pass
        return {"ok": True, "versao_nova": novo, "oficiais": oficiais}
    except Exception as e:
        _log(f"falha: {e}")
        try:
            if gravada:
                _rest("PATCH", f"oct_ibpt_versao?uf=eq.{UF}", {"erro": str(e)[:500],
                      "conferido_em": datetime.now(timezone.utc).isoformat()}, prefer="return=minimal")
        except Exception:
            pass
        return {"ok": False, "erro": str(e)}


_on = False


def iniciar():
    global _on
    if _on or os.environ.get("IBPT_SYNC", "1").strip().lower() in ("0", "false", "nao", "off"):
        return
    _on = True

    def loop():
        time.sleep(90)              # deixa o boot estabilizar
        while True:
            ciclo()
            time.sleep(CICLO_SEG)

    threading.Thread(target=loop, daemon=True).start()
    _log(f"atualização automática ativa (a cada {CICLO_SEG // 3600} h)")


if __name__ == "__main__":
    print(ciclo())
