FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tfh ./tfh
COPY frontend ./frontend

ENV TFH_DATA_DIR=/data \
    PORT=8080
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "-m", "tfh"]
