FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRAFFIC_DB_PATH=/var/lib/foxtrot/traffic/traffic.db
WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 traffic \
    && useradd --uid 10001 --gid traffic --no-create-home traffic \
    && install -d -o traffic -g traffic /var/lib/foxtrot/traffic

COPY __init__.py cloudflare.py contract.py main.py telemetry.py telemetry_contract.py ./server/
USER 10001:10001
EXPOSE 8001
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=2).close()"]
CMD ["sh", "-ec", ": \"${TRAFFIC_SERVICE_TOKEN:?TRAFFIC_SERVICE_TOKEN is required}\"; exec python -m uvicorn server.main:app --host 0.0.0.0 --port 8001 --workers 1"]
