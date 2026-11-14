# AI Server Monitor

AI-powered, read-only monitor for a self-hosted Linux server. It collects host data
(CPU, memory, disks, network, processes, Docker, apt updates, NVIDIA GPU, systemd,
journal, Prometheus), has an LLM behind any OpenAI-compatible API write a status report
every few minutes, streams it to a web UI and Telegram, and can optionally automate the
*arr stack (Radarr, Sonarr, Prowlarr, Whisparr) and qBittorrent under per-action policy.

Status: early development. See [docs/DESIGN.md](docs/DESIGN.md) for the design and milestones.

## Install (Debian/Ubuntu host with Docker)

```bash
mkdir -p ~/ai-server-monitor && cd ~/ai-server-monitor
curl -fsSL https://raw.githubusercontent.com/drumslave-git/ai-server-monitor/main/docker/compose.example.yaml -o compose.yaml
docker compose up -d
```

Open `http://<server>:8080` and configure everything under **Settings** (LLM endpoint,
collectors, Telegram, *arr…). Changes apply immediately; there are no config files.

The compose file only sets what the container needs to run:

- `./data` — settings and history (SQLite). Back this up; it contains your API keys.
- `/:/host:ro,rslave` and `pid: host` — read-only view of the host.
- `DOCKER_HOST` → a socket proxy that only allows read (GET) requests.
- `# gpus: all` — uncomment for NVIDIA GPU monitoring.

There is no built-in login: keep port 8080 on your LAN or put a reverse proxy with
authentication in front of it.

Update: `docker compose pull && docker compose up -d`. Pin a version by changing the image
tag (`ghcr.io/drumslave-git/ai-server-monitor:0.3.0`; tags `X.Y.Z`, `X.Y`, `latest`, amd64).
If the GHCR package is private, `docker login ghcr.io` with a token that has `read:packages`.

## Troubleshooting

Every collector's status and error is on the **Overview** page; a collector that can't
work on your host never stops the service. Disable it under Settings → Collectors if you
don't need it.

- **systemd: "D-Bus … closed the connection"** — the host's system bus dropped the
  container's connection. On Ubuntu this is usually D-Bus AppArmor mediation blocking the
  container's `docker-default` AppArmor profile. Check with
  `journalctl -b | grep -iE 'apparmor="DENIED".*dbus'`.

## Development

```bash
cd backend && python -m venv .venv && .venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
.venv/bin/pytest && .venv/bin/ruff check . && .venv/bin/ruff format --check .
ASM_DATA_DIR=./data .venv/bin/python -m ai_server_monitor                 # API on :8080
cd ../frontend && npm install && npm run dev                                # UI on :5173, proxies /api
```

Local image: `docker build -f docker/Dockerfile -t ai-server-monitor:dev .`

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
