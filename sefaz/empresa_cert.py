"""
sefaz/empresa_cert.py  -  Carrega certificado + senha de uma empresa no servidor.

Multi-empresa: cada empresa tem
  - o certificado A1 no Storage (bucket octano-certs, caminho em oct_empresas.cert_path)
  - a senha CIFRADA em oct_empresas.cert_senha_cifrada (Fernet, CHAVE_MESTRA)
  - csc e csc_id em oct_empresas

O servidor usa a SERVICE KEY do Supabase (variavel de ambiente) para ler o
Storage e a tabela. O cliente (PDV/retaguarda) nunca recebe cert nem senha.
"""

import os
import json
import base64
import urllib.request
import urllib.error

from .cripto import decifrar


def _supabase_conf():
    url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY nao configurados no servidor.")
    return url, key


def _rest_get(path, params=""):
    url, key = _supabase_conf()
    full = f"{url}/rest/v1/{path}{params}"
    req = urllib.request.Request(full)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _storage_download(caminho):
    """Baixa um arquivo do bucket octano-certs e devolve em base64."""
    url, key = _supabase_conf()
    full = f"{url}/storage/v1/object/octano-certs/{caminho}"
    req = urllib.request.Request(full)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return base64.b64encode(r.read()).decode("utf-8")


def carregar_empresa(empresa_id):
    """Retorna dict com dados da empresa + cert_base64 + cert_senha (decifrada).
    Lanca RuntimeError com mensagem clara se algo faltar."""
    rows = _rest_get("oct_empresas", f"?id=eq.{empresa_id}&select=*")
    if not rows:
        raise RuntimeError(f"Empresa {empresa_id} nao encontrada.")
    emp = rows[0]

    cert_path = emp.get("cert_path")
    if not cert_path:
        raise RuntimeError("Empresa sem certificado cadastrado (cert_path vazio).")
    senha_cifrada = emp.get("cert_senha_cifrada")
    if not senha_cifrada:
        raise RuntimeError("Empresa sem senha de certificado cadastrada. Cadastre o certificado no retaguarda.")

    cert_base64 = _storage_download(cert_path)
    cert_senha = decifrar(senha_cifrada)

    return {
        "empresa": emp,
        "cert_base64": cert_base64,
        "cert_senha": cert_senha,
        "csc": emp.get("csc"),
        "csc_id": emp.get("csc_id"),
    }
