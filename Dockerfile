FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    libxml2-dev libxslt-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
# 30/09/2026: era --workers 2 SINCRONO = 2 pedidos por vez para os 4 postos + retaguarda.
# Um PDF/DistDFe/consulta lenta segurava a NFC-e na fila (30-60 s no posto). gthread:
# cada processo atende 16 pedidos ao mesmo tempo (I/O de rede solta o GIL).
CMD gunicorn main:app --bind 0.0.0.0:$PORT --workers 2 --threads 16 --worker-class gthread --timeout 300
