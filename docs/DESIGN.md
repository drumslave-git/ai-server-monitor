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
| Configuration | Web UI only (stored in SQLite, applied live); compose sets only mounts, data dir, port, Docker proxy |
| Auth | None built in; reverse proxy handles it |
| Repo / CI | GitHub, GitHub Actions publishes `ghcr.io/drumslave-git/ai-server-monitor` (amd64) on version bumps |

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

All host access is read-only. The container needs just two things for everything below:
`pid: host` and the host root mounted at `/host` read-only (`/:/host:ro,rslave`), plus
Docker API access through a GET-only socket proxy (see `docker/compose.example.yaml`).

| Source | How (as built in M2) |
|---|---|
| CPU, memory, swap, load, disks, network, temps, processes | `psutil` (host `/proc` via `pid: host`). Host mounts from `/proc/1/mounts`, usage via `statvfs` under `/host`; interfaces from `/proc/1/net/dev` (host net namespace); process owners from host `/etc/passwd`; process → container via `/proc/<pid>/cgroup` |
| Docker | Engine API over httpx (unix socket or `tcp://` socket proxy): state, health (running only), restarts + deltas, one-shot stats (CPU% from deltas), log error scan since last run, `/system/df` every 30 min |
| Container image updates | (M3) compare local image digests with registry manifest digests, cached |
| apt updates | Container's `apt list --upgradable -o Dir=/host` with caches + locking disabled; security flag from `-security` suites; `reboot-required(.pkgs)`; age of host apt lists |
| NVIDIA GPU | NVML (`nvidia-ml-py`) in a thread: util, VRAM, temp, power, fan, per-process VRAM. `enabled: auto` (needs NVIDIA runtime, `utility` capability) |
| systemd units | `dbus-fast` → `/host/run/dbus/system_bus_socket`: `ListUnits`, failed units with result + since, `NRestarts` deltas, `SystemState` |
| Journal | `journalctl --directory=/host/var/log/journal -o json -p warning`, continued by cursor: counts by priority/source, grouped normalized messages, notable kernel events (OOM, I/O, FS errors, segfaults, MCE, thermal, GPU Xid) |
| Prometheus | (M3) PromQL HTTP API, configured queries plus a free-form `promql_query` chat tool |
| *arr / qBit | (M9) Radarr/Sonarr/Whisparr v3 API, Prowlarr v1 API, qBittorrent WebUI API v2 |

Collector results are kept in memory (latest per collector, served at
`/api/collectors[/{name}]`) and persisted to `collector_samples` at most every
`storage.sample_interval` (default 5m), using a slimmed `persist_view`.

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

Everything is configured in the web UI (Settings page), applied live without a restart.
There are no config files.

- **Settings** (`config.Settings`, a pydantic model) are stored as one JSON document in the
  SQLite `kv` table. `GET /api/settings` returns the values (secrets masked as `********`),
  the JSON schema and section titles; the UI renders the form from the schema, so a new
  backend setting appears in the UI automatically, with its docstring as help text.
  `PUT /api/settings` validates (422 with per-field messages), saves, and rebuilds the
  scheduler, LLM client and collectors in place. Sending a masked secret back keeps the
  stored value. `POST /api/settings/test-llm` tries unsaved LLM settings.
- **On upgrade**, stored settings are validated leniently: parts that no longer fit fall
  back to defaults (with a logged warning and a backup copy in `kv`) instead of failing.
- **Bootstrap** (`config.Bootstrap`): only what's needed before the app can start, from
  `ASM_*` env vars with defaults matching the image — `ASM_DATA_DIR` (/data),
  `ASM_PORT` (8080), `ASM_HOST_ROOT` (/host). Plus the standard `DOCKER_HOST` for the
  socket proxy. The compose file sets nothing else.
- Secrets are stored in clear text in the SQLite file under the data directory; protect
  that directory like any other credentials store.

## Storage schema (SQLite)

- `collector_samples(ts, collector, data_json)` — raw/compact samples, pruned by retention.
- `reports(id, ts, status, health, score, summary, findings, markdown, digest, digest_hash, model, tokens_in, tokens_out, latency_ms, error, parsed)` —
  `status` ∈ ok | failed | skipped (failed/skipped rows carry the reason in `error`; an identical
  repeat only moves the last row's `ts`); `parsed` = 0 when the model's JSON header could not be
  read and the rule-based fallback was used.
- `chat_sessions`, `chat_messages` — chat history.
- `actions(id, ts, action, target, args_json, policy, status, source, result_json, decided_by)` —
  permanent audit log; `status` ∈ suggested | pending | approved | rejected | expired | executed | failed | dry_run.

## API (backend ↔ SPA)

- `GET /api/status` — dashboard header in one call: latest successful report (health, score,
  summary, findings), the newest failed/skipped attempt, next report time, collector states and
  key resource numbers (typed: `StatusResponse`).
- `GET /api/stream` — SSE over the in-process event bus; `?types=report,metrics` filters by
  prefix. Events: `report.started|token|done|failed`, `metrics.tick` (compact cpu/mem/load/net
  or per-GPU numbers after each system/gpu collect), `collector.status` (ok/failing changes).
  Every frame has an `id:`; `retry: 3000` is sent first; a `: keepalive` comment every 15 s;
  each client has a bounded queue (a slow client loses its oldest events, publishers never
  wait). There is no replay on reconnect: clients re-fetch state via `/api/status`. The action
  layer (M9) should publish `approval.pending`, `approval.decided`, `action.result`.
- `GET /api/reports` (paginated, newest first, no Markdown), `GET /api/reports/{id}`;
  `POST /api/reports/run` (202; 409 if one is running); `GET /api/reports/digest` (the digest
  the next report would be written from, `?format=text` for plain text).
- `POST /api/chat` (SSE response), `GET /api/chat/sessions`.
- `GET /api/actions`, `GET /api/approvals`, `POST /api/approvals/{id}` (approve/reject).
- `GET /api/arr/overview`.
- There is no separate `/api/config`: `GET /api/settings` already returns the settings with secrets masked.
- `GET /metrics` (Prometheus text format, hand-rolled in `outputs/metrics.py`; on/off and an
  optional scrape bearer token in Settings → Outputs → metrics), `GET /healthz`. Series (prefix
  `asm_`): `health_score`, `health_status{status}`, `findings{severity}`,
  `report_last_success_timestamp_seconds`, `report_generation_seconds`, `reports_total{result}`,
  `collector_up|duration_seconds|errors_total|last_success_timestamp_seconds{collector}`,
  `llm_request_duration_seconds` (histogram), `llm_requests_total{result}`,
  `llm_tokens_total{direction}`, `llm_errors_total{type}`, `build_info{version}`. Other modules
  add series with `MetricsHub.add_provider` (the action layer lists its names in `metrics.py`).
  An example Grafana dashboard is in `docs/grafana-dashboard.json`.

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
  ai_server_monitor/   # import package (dist name: ai-server-monitor)
    __main__.py        # entry point: python -m ai_server_monitor
    main.py            # FastAPI app factory + API routes
    context.py         # AppContext: owns db + settings-dependent services, live re-apply
    config.py          # Bootstrap (env) + Settings (GUI) models, durations, secrets
    settings_store.py  # settings persistence, secret merging, lenient upgrade validation
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
    outputs/           # sse (+ live metric ticks); telegram, metrics later
  tests/
frontend/              # React + Vite SPA (schema-driven settings form, overview)
                       #   src/api-types.ts is generated: `npm run gen:api` (backend OpenAPI schema
                       #   -> openapi-typescript; tools/api-gen pins TypeScript 5 for the generator)
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
