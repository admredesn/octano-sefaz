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

09/10/2026: com prazo (emissao da NFC-e) as fontes correm em PARALELO, e no
nucleo o que foi achado fica tambem em disco (cnpj_cache.json) -- ver _corrida.
"""

import json
import os
import re
import threading
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


# ---------------------------------------------------------------------------
# MEMORIA EM DISCO (09/10/2026) -- so' no nucleo do posto.
# O que ja' foi achado fica guardado ao lado do banco: reiniciar o nucleo (toda
# publicacao reinicia) nao apaga, e com TODAS as fontes fora o cliente que ja'
# comprou continua saindo com o endereco DELE. No servidor nao ha' pasta do
# posto: fica so' a memoria, como antes.
# ---------------------------------------------------------------------------
_DISCO_TRAVA = threading.Lock()
_DISCO_LIDO = False
_DISCO_MAX = 3000


def _arquivo_disco():
    p = os.environ.get("OCTANO_CNPJ_CACHE", "").strip()
    if p:
        return None if p == "-" else p
    try:
        from ..db.banco import base_dir      # so' existe no nucleo
        return os.path.join(base_dir(), "cnpj_cache.json")
    except Exception:
        return None


def _ler_disco():
    global _DISCO_LIDO
    if _DISCO_LIDO:
        return
    with _DISCO_TRAVA:
        if _DISCO_LIDO:
            return
        _DISCO_LIDO = True
        arq = _arquivo_disco()
        if not arq or not os.path.exists(arq):
            return
        try:
            with open(arq, encoding="utf-8") as f:
                guardado = json.load(f)
            for c, par in (guardado or {}).items():
                if c not in _CACHE and isinstance(par, list) and len(par) == 2 and isinstance(par[1], dict):
                    _CACHE[c] = (float(par[0]), par[1])
        except Exception:
            pass                               # arquivo estragado: recomeca vazio


def _guardar(c, dados):
    """Memoria + disco. Nunca derruba a consulta por falha de gravacao."""
    with _DISCO_TRAVA:
        _CACHE[c] = (time.time(), dados)
    arq = _arquivo_disco()
    if not arq:
        return
    try:
        with _DISCO_TRAVA:
            itens = sorted(list(_CACHE.items()), key=lambda kv: kv[1][0])[-_DISCO_MAX:]
            tmp = arq + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: [v[0], v[1]] for k, v in itens}, f, ensure_ascii=False)
            os.replace(tmp, arq)
    except Exception:
        pass


def _serve(dados, exige_ibge):
    return bool(dados) and (not exige_ibge or bool(dados.get("codigo_municipio_ibge")))


# Com prazo, a 1a fonte tem este tempo sozinha; passou disso, as reservas saem
# JUNTAS e vale a primeira que responder.
_ESPERA_PRIMEIRA = 0.7


def _corrida(c, prazo, exige_ibge):
    """Consulta COM PRAZO: as fontes correm em paralelo.

    Antes era uma depois da outra, e o prazo valia para a soma: em 09/10/2026 a
    BrasilAPI (a 1a) levou 6 s para devolver erro, gastou os 2,5 s inteiros da
    emissao e as outras tres -- que respondiam em 0,7 s -- nem foram tentadas.
    Sem endereco, a NFC-e saiu com o endereco do POSTO no lugar do do cliente e
    a SEFAZ recusou (482: municipio do destinatario diverge do cadastro) a venda
    da Degraus, contribuinte de outra cidade, no Florestal.

    Devolve (dados|None, viu_404). Quem chegar DEPOIS do prazo ainda guarda o
    que achou: a proxima tentativa do operador ja' sai da memoria."""
    fim = time.time() + prazo
    res = {}

    def roda(nome, url, tmo, ler):
        st, dados = 0, None
        try:
            st, bruto = _get(url.format(c=c), max(0.3, min(tmo, fim - time.time() + 1.5)))
            dados = ler(bruto) if st == 200 else None
            if dados:
                dados["_fonte"] = nome
                velho = (_CACHE.get(c) or (0, None))[1]
                # nao troca um achado COM codigo do municipio por um sem
                if _serve(dados, True) or not _serve(velho, True):
                    _guardar(c, dados)
        except Exception:
            pass
        res[nome] = (st, dados)

    def lanca(fonte):
        threading.Thread(target=roda, args=fonte, daemon=True, name="cnpj-" + fonte[0]).start()

    lanca(FONTES[0])
    soltou = False
    reserva_em = time.time() + min(_ESPERA_PRIMEIRA, prazo / 3.0)
    while True:
        sem_ibge = None
        for nome, _u, _t, _l in FONTES:
            d = (res.get(nome) or (0, None))[1]
            if _serve(d, True):
                return d, False                 # com o codigo do municipio: serve a qualquer um
            if sem_ibge is None and d:
                sem_ibge = d
        agora = time.time()
        if not soltou and (agora >= reserva_em or FONTES[0][0] in res):
            for fonte in FONTES[1:]:
                lanca(fonte)
            soltou = True
        if agora >= fim or (soltou and len(res) == len(FONTES)):
            break
        time.sleep(0.03)
    # resposta SEM o codigo do municipio (ReceitaWS) so' no fim, e so' para quem
    # aceita: quem monta endereco com ela cai no endereco do posto.
    if sem_ibge and not exige_ibge:
        return sem_ibge, False
    return None, any(st == 404 for st, _d in res.values())


def consultar_cnpj(cnpj, prazo=None, validade=None, exige_ibge=False):
    """Devolve (status, dados). status: 200 achou; 404 nenhuma fonte conhece o
    CNPJ (e pelo menos uma respondeu 404); 502 nenhuma fonte respondeu.
    prazo: segundos no TOTAL. Usado DENTRO da emissao da NFC-e, que nao pode
      esperar 30 s por um endereco (30/09/2026). Com prazo as fontes correm em
      paralelo (ver _corrida).
    validade: por quanto tempo (s) um achado guardado dispensa consultar de novo
      (padrao 24 h; a emissao usa 30 dias -- endereco de empresa muda pouco).
    exige_ibge: so' serve resposta com o codigo do municipio (a ReceitaWS nao
      traz, e sem ele nao ha' <enderDest>)."""
    c = _so_digitos(cnpj)
    if len(c) != 14:
        return 400, {"erro": "CNPJ deve ter 14 digitos"}
    _ler_disco()
    hit = _CACHE.get(c)
    if hit and time.time() - hit[0] < (validade or _CACHE_SEG) and _serve(hit[1], exige_ibge):
        return 200, hit[1]
    viu_404 = False
    if prazo:
        dados, viu_404 = _corrida(c, prazo, exige_ibge)
        if dados:
            return 200, dados
    else:
        for nome, url, tmo, ler in FONTES:
            st, bruto = _get(url.format(c=c), tmo)
            if st == 404:
                viu_404 = True
                continue
            dados = ler(bruto) if st == 200 else None
            if _serve(dados, exige_ibge):
                dados["_fonte"] = nome
                _guardar(c, dados)
                return 200, dados
    # ninguem respondeu: o que ja' se sabia desse CNPJ vale mais que nada
    hit = _CACHE.get(c)
    if hit and _serve(hit[1], exige_ibge):
        return 200, hit[1]
    if viu_404:
        return 404, {"erro": "CNPJ nao encontrado"}
    return 502, {"erro": "Nenhuma fonte de CNPJ respondeu (BrasilAPI, cnpj.ws, ReceitaWS, CNPJa)"}
