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

@app.route("/cnpj/<cnpj>", methods=["GET"])
def consultar_cnpj(cnpj):
    """Consulta dados publicos de um CNPJ via BrasilAPI (proxy servidor-a-servidor
    para evitar bloqueio de CORS no navegador). Retorna o JSON da BrasilAPI."""
    import requests
    try:
        cnpj_limpo = "".join(ch for ch in (cnpj or "") if ch.isdigit())
        if len(cnpj_limpo) != 14:
            return jsonify({"erro": "CNPJ deve ter 14 digitos"}), 400
        r = requests.get(
            "https://brasilapi.com.br/api/cnpj/v1/" + cnpj_limpo,
            timeout=15,
            headers={"User-Agent": "Octano-Sistemas/1.0"},
        )
        if r.status_code == 404:
            return jsonify({"erro": "CNPJ nao encontrado"}), 404
        if r.status_code != 200:
            return jsonify({"erro": "Falha na consulta", "status": r.status_code}), 502
        return jsonify(r.json())
    except requests.exceptions.Timeout:
        return jsonify({"erro": "Tempo de consulta esgotado"}), 504
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

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

@app.route("/cancelar", methods=["POST"])
def cancelar():
    """Cancela uma NF-e modelo 55 autorizada (evento 110111) na SEFAZ-MG."""
    try:
        from sefaz.cancelamento import cancelar_nfe
        dados = request.get_json()
        chave = dados.get("chave")
        protocolo = dados.get("protocolo")
        justificativa = dados.get("justificativa")
        cnpj = dados.get("cnpj", "")
        cert_base64 = dados.get("cert_base64")
        cert_senha = dados.get("cert_senha")
        ambiente = dados.get("ambiente", "homologacao")

        if not all([chave, protocolo, justificativa, cert_base64, cert_senha]):
            return jsonify({"erro": "chave, protocolo, justificativa, cert_base64 e cert_senha sao obrigatorios"}), 400

        resultado = cancelar_nfe(chave, protocolo, justificativa, cnpj, cert_base64, cert_senha, ambiente)
        codigo = 200 if resultado.get("ok") else 422
        return jsonify(resultado), codigo

    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/emitir-nfce", methods=["POST"])
def emitir_nfce_rota():
    """Autoriza uma NFC-e modelo 65 na SEFAZ-MG (com QR Code via CSC)."""
    try:
        from sefaz.nfce import emitir_nfce
        import random
        dados = request.get_json()
        cert_base64 = dados.get("cert_base64")
        cert_senha = dados.get("cert_senha")
        ambiente = dados.get("ambiente", "homologacao")
        nota = dados.get("nota")
        empresa = dados.get("empresa")
        csc = dados.get("csc")
        csc_id = dados.get("csc_id")

        if not all([cert_base64, cert_senha, nota, empresa]):
            return jsonify({"erro": "cert_base64, cert_senha, nota e empresa sao obrigatorios"}), 400
        if not csc or not csc_id:
            return jsonify({"erro": "csc e csc_id sao obrigatorios para NFC-e"}), 400
        if not nota.get("itens"):
            return jsonify({"erro": "nota.itens vazio"}), 400

        nota.setdefault("cnf", str(random.randint(10000000, 99999999)))
        resultado = emitir_nfce(nota, empresa, cert_base64, cert_senha, csc, csc_id, ambiente)
        codigo = 200 if resultado.get("ok") else 422
        return jsonify(resultado), codigo
    except Exception as e:
        return jsonify({"erro": str(e)}), 500

@app.route("/danfe", methods=["POST"])
def danfe():
    """Gera o DANFE (PDF) a partir do XML completo (nfeProc) da NF-e autorizada.
    Recebe {"xml": "<nfeProc...>"} e devolve o PDF binario."""
    try:
        from flask import Response
        from brazilfiscalreport.danfe import Danfe
        dados = request.get_json()
        xml = dados.get("xml")
        if not xml:
            return jsonify({"erro": "xml (nfeProc) e obrigatorio"}), 400
        # a lib exige o nfeProc (NFe + protNFe). Se vier so a <NFe>, nao gera.
        if "infProt" not in xml and "protNFe" not in xml:
            return jsonify({"erro": "XML sem protocolo (protNFe). Envie o nfeProc completo da nota autorizada."}), 400

        danfe = Danfe(xml=xml)
        # FPDF2: output() sem argumento retorna o bytearray do PDF.
        pdf_data = bytes(danfe.output())

        return Response(
            pdf_data,
            mimetype="application/pdf",
            headers={"Content-Disposition": "attachment; filename=danfe.pdf"},
        )
    except Exception as e:
        return jsonify({"erro": "Falha ao gerar DANFE: " + str(e)}), 500
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
