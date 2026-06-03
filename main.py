import os
import json
import tempfile
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
CORS(app, origins="*")

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "servico": "Octano SEFAZ", "versao": "1.0.0"})

@app.route("/manifestar", methods=["POST"])
def manifestar():
    """Consulta NF-es emitidas contra o CNPJ na SEFAZ (DistDFe)"""
    try:
        from sefaz.distdfe import consultar_distdfe
        dados = request.get_json()
        cnpj = dados.get("cnpj", "").replace(".", "").replace("/", "").replace("-", "")
        cert_base64 = dados.get("cert_base64")
        cert_senha = dados.get("cert_senha")
        ambiente = dados.get("ambiente", "homologacao")
        ultimo_nsu = dados.get("ultimo_nsu", "0")

        if not cnpj or not cert_base64 or not cert_senha:
            return jsonify({"erro": "cnpj, cert_base64 e cert_senha sao obrigatorios"}), 400

        resultado = consultar_distdfe(cnpj, cert_base64, cert_senha, ambiente, ultimo_nsu)
        return jsonify(resultado)

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/manifestar/ciencia", methods=["POST"])
def manifestar_ciencia():
    """Registra ciência da operação para uma NF-e"""
    try:
        from sefaz.evento import registrar_evento
        dados = request.get_json()
        cnpj = dados.get("cnpj", "").replace(".", "").replace("/", "").replace("-", "")
        chave = dados.get("chave_nfe")
        cert_base64 = dados.get("cert_base64")
        cert_senha = dados.get("cert_senha")
        ambiente = dados.get("ambiente", "homologacao")

        if not all([cnpj, chave, cert_base64, cert_senha]):
            return jsonify({"erro": "Parametros obrigatorios ausentes"}), 400

        resultado = registrar_evento(cnpj, chave, cert_base64, cert_senha, ambiente, tipo="210210")
        return jsonify(resultado)

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/manifestar/confirmacao", methods=["POST"])
def manifestar_confirmacao():
    """Confirma operação de uma NF-e"""
    try:
        from sefaz.evento import registrar_evento
        dados = request.get_json()
        cnpj = dados.get("cnpj", "").replace(".", "").replace("/", "").replace("-", "")
        chave = dados.get("chave_nfe")
        cert_base64 = dados.get("cert_base64")
        cert_senha = dados.get("cert_senha")
        ambiente = dados.get("ambiente", "homologacao")

        if not all([cnpj, chave, cert_base64, cert_senha]):
            return jsonify({"erro": "Parametros obrigatorios ausentes"}), 400

        resultado = registrar_evento(cnpj, chave, cert_base64, cert_senha, ambiente, tipo="210200")
        return jsonify(resultado)

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/xml/<chave>", methods=["POST"])
def baixar_xml(chave):
    """Baixa o XML completo de uma NF-e pelo NSU"""
    try:
        from sefaz.distdfe import baixar_xml_nfe
        dados = request.get_json()
        cnpj = dados.get("cnpj", "").replace(".", "").replace("/", "").replace("-", "")
        cert_base64 = dados.get("cert_base64")
        cert_senha = dados.get("cert_senha")
        ambiente = dados.get("ambiente", "homologacao")
        nsu = dados.get("nsu")

        resultado = baixar_xml_nfe(cnpj, cert_base64, cert_senha, ambiente, nsu)
        return jsonify(resultado)

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/emitir", methods=["POST"])
def emitir():
    """Autoriza uma NF-e modelo 55 na SEFAZ (combustivel monofasico ICMS61)."""
    try:
        from sefaz.emissao import emitir_nfe
        import random
        dados = request.get_json()
        cert_base64 = dados.get("cert_base64")
        cert_senha = dados.get("cert_senha")
        ambiente = dados.get("ambiente", "homologacao")
        nota = dados.get("nota")

        if not all([cert_base64, cert_senha, nota]):
            return jsonify({"erro": "cert_base64, cert_senha e nota sao obrigatorios"}), 400
        if not nota.get("itens"):
            return jsonify({"erro": "nota.itens vazio"}), 400

        # codigo numerico aleatorio (cNF) se o caller nao mandar
        nota.setdefault("cnf", str(random.randint(10000000, 99999999)))

        resultado = emitir_nfe(nota, cert_base64, cert_senha, ambiente)
        codigo = 200 if resultado.get("ok") else 422
        return jsonify(resultado), codigo

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
