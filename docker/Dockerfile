# syntax=docker/dockerfile:1
# Build from the repo root:  docker build -f docker/Dockerfile -t ai-server-monitor .

# ---------------------------------------------------------------- frontend build
FROM node:22-alpine AS frontend
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
# `npm install`, not `npm ci`: a lockfile made on Windows/macOS lacks Linux-only optional
# native deps (rolldown/emnapi), which `npm ci`'s strict sync check rejects.
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------- runtime
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ASM__SERVER__STATIC_DIR=/app/static \
    ASM__STORAGE__PATH=/data/monitor.db

WORKDIR /app

# journalctl (from `systemd`) reads the host journal; `apt` (already in the base image)
# reads the host's package state. Neither runs as a service inside the container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends systemd \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cached layer), then the package itself.
COPY backend/pyproject.toml backend/README.md /app/backend/
RUN mkdir -p /app/backend/servermon \
    && touch /app/backend/servermon/__init__.py \
    && pip install /app/backend \
    && pip uninstall -y ai-server-monitor
COPY backend/servermon /app/backend/servermon
RUN pip install --no-deps /app/backend

COPY --from=frontend /src/frontend/dist /app/static
COPY config.example.yaml /app/config.example.yaml

# Runs as root: reading other processes' /proc entries (pid: host), the Docker socket
# and the host journal needs it. All host mounts are expected to be read-only.
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ASM__SERVER__PORT','8080'), timeout=4)"

CMD ["python", "-m", "servermon"]
