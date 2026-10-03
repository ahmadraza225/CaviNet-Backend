# Backend API and worker image. Build context: repository root (needs ml/ too).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 1000 cavinet \
    && mkdir -p /data /models \
    && chown cavinet:cavinet /data /models

WORKDIR /app

# PyTorch for CPU (section 13: no GPU is needed to run CaviNet). The default PyPI wheel for
# x86-64 bundles several GB of GPU libraries; the CPU index has small wheels for x86-64 and
# ARM (Apple Silicon Macs). Override only if that index is unreachable.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --index-url "${TORCH_INDEX_URL}" "torch>=2.3,<3"

# lungmask R231 weights, baked in at build time (section 11.1): no download at runtime.
ENV LUNGMASK_WEIGHTS=/opt/lungmask/unet_r231-d5d2fc3d.pth
RUN mkdir -p /opt/lungmask && python -c "\
import hashlib, urllib.request; \
url = 'https://github.com/JoHof/lungmask/releases/download/v0.0/unet_r231-d5d2fc3d.pth'; \
path = '/opt/lungmask/unet_r231-d5d2fc3d.pth'; \
urllib.request.urlretrieve(url, path); \
digest = hashlib.sha256(open(path, 'rb').read()).hexdigest(); \
assert digest == 'd5d2fc3df432115933491f115b283e94d1f5c841c0e82f6e7bd2d65d3ccde69f', digest"

# The shared AI package (section 11), then the API.
COPY ml/ /opt/ml/
RUN pip install /opt/ml

COPY backend/ /app/
RUN pip install /app

USER cavinet
ENV MODEL_PATH=/models/cavinet_model.pth
EXPOSE 8000

# The backend port is not published; only nginx reaches it, so its X-Forwarded-For is trusted.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
