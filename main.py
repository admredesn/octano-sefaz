import os
import json
import tempfile
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

from webhook_pagbank import registrar_rotas_webhook

load_dotenv()

app = Flask(__name__)
CORS(app, origins="*")

# recebe as notificacoes de transacao do PagBank (teste da maquininha)
registrar_rotas_webhook(app)

@app.route("/", methods=["GET"])
def health():
    # 'build' = marcador p/ confirmar QUAL versao o Railway esta rodando (verificacao de deploy)
    return jsonify({"status": "ok", "servico": "Octano SEFAZ", "versao": "1.0.0",
                    "build": "2026-07-21-dfe-auto",
                    "dfe_auto": os.environ.get("DFE_AUTO", "").strip().lower() in ("1", "true", "sim", "on")})

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

@app.route("/cancelar-nfce", methods=["POST"])
def cancelar_nfce_rota():
    """Cancela uma NFC-e (modelo 65) autorizada via evento 110111 na SEFAZ-MG."""
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

        resultado = cancelar_nfe(chave, protocolo, justificativa, cnpj, cert_base64, cert_senha, ambiente, modelo="65")
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

@app.route("/cadastrar-cert", methods=["POST"])
def cadastrar_cert():
    """Cifra a senha do certificado de uma empresa e grava no banco.
    Recebe {empresa_id, senha}. Usado no cadastro (retaguarda). Unica rota que
    recebe a senha em texto, e so o gerente a usa ao cadastrar o certificado."""
    try:
        from sefaz.cripto import cifrar
        import urllib.request, urllib.error
        dados = request.get_json()
        empresa_id = dados.get("empresa_id")
        senha = dados.get("senha")
        if not empresa_id or senha is None:
            return jsonify({"erro": "empresa_id e senha sao obrigatorios"}), 400

        senha_cifrada = cifrar(senha)

        # grava via REST (service key)
        url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
        if not url or not key:
            return jsonify({"erro": "SUPABASE_URL / SUPABASE_SERVICE_KEY nao configurados"}), 500
        body = json.dumps({"cert_senha_cifrada": senha_cifrada}).encode()
        req = urllib.request.Request(
            f"{url}/rest/v1/oct_empresas?id=eq.{empresa_id}",
            data=body, method="PATCH",
        )
        req.add_header("apikey", key)
        req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Prefer", "return=minimal")
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = r.status in (200, 204)
        return jsonify({"ok": ok}), (200 if ok else 422)
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/emitir-nfce-empresa", methods=["POST"])
def emitir_nfce_empresa():
    """Emite NFC-e usando o certificado/senha guardados no servidor (por empresa_id).
    O cliente (PDV/retaguarda) NAO envia cert nem senha - so empresa_id + nota + empresa."""
    try:
        from sefaz.nfce import emitir_nfce
        from sefaz.empresa_cert import carregar_empresa
        import random
        dados = request.get_json()
        empresa_id = dados.get("empresa_id")
        ambiente = dados.get("ambiente", "homologacao")
        nota = dados.get("nota")
        empresa = dados.get("empresa")
        if not empresa_id or not nota or not empresa:
            return jsonify({"erro": "empresa_id, nota e empresa sao obrigatorios"}), 400
        if not nota.get("itens"):
            return jsonify({"erro": "nota.itens vazio"}), 400

        ctx = carregar_empresa(empresa_id)
        csc = ctx.get("csc"); csc_id = ctx.get("csc_id")
        if not csc or not csc_id:
            return jsonify({"erro": "csc e csc_id nao cadastrados para a empresa"}), 422

        nota.setdefault("cnf", str(random.randint(10000000, 99999999)))
        resultado = emitir_nfce(nota, empresa, ctx["cert_base64"], ctx["cert_senha"], csc, csc_id, ambiente)
        codigo = 200 if resultado.get("ok") else 422
        return jsonify(resultado), codigo
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/cancelar-nfce-empresa", methods=["POST"])
def cancelar_nfce_empresa():
    """Cancela NFC-e (evento 110111) usando cert/senha do servidor (por empresa_id)."""
    try:
        from sefaz.cancelamento import cancelar_nfe
        from sefaz.empresa_cert import carregar_empresa
        dados = request.get_json()
        empresa_id = dados.get("empresa_id")
        chave = dados.get("chave")
        protocolo = dados.get("protocolo")
        justificativa = dados.get("justificativa")
        ambiente = dados.get("ambiente", "homologacao")
        if not all([empresa_id, chave, protocolo, justificativa]):
            return jsonify({"erro": "empresa_id, chave, protocolo e justificativa sao obrigatorios"}), 400

        ctx = carregar_empresa(empresa_id)
        cnpj = (ctx["empresa"].get("cnpj") or "").replace(".", "").replace("/", "").replace("-", "")
        resultado = cancelar_nfe(chave, protocolo, justificativa, cnpj,
                                 ctx["cert_base64"], ctx["cert_senha"], ambiente, modelo="65")
        codigo = 200 if resultado.get("ok") else 422
        return jsonify(resultado), codigo
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/consultar-nfce-empresa", methods=["POST"])
def consultar_nfce_empresa():
    """Consulta a situacao ATUAL de uma NFC-e na SEFAZ (autorizada/cancelada + eventos).
    Read-only. Body: { empresa_id, chave, ambiente }."""
    try:
        from sefaz.nfce import consultar_situacao_nfce
        from sefaz.empresa_cert import carregar_empresa
        dados = request.get_json() or {}
        empresa_id = dados.get("empresa_id")
        chave = dados.get("chave")
        ambiente = dados.get("ambiente", "homologacao")
        if not empresa_id or not chave:
            return jsonify({"erro": "empresa_id e chave sao obrigatorios"}), 400
        ctx = carregar_empresa(empresa_id)
        resultado = consultar_situacao_nfce(chave, ctx["cert_base64"], ctx["cert_senha"], ambiente)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({"ok": False, "erro": str(e)}), 500


@app.route("/status-servico-nfce-empresa", methods=["POST"])
def status_servico_nfce_empresa():
    """Consulta o status do servico da SEFAZ-MG (NFC-e) usando o cert da empresa.
    Usado pelo nucleo para decidir se da para sair de contingencia e retransmitir.
    Retorna {ok, online, cstat, xmotivo}. online=True quando cStat=107."""
    try:
        from sefaz.nfce import status_servico_nfce
        from sefaz.empresa_cert import carregar_empresa
        dados = request.get_json() or {}
        empresa_id = dados.get("empresa_id")
        ambiente = dados.get("ambiente", "homologacao")
        uf = dados.get("uf", "MG")
        if not empresa_id:
            return jsonify({"erro": "empresa_id e obrigatorio"}), 400
        ctx = carregar_empresa(empresa_id)
        resultado = status_servico_nfce(ctx["cert_base64"], ctx["cert_senha"], ambiente, uf)
        # sempre 200: "offline" e uma resposta valida, nao um erro HTTP
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({"ok": False, "online": False, "erro": str(e)}), 500


@app.route("/transmitir-contingencia", methods=["POST"])
def transmitir_contingencia():
    """Retransmite a SEFAZ um XML de NFC-e JA ASSINADO (emitido antes em
    contingencia tpEmis=9). NAO reassina nem remonta — preserva chave e digest.
    Body: { empresa_id, xml_assinado, ambiente }.
    Retorna {ok, cstat_nfe, xmotivo, protocolo, nfe_proc, comunicacao_falhou?}.
    O nucleo usa isso para decidir tirar (ou nao) o cupom da fila."""
    try:
        from sefaz.nfce import transmitir_nfce_assinada
        from sefaz.empresa_cert import carregar_empresa
        dados = request.get_json() or {}
        empresa_id = dados.get("empresa_id")
        xml_assinado = dados.get("xml_assinado")
        ambiente = dados.get("ambiente", "homologacao")
        if not empresa_id or not xml_assinado:
            return jsonify({"erro": "empresa_id e xml_assinado sao obrigatorios"}), 400
        ctx = carregar_empresa(empresa_id)
        resultado = transmitir_nfce_assinada(xml_assinado, ctx["cert_base64"], ctx["cert_senha"], ambiente)
        # 200 se autorizou; 202 (aceito, pendente) se a comunicacao falhou e
        # o cupom deve permanecer na fila; 422 para rejeicao real da SEFAZ.
        if resultado.get("ok"):
            codigo = 200
        elif resultado.get("comunicacao_falhou"):
            codigo = 202
        else:
            codigo = 422
        return jsonify(resultado), codigo
    except Exception as e:
        return jsonify({"erro": str(e)}), 500


@app.route("/danfce", methods=["POST"])
def danfce():
    """Gera o DANFCE (cupom 80mm em PDF) da NFC-e a partir do nfeProc autorizado.
    Gerador proprio (a brazilfiscalreport nao suporta DANFCE)."""
    try:
        from flask import Response
        from sefaz.danfce_gen import gerar_danfce_pdf
        dados = request.get_json()
        xml = dados.get("xml")
        if not xml:
            return jsonify({"erro": "xml (nfeProc) e obrigatorio"}), 400
        if "infProt" not in xml and "protNFe" not in xml:
            return jsonify({"erro": "XML sem protocolo (protNFe). Envie o nfeProc completo da NFC-e autorizada."}), 400
        pdf_data = gerar_danfce_pdf(xml, extras=dados.get("extras"))
        return Response(
            pdf_data,
            mimetype="application/pdf",
            headers={"Content-Disposition": "attachment; filename=danfce.pdf"},
        )
    except Exception as e:
        return jsonify({"erro": "Falha ao gerar DANFCE: " + str(e)}), 500

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


# ── Agendador da consulta automatica de NF-e (DistDFe) ──────────────────────
# Roda so se DFE_AUTO=1. Iniciado no import (gunicorn --workers 2); o claim
# atomico no banco (oct_dfe_controle) garante 1 consulta por empresa/intervalo.
try:
    from sefaz.dfe_auto import iniciar_agendador
    iniciar_agendador()
except Exception as _e:
    print("[dfe-auto] nao iniciou:", _e)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
