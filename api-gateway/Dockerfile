FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
# gcc solo para compilar lru-dict (dependencia de siwe/web3 sin wheel arm64); se elimina despues
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . .

ENV PORT=3000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
