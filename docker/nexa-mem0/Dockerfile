FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY src/dokploy_wizard/packs/openclaw/nexa_mem0_sidecar.py /app/nexa_mem0_sidecar.py

RUN python -m pip install --no-cache-dir fastapi uvicorn mem0ai

ENTRYPOINT ["python", "/app/nexa_mem0_sidecar.py"]
