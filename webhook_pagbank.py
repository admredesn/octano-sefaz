"""
webhook_pagbank.py  -  Endpoint para RECEBER as notificacoes de transacao do
PagBank no Railway (que e publico) e permitir CONSULTAR pelo cmd/navegador.

Como funciona:
  1. O PagBank manda um POST para /webhook/pagbank sempre que uma transacao
     muda de estado (inclui, potencialmente, vendas da maquininha).
  2. Este endpoint guarda o que chegou (em memoria) com data/hora.
  3. Voce consulta pelo cmd:  curl https://SEU-RAILWAY/webhook/pagbank/ultimos
     e ve tudo que o PagBank enviou - sem site de terceiros.

Objetivo do teste: descobrir SE a venda avulsa da maquininha dispara
notificacao. Faca uma venda de R$1,00 e veja se aparece aqui.

COMO ADICIONAR AO RAILWAY (main.py):
  - Cole as rotas abaixo dentro do seu main.py (antes do
    if __name__ == "__main__"), ou importe este arquivo.
  - Faca deploy. A URL a cadastrar no PagBank sera:
        https://octano-sefaz-production-66d4.up.railway.app/webhook/pagbank
"""

from flask import request, jsonify
from datetime import datetime, timezone
from collections import deque

# guarda as ultimas 200 notificacoes recebidas (em memoria; reinicia no deploy)
_WEBHOOK_LOG = deque(maxlen=200)


def registrar_rotas_webhook(app):
    """Chame registrar_rotas_webhook(app) no main.py, passando o app Flask."""

    @app.route("/webhook/pagbank", methods=["POST", "GET"])
    def webhook_pagbank():
        """
        Recebe a notificacao do PagBank. O PagBank envia via POST
        (application/x-www-form-urlencoded) com notificationCode e
        notificationType. Guardamos TUDO que chegar, cru, para inspecao.
        """
        agora = datetime.now(timezone.utc).isoformat()
        try:
            registro = {
                "recebido_em": agora,
                "metodo": request.method,
                "content_type": request.headers.get("Content-Type", ""),
                # form (formato antigo: notificationCode/notificationType)
                "form": request.form.to_dict() if request.form else {},
                # json (formato novo v4, se vier assim)
                "json": (request.get_json(silent=True) or {}),
                # querystring, se houver
                "args": request.args.to_dict() if request.args else {},
                # corpo cru (fallback, caso venha em formato inesperado)
                "raw": request.get_data(as_text=True)[:2000],
                "remote_addr": request.remote_addr,
            }
        except Exception as e:
            registro = {"recebido_em": agora, "erro_parse": str(e),
                        "raw": request.get_data(as_text=True)[:2000]}

        _WEBHOOK_LOG.appendleft(registro)
        # PagBank espera 200 rapido; responde OK sempre
        return jsonify({"ok": True}), 200

    @app.route("/webhook/pagbank/ultimos", methods=["GET"])
    def webhook_pagbank_ultimos():
        """
        Consulta o que o PagBank enviou. Use no cmd:
          curl https://SEU-RAILWAY/webhook/pagbank/ultimos
        ?n=5 limita a quantidade. Vazio = nada foi recebido ainda.
        """
        try:
            n = int(request.args.get("n", 20))
        except ValueError:
            n = 20
        itens = list(_WEBHOOK_LOG)[:n]
        return jsonify({
            "total_recebido_desde_o_deploy": len(_WEBHOOK_LOG),
            "mostrando": len(itens),
            "notificacoes": itens,
        })

    @app.route("/webhook/pagbank/limpar", methods=["POST", "GET"])
    def webhook_pagbank_limpar():
        """Limpa o log (util entre testes)."""
        _WEBHOOK_LOG.clear()
        return jsonify({"ok": True, "mensagem": "log limpo"})
