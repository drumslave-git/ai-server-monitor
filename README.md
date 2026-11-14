# AI Server Monitor

AI-powered, read-only monitor for a self-hosted Linux server. It collects host data
(CPU, memory, disks, network, processes, Docker, apt updates, NVIDIA GPU, systemd,
journal, Prometheus), has an LLM behind any OpenAI-compatible API write a status report
every few minutes, streams it to a web UI and Telegram, and can optionally automate the
*arr stack (Radarr, Sonarr, Prowlarr, Whisparr) and qBittorrent under per-action policy.

Status: early development. See [docs/DESIGN.md](docs/DESIGN.md) for the design and milestones.

## Install (Debian/Ubuntu host with Docker)

```bash
mkdir -p ~/servermon && cd ~/servermon
curl -fsSL https://raw.githubusercontent.com/drumslave-git/ai-server-monitor/main/docker/compose.example.yaml -o compose.yaml
curl -fsSL https://raw.githubusercontent.com/drumslave-git/ai-server-monitor/main/config.example.yaml -o config.yaml
# edit config.yaml: at least llm.base_url / llm.model (or set LLM_* in a .env file)
docker compose up -d
```

Then open `http://<server>:8080` (put a reverse proxy with authentication in front of it;
there is no built-in login). `http://<server>:8080/api/collectors` shows the raw data.

- Image: `ghcr.io/drumslave-git/ai-server-monitor` — tags `X.Y.Z`, `X.Y`, `latest` (amd64).
  Pin a version with `SERVERMON_VERSION=0.2.0` in `.env`.
- Host access is read-only: `pid: host`, the host root mounted at `/host:ro`, and Docker
  through a GET-only socket proxy.
- If the GHCR package is private, `docker login ghcr.io` with a token that has `read:packages`.

Update: `docker compose pull && docker compose up -d`.

## Development

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .
ASM_CONFIG=../config.example.yaml .venv/bin/python -m servermon            # API on :8080
cd ../frontend && npm install && npm run dev                                # UI on :5173, proxies /api
```

Local image: `docker build -f docker/Dockerfile -t servermon:dev .`

## Releasing

The version in `backend/pyproject.toml` is the release trigger.

```bash
python scripts/release.py patch     # or minor / major / X.Y.Z — also syncs frontend/package.json
git commit -am "Release vX.Y.Z" && git push
```

On push to `main`, the **Release** workflow notices the version change, runs CI, tags
`vX.Y.Z` and publishes the image to GHCR. No version change → nothing is published.
Re-publish the current version via Actions → Release → *Run workflow*.
**CI** (lint, tests, frontend build) runs on every push and pull request.
