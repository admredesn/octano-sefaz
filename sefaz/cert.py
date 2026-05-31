import base64
import tempfile
import os
from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PrivateFormat, NoEncryption
from cryptography.hazmat.backends import default_backend

def extrair_cert_pem(cert_base64: str, senha: str):
    """Extrai chave privada e certificado PEM do .pfx em base64"""
    pfx_bytes = base64.b64decode(cert_base64)
    senha_bytes = senha.encode("utf-8") if isinstance(senha, str) else senha

    private_key, certificate, additional_certs = pkcs12.load_key_and_certificates(
        pfx_bytes, senha_bytes, default_backend()
    )

    key_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    cert_pem = certificate.public_bytes(Encoding.PEM)

    # Salva em arquivos temporários
    key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".key.pem")
    cert_file = tempfile.NamedTemporaryFile(delete=False, suffix=".cert.pem")

    key_file.write(key_pem)
    key_file.close()
    cert_file.write(cert_pem)
    cert_file.close()

    return cert_file.name, key_file.name

def limpar_arquivos(*arquivos):
    for f in arquivos:
        try:
            os.unlink(f)
        except:
            pass
