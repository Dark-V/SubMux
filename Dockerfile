FROM python:3.13-slim

ARG SUBMUX_VERSION=0.1.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUBMUX_VERSION=${SUBMUX_VERSION} \
    ADMIN_PORT=8080 \
    PUBLIC_PORT=8081 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 --create-home submux \
    && mkdir -p /data \
    && chown -R submux:submux /data /app

COPY --chown=submux:submux app ./app

USER submux

EXPOSE 8080 8081
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('ADMIN_PORT', '8080') + '/healthz', timeout=3)"

CMD ["python", "-m", "app.main"]
