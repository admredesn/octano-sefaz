# Octano SEFAZ

Microserviço para comunicação com a SEFAZ Nacional.

## Endpoints

- `GET /` — health check
- `POST /manifestar` — consulta NF-es emitidas contra o CNPJ (DistDFe)
- `POST /manifestar/ciencia` — registra ciência da operação
- `POST /manifestar/confirmacao` — confirma operação
- `POST /xml/{chave}` — baixa XML completo por NSU

## Variáveis de ambiente

- `PORT` — porta do servidor (definida pelo Railway)

## Deploy

Deploy automático pelo Railway a partir deste repositório.
