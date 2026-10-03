# Backend API and worker image. Build context: repository root (needs ml/ too).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 1000 cavinet \
    && mkdir -p /data \
    && chown cavinet:cavinet /data

WORKDIR /app

# The shared ML package (preprocessing/inference code, filled in from Phase 5).
COPY ml/ /opt/ml/
RUN pip install /opt/ml

COPY backend/ /app/
RUN pip install /app

USER cavinet
EXPOSE 8000

# The backend port is not published; only nginx reaches it, so its X-Forwarded-For is trusted.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
