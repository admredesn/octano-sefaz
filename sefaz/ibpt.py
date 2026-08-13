# -*- coding: utf-8 -*-
"""
Tabela IBPT (Lei 12.741 — "valor aproximado dos tributos") para MG.
Fonte: IBPT/empresometro.com.br, extraida do pacote ACBr (TabelaIBPTaxMG).

Uso: v_tot_trib(itens) devolve a soma do tributo aproximado dos itens da nota,
para preencher <vTotTrib> na NFC-e/NF-e e imprimir "Val. Aprox. Tributos" no
cupom. E puramente informativo: nao altera imposto devido.

A prova de falha: se a tabela nao carregar ou o NCM nao existir, devolve 0.0
e a emissao segue normal.
"""
import csv
import os
import re

_TABELA = None  # ncm(str8) -> (nac, imp, est, mun) em %

# origens de mercadoria importada (NF-e/ICMS) -> usa a coluna federal "importado"
_ORIGEM_IMPORTADA = {"1", "2", "3", "6", "7", "8"}


def _carregar():
    global _TABELA
    if _TABELA is not None:
        return _TABELA
    _TABELA = {}
    caminho = os.path.join(os.path.dirname(__file__), "ibpt_mg.csv")
    try:
        with open(caminho, "r", encoding="utf-8", newline="") as f:
            rd = csv.reader(f, delimiter=";")
            next(rd, None)  # cabecalho
            for row in rd:
                if len(row) < 5:
                    continue
                ncm = (row[0] or "").strip()
                if not ncm:
                    continue
                try:
                    _TABELA[ncm] = (
                        float(row[1] or 0), float(row[2] or 0),
                        float(row[3] or 0), float(row[4] or 0),
                    )
                except ValueError:
                    continue
    except Exception:
        # tabela ausente/corrompida: segue com dicionario vazio -> tudo 0.0
        pass
    return _TABELA


def aliquota_pct(ncm, origem="0"):
    """% aproximado de tributos (federal + estadual + municipal) para o NCM."""
    tab = _carregar()
    ncm = re.sub(r"\D", "", str(ncm or ""))[:8]
    dados = tab.get(ncm)
    if not dados:
        return 0.0
    nac, imp, est, mun = dados
    federal = imp if str(origem or "0") in _ORIGEM_IMPORTADA else nac
    return federal + est + mun


def v_item_trib(vprod, ncm, origem="0"):
    """Tributo aproximado (R$) de um item."""
    try:
        return round(float(vprod) * aliquota_pct(ncm, origem) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def v_tot_trib(itens):
    """Soma do tributo aproximado (R$) dos itens da nota."""
    total = 0.0
    for it in (itens or []):
        total += v_item_trib(it.get("vProd"), it.get("ncm"), it.get("origem", "0"))
    return round(total, 2)
