"""
sefaz/cnpj_fontes.py  -  Consulta de CNPJ com fontes reserva (30/09/2026).

Por que existe: a rota /cnpj (cadastro de clientes no retaguarda e no PDV) e o
endereco do destinatario da NFC-e dependiam SO' da BrasilAPI. Em 30/09/2026 ela
devolveu 504 depois de 10 s para o CNPJ do POSTO SEVEN BH, que as outras fontes
publicas respondiam em 0,4 s — e o cadastro ficou sem razao social.

Ordem: BrasilAPI -> cnpj.ws -> ReceitaWS -> CNPJa. A resposta sai SEMPRE no
formato da BrasilAPI (as telas ja leem esses nomes), mais:
  - inscricao_estadual: IE ativa no estado do CNPJ, quando a fonte informa;
  - _fonte: de onde veio.
Achados ficam 24 h em memoria (cnpj.ws e ReceitaWS limitam 3 consultas/min).
"""

import re
import time

import requests

_UA = {"User-Agent": "Octano-Sistemas/1.0"}
_CACHE = {}            # cnpj -> (instante, dados)
_CACHE_SEG = 24 * 3600


def _get(url, timeout):
    try:
        r = requests.get(url, timeout=timeout, headers=_UA)
        if r.status_code == 404:
            return 404, None
        if r.status_code != 200:
            return r.status_code, None
        return 200, r.json()
    except Exception:
        return 0, None


def _so_digitos(s):
    return re.sub(r"\D", "", str(s or ""))


def _de_brasilapi(d):
    if not d or not d.get("razao_social"):
        return None
    d = dict(d)
    d.setdefault("inscricao_estadual", "")
    return d


def _de_cnpjws(d):
    if not d or not d.get("razao_social"):
        return None
    e = d.get("estabelecimento") or {}
    uf = (e.get("estado") or {}).get("sigla") or ""
    ies = [x for x in (e.get("inscricoes_estaduais") or [])
           if x and x.get("ativo") is not False and (not uf or (x.get("estado") or {}).get("sigla") == uf)]
    cid = e.get("cidade") or {}
    return {
        "cnpj": _so_digitos(e.get("cnpj")),
        "razao_social": d.get("razao_social"),
        "nome_fantasia": e.get("nome_fantasia") or "",
        "descricao_tipo_de_logradouro": e.get("tipo_logradouro") or "",
        "logradouro": " ".join(x for x in (e.get("tipo_logradouro"), e.get("logradouro")) if x),
        "numero": e.get("numero") or "",
        "complemento": e.get("complemento") or "",
        "bairro": e.get("bairro") or "",
        "municipio": cid.get("nome") or "",
        "codigo_municipio_ibge": cid.get("ibge_id"),
        "uf": uf,
        "cep": _so_digitos(e.get("cep")),
        "ddd_telefone_1": _so_digitos((e.get("ddd1") or "") + (e.get("telefone1") or "")),
        "email": e.get("email") or "",
        "descricao_situacao_cadastral": e.get("situacao_cadastral") or "",
        "opcao_pelo_simples": bool((d.get("simples") or {}).get("simples") == "Sim"),
        "inscricao_estadual": ies[0].get("inscricao_estadual") if ies else "",
    }


def _de_receitaws(d):
    if not d or d.get("status") != "OK" or not d.get("nome"):
        return None
    return {
        "cnpj": _so_digitos(d.get("cnpj")),
        "razao_social": d.get("nome"),
        "nome_fantasia": d.get("fantasia") or "",
        "logradouro": d.get("logradouro") or "",
        "numero": d.get("numero") or "",
        "complemento": d.get("complemento") or "",
        "bairro": d.get("bairro") or "",
        "municipio": (d.get("municipio") or "").title(),
        "codigo_municipio_ibge": None,
        "uf": d.get("uf") or "",
        "cep": _so_digitos(d.get("cep")),
        "ddd_telefone_1": _so_digitos((d.get("telefone") or "").split("/")[0]),
        "email": d.get("email") or "",
        "descricao_situacao_cadastral": d.get("situacao") or "",
        "opcao_pelo_simples": bool((d.get("simples") or {}).get("optante")),
        "inscricao_estadual": "",
    }


def _de_cnpja(d):
    if not d or not (d.get("company") or {}).get("name"):
        return None
    a = d.get("address") or {}
    f = (d.get("phones") or [{}])[0] or {}
    m = (d.get("emails") or [{}])[0] or {}
    return {
        "cnpj": _so_digitos(d.get("taxId")),
        "razao_social": d["company"]["name"],
        "nome_fantasia": d.get("alias") or "",
        "logradouro": a.get("street") or "",
        "numero": a.get("number") or "",
        "complemento": a.get("details") or "",
        "bairro": a.get("district") or "",
        "municipio": a.get("city") or "",
        "codigo_municipio_ibge": a.get("municipality"),
        "uf": a.get("state") or "",
        "cep": _so_digitos(a.get("zip")),
        "ddd_telefone_1": _so_digitos((f.get("area") or "") + (f.get("number") or "")),
        "email": m.get("address") or "",
        "descricao_situacao_cadastral": (d.get("status") or {}).get("text") or "",
        "opcao_pelo_simples": bool(((d.get("company") or {}).get("simples") or {}).get("optant")),
        "inscricao_estadual": "",
    }


FONTES = [
    ("BrasilAPI", "https://brasilapi.com.br/api/cnpj/v1/{c}", 6, _de_brasilapi),
    ("cnpj.ws", "https://publica.cnpj.ws/cnpj/{c}", 8, _de_cnpjws),
    ("ReceitaWS", "https://receitaws.com.br/v1/cnpj/{c}", 8, _de_receitaws),
    ("CNPJa", "https://open.cnpja.com/office/{c}", 8, _de_cnpja),
]


def consultar_cnpj(cnpj):
    """Devolve (status, dados). status: 200 achou; 404 nenhuma fonte conhece o
    CNPJ (e pelo menos uma respondeu 404); 502 nenhuma fonte respondeu."""
    c = _so_digitos(cnpj)
    if len(c) != 14:
        return 400, {"erro": "CNPJ deve ter 14 digitos"}
    hit = _CACHE.get(c)
    if hit and time.time() - hit[0] < _CACHE_SEG:
        return 200, hit[1]
    viu_404 = False
    for nome, url, tmo, ler in FONTES:
        st, bruto = _get(url.format(c=c), tmo)
        if st == 404:
            viu_404 = True
            continue
        dados = ler(bruto) if st == 200 else None
        if dados:
            dados["_fonte"] = nome
            _CACHE[c] = (time.time(), dados)
            return 200, dados
    if viu_404:
        return 404, {"erro": "CNPJ nao encontrado"}
    return 502, {"erro": "Nenhuma fonte de CNPJ respondeu (BrasilAPI, cnpj.ws, ReceitaWS, CNPJa)"}
