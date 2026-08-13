# -*- coding: utf-8 -*-
"""
Tabela de codigos de produtos da ANP (cProdANP) para o grupo <comb> da NF-e/NFC-e.
Fonte: Tabela oficial cProdANP (SIMP/ANP), 9 digitos.

Uso conservador e a prova de falha:
  - descricao(cod)  -> descANP oficial (para preencher <descANP> quando o item
                       nao trouxer descricao propria). NUNCA sobrescreve o cadastro.
  - existe(cod)     -> True se o codigo consta na tabela (validacao/aviso).

NAO altera o cProdANP que vem do cadastro do produto (a emissao ja funciona com
ele). Serve so para completar/validar. Se a tabela faltar, tudo devolve vazio/False
e a emissao segue igual.
"""
import csv
import os
import re

_TABELA = None  # cod(str9) -> descricao


def _carregar():
    global _TABELA
    if _TABELA is not None:
        return _TABELA
    _TABELA = {}
    caminho = os.path.join(os.path.dirname(__file__), "tabela_anp.csv")
    try:
        with open(caminho, "r", encoding="utf-8", newline="") as f:
            rd = csv.reader(f, delimiter=";")
            next(rd, None)
            for row in rd:
                if len(row) >= 2 and row[0].strip():
                    _TABELA[row[0].strip()] = row[1].strip()
    except Exception:
        pass
    return _TABELA


def _norm(cod):
    return re.sub(r"\D", "", str(cod or ""))[:9]


def existe(cod):
    return _norm(cod) in _carregar()


def descricao(cod):
    return _carregar().get(_norm(cod), "")
