"""
sefaz/cripto.py  -  Criptografia simetrica das senhas de certificado.

Cada empresa tem sua senha de certificado A1 guardada CIFRADA no banco
(oct_empresas.cert_senha_cifrada). A cifragem usa Fernet (cryptography) com
uma CHAVE_MESTRA unica do servidor (variavel de ambiente CHAVE_MESTRA).

- A senha em texto so trafega no momento do cadastro (rota /cadastrar-cert).
- Na emissao, o servidor decifra internamente; o cliente nunca ve a senha.
- Quem tiver a CHAVE_MESTRA consegue decifrar — por isso ela fica so no
  servidor (variavel de ambiente), nunca no cliente nem no banco.
"""

import os
from cryptography.fernet import Fernet, InvalidToken


def _fernet():
    chave = os.environ.get("CHAVE_MESTRA", "").strip()
    if not chave:
        raise RuntimeError("CHAVE_MESTRA nao configurada no servidor.")
    try:
        return Fernet(chave.encode("utf-8"))
    except Exception as e:
        raise RuntimeError("CHAVE_MESTRA invalida (deve ser uma chave Fernet base64 de 44 chars): " + str(e))


def cifrar(senha_texto: str) -> str:
    """Cifra a senha em texto e devolve o token (str) para gravar no banco."""
    f = _fernet()
    return f.encrypt((senha_texto or "").encode("utf-8")).decode("utf-8")


def decifrar(token_cifrado: str) -> str:
    """Decifra o token guardado no banco e devolve a senha em texto."""
    f = _fernet()
    try:
        return f.decrypt((token_cifrado or "").encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError("Falha ao decifrar a senha do certificado (token invalido ou CHAVE_MESTRA trocada).")


def gerar_chave_mestra() -> str:
    """Utilitario: gera uma nova CHAVE_MESTRA (use uma vez, guarde no servidor)."""
    return Fernet.generate_key().decode("utf-8")
