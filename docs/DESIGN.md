# AI Server Monitor — Design (v1)

An AI-powered monitor for a single self-hosted Linux server. An LLM (any OpenAI-compatible API
with native tool calling) answers questions in chat by looking at the host through a read-only
shell, and can call tools of external MCP servers under per-action policy.

## Goals and non-goals

**Goals**
- Works with any OpenAI-compatible endpoint that supports native tool calling, small local
  models as well as strong cloud models.
- Strictly read-only towards the host. The only write access is MCP tool calls,
  and every write is policy-gated and audited.
- Single container, simple config, no external database.

**Non-goals (v1)**
- Multi-host monitoring (hub + agents).
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
| Outputs | Web UI (live via SSE), Telegram bot, Prometheus `/metrics` |
| Chat | UI + Telegram, with native tool calling |
| Configuration | Web UI only (stored in SQLite, applied live); compose sets only mounts, data dir, port, Docker proxy |
| Auth | None built in; reverse proxy handles it |
| Repo / CI | GitHub, GitHub Actions publishes `ghcr.io/drumslave-git/ai-server-monitor` (amd64) on version bumps |

## Architecture

See `docs/handover-agent-runtime.md` for the agent runtime.

### Components

- **Chat engine** — conversational loop over the tool registry, with native OpenAI
  tool calling.
- **Tool registry** — single source of truth for everything the LLM can call, each with a
  JSON schema, a description, and a `kind: read|write`. Write tools (MCP tool calls)
  always go through the action layer.
- **Action layer** — every write passes through here:
  - Per-action policy: `off | suggest | approve | auto`.
  - Rate limits / caps (e.g. max N calls per hour).
  - Pending approvals are stored and surfaced as buttons in the UI and Telegram
    inline keyboards; they expire after a configurable time.
  - Everything (suggested, approved, rejected, executed, failed) is written to the
    audit log.
- **Event bus** — in-process pub/sub. Pending approvals and action results are published
  once and fanned out to SSE, Telegram, and metrics.
- **Outputs**
  - **Web UI** — chat, approvals queue, action audit log, logs, settings view.
  - **Telegram** — chat, approval inline buttons. Restricted to an allowlist of chat IDs.
  - **Prometheus `/metrics`** — LLM latency/tokens/errors, actions by type/result,
    pending approvals.

## Configuration

Everything is configured in the web UI (Settings page), applied live without a restart.
There are no config files.

- **Settings** (`config.Settings`, a pydantic model) are stored as one JSON document in the
  SQLite `kv` table. `GET /api/settings` returns the values (secrets masked as `********`),
  the JSON schema and section titles; the UI renders the form from the schema, so a new
  backend setting appears in the UI automatically, with its docstring as help text.
  `PUT /api/settings` validates (422 with per-field messages), saves, and rebuilds the
  scheduler and LLM client in place. Sending a masked secret back keeps the
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

- `chat_sessions`, `chat_messages` — chat history.
- `actions(id, ts, action, target, args_json, policy, status, source, result_json, decided_by)` —
  permanent audit log; `status` ∈ suggested | pending | approved | rejected | expired | executed | failed | dry_run.

## API (backend ↔ SPA)

- `GET /api/status` — version, uptime, model names, pending approvals and automation state.
- `GET /api/stream` — SSE over the in-process event bus; `?types=approval,action` filters by
  prefix. Events: `action.suggested|result`, `approval.pending|decided|expired` (action layer, compact payloads).
  Every frame has an `id:`; `retry: 3000` is sent first; a `: keepalive` comment every 15 s;
  each client has a bounded queue (a slow client loses its oldest events, publishers never
  wait). There is no replay on reconnect: clients re-fetch state via `/api/status`. The action
  layer (M9) should publish `approval.pending`, `approval.decided`, `action.result`.
- `POST /api/chat` (SSE response), `GET /api/chat/sessions`.
- `GET /api/actions`, `GET /api/approvals`, `POST /api/approvals/{id}` (approve/reject).
- There is no separate `/api/config`: `GET /api/settings` already returns the settings with secrets masked.
- `GET /metrics` (Prometheus text format, hand-rolled in `outputs/metrics.py`; on/off and an
  optional scrape bearer token in Settings → Outputs → metrics), `GET /healthz`. Series (prefix
  `asm_`): `llm_request_duration_seconds` (histogram), `llm_requests_total{result}`,
  `llm_tokens_total{direction}`, `llm_errors_total{type}`, `build_info{version}`, and for the
  action layer: `actions_total{action,result}`, `pending_approvals`, `pending_approval_batches`. Other modules
  add series with `MetricsHub.add_provider` (the action layer lists its names in `metrics.py`).

## Security notes

- Host mounts are read-only; the Docker socket should go through a GET-only socket
  proxy where possible (documented in the compose example).
- Secrets are never sent to the LLM; tool outputs are scrubbed of API keys/passwords.
- Log lines and tool results are untrusted input for the LLM: they are clearly delimited
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
    chat/              # chat engine
    tools/             # tool registry + read tools
    actions/           # policy, limits, approvals, audit
    outputs/           # sse, telegram, metrics
  tests/
frontend/              # React + Vite SPA (schema-driven settings form, overview)
                       #   src/api-types.ts is generated: `npm run gen:api` (backend OpenAPI schema
                       #   -> openapi-typescript; tools/api-gen pins TypeScript 5 for the generator)
docker/                # Dockerfile, compose example
docs/
```

## Milestones

1. **Foundation** — repo, config, SQLite + migrations, scheduler, LLM client, Dockerfile.
2. **Event bus + API + SSE**.
3. **Web UI** — chat, approvals, audit log.
4. **Telegram bot** — chat, approval buttons.
5. **Prometheus `/metrics` exporter**.
6. **Action layer** — policy, limits, approvals, audit.
7. **Chat tool-calling** — tool registry.
8. **Packaging & docs** — compose example, README, GitHub Actions image publish.
