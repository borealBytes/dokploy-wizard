FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "dokploy_wizard.packs.openclaw.nexa_runtime_sidecar"]
