# AI Server Monitor — Design (v1)

An AI-powered monitor for a single self-hosted Linux server. It continuously collects
read-only information about the host (resources, hardware, Docker, updates, logs,
Prometheus), has an LLM (any OpenAI-compatible API) write a status report every N
minutes, streams that report to a web UI and Telegram, answers questions in chat, and
can optionally automate the *arr stack (Radarr, Sonarr, Prowlarr, Whisparr) and
qBittorrent under per-action policy.

## Goals and non-goals

**Goals**
- Works with *any* OpenAI-compatible endpoint, including small local models (7–14B)
  with weak or no tool-calling, as well as strong cloud models.
- Strictly read-only towards the host. The only write access is to *arr/qBit APIs,
  and every write is policy-gated and audited.
- Single container, simple config, no external database.
- Useful output: what is happening, what is eating resources, health, pending updates,
  trends — not a metrics dump.

**Non-goals (v1)**
- Multi-host monitoring (hub + agents). Keep collector interfaces clean so it can be
  added later.
- Remediation on the host itself (restarting containers, installing updates, etc.).
- Built-in authentication — a reverse proxy (Authelia/Authentik/etc.) is expected in front.
- SMART / disk-health monitoring.

## Decisions

| Area | Decision |
|---|---|
| Language | Python 3.12+, asyncio, FastAPI |
| Frontend | React + Vite SPA, built in a Docker build stage, served by FastAPI |
| Deployment | One Docker container on a Debian/Ubuntu host |
| LLM | OpenAI-compatible API (`base_url`, `api_key`, `model`), streaming |
| Storage | SQLite (WAL), configurable retention (default 30 days), permanent action audit log |
| Report cadence | Full LLM report every N minutes (default 5, configurable) |
| Outputs | Web UI (live via SSE), Telegram bot, Prometheus `/metrics` |
| Chat | UI + Telegram, with tool-calling |
| Auth | None built in; reverse proxy handles it |
| Repo / CI | GitHub, GitHub Actions publishes the image |

## Architecture

```
                ┌─────────────────────────── container ───────────────────────────┐
 /proc,/sys ──► │ Collectors ──► Snapshot store (SQLite) ──► Snapshot digester     │
 docker.sock ─► │   system, docker, apt,          ▲               │                │
 journal ─────► │   gpu, systemd/journal,         │               ▼                │
 apt state ───► │   prometheus, image-updates     │         Report engine ──► LLM  │
 Prometheus ──► │                                 │               │  (stream)      │
                │ *arr / qBit clients ◄── Action layer (policy, limits, audit)     │
                │        ▲                        ▲               │                │
                │        └──── Tool registry ─────┴── Chat engine ┤                │
                │                                                 ▼                │
                │            Event bus ──► SSE (web UI) / Telegram / /metrics      │
                └──────────────────────────────────────────────────────────────────┘
```

### Components

- **Scheduler** — asyncio tasks. Each collector has its own interval (fast ones like CPU
  every 15–30s, slow ones like apt/image updates every few hours). The report job runs
  every `report.interval` (default 5m).
- **Collectors** — implement a common interface `async collect() -> CollectorResult`
  (typed data + collection errors). A failing collector never breaks a report; its
  error is included so the LLM can mention it.
- **Snapshot store** — stores raw collector results and periodic compact snapshots in
  SQLite for trends (e.g. "disk grew 40 GB this week", "container X restarted 6 times
  today").
- **Snapshot digester** — the key to supporting weak models. Turns raw data into a
  compact, pre-analyzed text/JSON digest *before* the LLM sees it:
  top-N processes and containers by CPU/RAM/IO, deltas since the last report,
  threshold breaches, rule-based anomaly flags, trend summaries, pending updates,
  failed units, log error bursts, *arr queue/health summary. Has a token budget and
  truncates by priority.
- **Report engine** — one streamed chat-completion call per interval with a fixed
  system prompt + digest + the previous report's summary (for continuity). No
  tool-calling required. Output: Markdown report with a structured header
  (overall health `ok|warn|crit`, health score 0–100, key findings) parsed from a
  small JSON block, with a fallback if the model gets the format wrong.
  Optional setting `skip_if_unchanged` (default off) to avoid paying for identical
  reports.
- **Chat engine** — conversational loop over the tool registry.
  - Native OpenAI tool-calling when the model supports it.
  - Fallback mode for models without tool-calling: JSON-in-text protocol
    (`{"tool": ..., "args": ...}`) parsed and validated by us.
  - Mode is auto-detected at startup with a probe, overridable in config.
- **Tool registry** — single source of truth for everything the LLM can call: read
  tools (query metrics, list containers, read logs, PromQL query, *arr search/lookup)
  and write tools (*arr/qBit actions). Each tool has a JSON schema, a description, and
  a `kind: read|write`. Write tools always go through the action layer.
- **Action layer** — every *arr/qBit write passes through here:
  - Per-action policy: `off | suggest | approve | auto`.
  - Rate limits / caps (e.g. max N torrent removals per hour, max N searches per run).
  - Global `dry_run` switch.
  - Pending approvals are stored and surfaced as buttons in the UI and Telegram
    inline keyboards; they expire after a configurable time.
  - Everything (suggested, approved, rejected, executed, failed) is written to the
    audit log.
- **Event bus** — in-process pub/sub. Report tokens, new findings, pending approvals,
  and action results are published once and fanned out to SSE, Telegram, and metrics.
- **Outputs**
  - **Web UI** — dashboard (live metrics, health, top consumers), live report stream,
    report history, chat, approvals queue, action audit log, *arr overview, settings view.
  - **Telegram** — report every interval (or configurable: every Nth / only on status
    change), alerts, chat, approval inline buttons. Restricted to an allowlist of chat IDs.
  - **Prometheus `/metrics`** — health score, finding counts by severity, collector
    success/latency, LLM latency/tokens/errors, *arr queue sizes, stalled downloads,
    actions by type/result, pending approvals.

## Data sources

All host access is read-only.

| Source | How | Container requirements |
|---|---|---|
| CPU, memory, swap, load, disks, filesystems, network, processes | `psutil` against host `/proc`, `/sys` | `pid: host`, mount `/proc` and `/sys` ro, `HOST_PROC`/`HOST_SYS` env; host root at `/host` ro for filesystem usage |
| Docker | Docker SDK: containers, stats, restarts, health, images | `/var/run/docker.sock` ro (consider a socket proxy that only allows GET) |
| Container image updates | Compare local image digests with registry manifest digests (Docker Hub, GHCR, etc.), cached | Network access to registries |
| apt updates | Parse `/host/var/lib/apt/lists` + `/host/var/lib/dpkg/status` and compute upgradable packages (python-apt or `apt` with `-o Dir=/host`); flag security updates and `reboot-required` | `/var/lib/apt`, `/var/lib/dpkg`, `/etc/apt`, `/var/run/reboot-required*` ro |
| NVIDIA GPU | NVML (`pynvml`): utilization, VRAM, temperature, power, per-process use | NVIDIA container runtime, `gpus: all` (optional) |
| systemd units | D-Bus to host systemd (failed units, recent restarts) | `/run/dbus/system_bus_socket` ro, or `systemctl --root` fallback |
| Journal / logs | `journalctl -D /host/var/log/journal` (error/crit bursts, top offenders); container logs via Docker API | `/var/log/journal` ro, `/etc/machine-id` ro |
| Prometheus | PromQL HTTP API, configured queries plus a free-form `promql_query` chat tool | Network access |
| *arr / qBit | Radarr/Sonarr/Whisparr v3 API, Prowlarr v1 API, qBittorrent WebUI API v2 | URLs + API keys / credentials |

Every collector can be disabled in config, and missing mounts or devices degrade
gracefully (the collector reports "unavailable").

## *arr / qBittorrent automation (v1)

| Automation | Description | Default policy |
|---|---|---|
| Stalled/failed cleanup | Find torrents that are stalled (no progress for X h, no seeds, `metaDL` stuck, error state) or *arr queue items with import failures; remove them from the client, blocklist the release in the owning *arr, trigger a re-search | `approve` |
| Missing / cutoff search | Periodically trigger searches for missing items and cutoff-unmet upgrades in small batches (rate-limited so indexers aren't hammered) | `suggest` |
| Add via chat | "Add Dune Part Two in 4K" → lookup → candidate list → choose quality profile / root folder → confirm → add + search | `approve` |
| Health / indexer triage | Read *arr health checks and Prowlarr indexer status; explain issues in reports; suggest/apply fixes such as testing or disabling a failing indexer | `suggest` |

Each automation is a write tool in the registry, so the same actions are available to
the scheduler (rule-driven), the report engine (as suggestions), and chat.

## Configuration

A single `config.yaml` (mounted at `/config/config.yaml`), every value overridable by
environment variables (`ASM__SECTION__KEY`), and secrets can come from env or files.
Sketch:

```yaml
llm:
  base_url: http://ollama:11434/v1
  api_key: ${LLM_API_KEY}
  model: qwen2.5:14b
  tool_mode: auto          # auto | native | json | none
  max_context_tokens: 8192
  temperature: 0.2

report:
  interval: 5m
  skip_if_unchanged: false
  language: en

storage:
  path: /data/monitor.db
  retention: 30d

collectors:
  system:        { enabled: true, interval: 15s }
  docker:        { enabled: true, interval: 30s }
  gpu:           { enabled: auto, interval: 30s }
  systemd:       { enabled: true, interval: 1m }
  journal:       { enabled: true, interval: 1m, priority: warning }
  apt:           { enabled: true, interval: 6h }
  image_updates: { enabled: true, interval: 12h }
  prometheus:
    enabled: false
    url: http://prometheus:9090
    queries: {}            # name: promql

outputs:
  telegram:
    enabled: false
    token: ${TELEGRAM_TOKEN}
    allowed_chat_ids: []
    report_every: 1        # send every Nth report
  metrics: { enabled: true }

arr:
  dry_run: true
  radarr:   [{ name: radarr, url: http://radarr:7878, api_key: ${RADARR_KEY} }]
  sonarr:   [{ name: sonarr, url: http://sonarr:8989, api_key: ${SONARR_KEY} }]
  whisparr: []
  prowlarr: { url: http://prowlarr:9696, api_key: ${PROWLARR_KEY} }
  qbittorrent: { url: http://qbittorrent:8080, username: admin, password: ${QBIT_PASS} }
  actions:
    stalled_cleanup: { policy: approve, stalled_after: 6h, max_per_hour: 10 }
    missing_search:  { policy: suggest, interval: 6h, batch: 10 }
    cutoff_search:   { policy: suggest, interval: 24h, batch: 5 }
    add_media:       { policy: approve }
    indexer_triage:  { policy: suggest }
```

## Storage schema (SQLite)

- `collector_samples(ts, collector, data_json)` — raw/compact samples, pruned by retention.
- `reports(id, ts, health, score, findings_json, markdown, model, tokens_in, tokens_out, latency_ms)`.
- `chat_sessions`, `chat_messages` — chat history.
- `actions(id, ts, action, target, args_json, policy, status, source, result_json, decided_by)` —
  permanent audit log; `status` ∈ suggested | pending | approved | rejected | expired | executed | failed | dry_run.

## API (backend ↔ SPA)

- `GET /api/status` — current health + latest snapshot summary.
- `GET /api/stream` — SSE: report tokens, findings, approvals, action results, metric ticks.
- `GET /api/reports`, `GET /api/reports/{id}`; `POST /api/reports/run` (run now).
- `POST /api/chat` (SSE response), `GET /api/chat/sessions`.
- `GET /api/actions`, `GET /api/approvals`, `POST /api/approvals/{id}` (approve/reject).
- `GET /api/arr/overview`.
- `GET /api/config` (secrets redacted).
- `GET /metrics`, `GET /healthz`.

## Security notes

- Host mounts are read-only; the Docker socket should go through a GET-only socket
  proxy where possible (documented in the compose example).
- Secrets are never sent to the LLM; tool outputs are scrubbed of API keys/passwords.
- Log lines and *arr data are untrusted input for the LLM: they are clearly delimited
  in prompts, and no write action can be executed solely because of text found in
  data — writes always go through the policy layer.
- Telegram restricted to allowlisted chat IDs. No built-in web auth: the docs require a
  reverse proxy with auth if the UI is exposed beyond the LAN.

## Project layout

```
backend/
  servermon/           # import package (dist name: ai-server-monitor)
    __main__.py        # entry point: python -m servermon
    main.py            # FastAPI app factory
    context.py         # AppContext: owns db, scheduler, llm; registers jobs
    config.py          # pydantic-settings models, YAML + ${VAR} + ASM__ env overrides
    db.py              # SQLite access, migrations (PRAGMA user_version), retention, kv
    migrations/        # NNNN_name.sql
    scheduler.py       # asyncio interval scheduler (trigger, timeouts, status)
    llm/               # httpx-based OpenAI-compatible client (not the OpenAI SDK, to
                       # handle local-server quirks), tool-mode probe, JSON tool fallback
    collectors/        # system, docker, apt, gpu, systemd, journal, prometheus, image_updates
    digest/            # snapshot digester, anomaly rules, trends
    report/            # report engine, prompts, output parsing
    chat/              # chat engine
    tools/             # tool registry + read tools
    arr/               # API clients (radarr, sonarr, whisparr, prowlarr, qbit) + automations
    actions/           # policy, limits, approvals, audit
    outputs/           # sse, telegram, metrics
  tests/
frontend/              # React + Vite SPA
docker/                # Dockerfile, compose example
docs/
```

## Milestones

1. **Foundation** — repo, config, SQLite + migrations, scheduler, LLM client (streaming, tool-mode probe), Dockerfile.
2. **Host collectors** — system, Docker, apt, GPU, systemd, journal.
3. **External collectors** — Prometheus, container image updates.
4. **Digest + report engine** — digester, anomaly rules, trends, report prompt/parsing, history.
5. **Event bus + API + SSE**.
6. **Web UI** — dashboard, live report, history, chat, approvals, audit log.
7. **Telegram bot** — reports, alerts, chat, approval buttons.
8. **Prometheus `/metrics` exporter**.
9. ***arr / qBit clients + action layer** — clients, policy, limits, approvals, audit.
10. ***arr automations** — stalled cleanup, missing/cutoff search, add via chat, indexer triage.
11. **Chat tool-calling** — tool registry, native + JSON fallback modes.
12. **Packaging & docs** — compose example, README, GitHub Actions image publish.
