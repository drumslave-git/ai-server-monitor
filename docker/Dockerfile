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

# Git commit this image was built from (shown next to the version in the UI).
ARG BUILD_COMMIT=unknown
ENV ASM_BUILD_COMMIT=$BUILD_COMMIT

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ASM_STATIC_DIR=/app/static

WORKDIR /app

# Install dependencies first (cached layer), then the package itself.
COPY backend/pyproject.toml backend/README.md /app/backend/
RUN mkdir -p /app/backend/ai_server_monitor \
    && touch /app/backend/ai_server_monitor/__init__.py \
    && pip install /app/backend \
    && pip uninstall -y ai-server-monitor
COPY backend/ai_server_monitor /app/backend/ai_server_monitor
RUN pip install --no-deps /app/backend

COPY --from=frontend /src/frontend/dist /app/static

# Runs as root: the shell tool enters the host filesystem with chroot and drops to an
# unprivileged user there (see agent/shell.py). All host mounts are expected to be read-only. Settings and history live in
# /data (SQLite); everything else is configured in the web UI.
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ASM_PORT','8080'), timeout=4)"

CMD ["python", "-m", "ai_server_monitor"]
