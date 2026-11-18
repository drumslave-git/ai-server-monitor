# AI Server Monitor — Design (0.7)

One container on the monitored host: a FastAPI backend (`backend/ai_server_monitor/`) and a
React/Vite SPA (`frontend/`, served by the backend). The rule since 0.7: **nothing is collected
by code; a language model does all the monitoring** through a read-only shell, and the code only
schedules, stores, shows and guards. Where a maintained library does the job, it is used
instead of our own code.

## Components

| Part | Code | Library |
|---|---|---|
| Agent loop: tools, step limit, approvals, tracing hooks | `agent/runtime.py` | Pydantic AI |
| Models: main and worker on any OpenAI-compatible endpoint | `agent/models.py` (`ModelHub`) | Pydantic AI `OpenAIChatModel`, `openai`, `httpx` |
| Read-only shell sandbox | `agent/shell.py` | the host's util-linux (`setpriv`, `prlimit`), `chroot` |
| Root shell for approved changes | `agent/change_shell.py` | util-linux `nsenter` |
| Tools: `bash`, `delegate`, `bash_write`, MCP tools | `agent/tools.py`, `mcp_client/` | `mcp` SDK |
| Jobs: store, builtins, prompts, runner, API, chat tools | `jobs/` | — |
| Schedule: jobs + housekeeping, pause | `schedule.py`, `automation.py` | APScheduler 3 |
| Chat engine and sessions | `chat/` | — |
| Approvals and audit (the action layer) | `actions/` | — |
| Telegram bot | `outputs/telegram.py` | python-telegram-bot |
| `/metrics` | `outputs/metrics.py` | prometheus_client |
| Live updates (SSE), event bus | `outputs/sse.py`, `events.py` | FastAPI streaming responses |
| Model call logs, app log | `tracing/` | — |
| Settings (GUI only), SQLite, migrations | `config.py`, `settings_store.py`, `db.py`, `migrations/` | pydantic, aiosqlite |

`context.py` (`AppContext`) owns all of it and rebuilds the settings-dependent services when
settings are saved.

## The agent loop

`run_agent(cfg, prompt, history=…)` runs one Pydantic AI agent and yields our own events
(`token`, `tool_call`, `tool_progress`, `tool_result`, `end`). `AgentConfig` carries the model,
instructions, `AgentTool`s (name, JSON schema, async handler, `requires_approval` or a per-call
`approval_for`), `max_steps`, tool timeout, the result size cap, the secret scrubber, the trace
context and an optional structured `output_type`.

- Step limit: a `PrepareTools` capability hides the tools past `max_steps` and adds a note to
  the instructions, so the model answers with what it has; `UsageLimits` is the backstop.
- Tool results are scrubbed of secrets, capped (`chat.max_tool_result_chars`) and wrapped as
  untrusted data. Failures come back to the model as text; they never end the run.
- Every model request goes through our `httpx` transport into `llm_calls` (Logs page), and every
  tool call into `llm_tool_calls`; sub-agent calls are tagged `worker:…` in the same turn.
- Tool sets: **chat** (main model) gets `bash`, `delegate`, the job tools, `bash_write` (when
  offered) and the MCP tools. **`delegate`** runs the worker model with `bash` only
  (`chat.worker_max_steps`). **Topic jobs** get `bash`; **the summary** gets `bash` and
  `delegate`. Jobs and the worker never get a writing tool.
- Ollama URLs get Pydantic AI's Ollama profile; reasoning effort is sent as
  `openai_reasoning_effort` (Pydantic AI's `thinking` setting sends nothing to these endpoints).

## Jobs and scheduling

`jobs` holds the definitions (builtins are seeded from `jobs/builtins.py`; unedited ones follow
later releases, edited ones keep the owner's version until *Reset*); `job_runs` holds the runs.
`JobRunner` runs a job as one agent run with `output_type=JobResult` (status, summary, findings)
under a per-job lock and a global semaphore (`jobs.max_concurrent_runs`) with a time limit.
Every failure is stored as `error` with the reason; the overall headline turns it into plain
words (`jobs/status.py`). The summary's input is the latest result of every enabled topic job
and the previous summary.

`schedule.py` is APScheduler's `AsyncIOScheduler` in memory, rebuilt from the `jobs` table after
every change (`sync_jobs`): interval or cron triggers in the host's time zone, `coalesce`,
`max_instances=1`. Housekeeping (pruning, approval sweeps, the hourly-cap queue) is jittered and
skips runs missed while paused. Pause pauses the scheduler and cancels scheduled runs in flight.
A stop stores runs in flight as `interrupted (the service stopped)`; after the start those jobs
are moved to run about a minute later (summary last), and the latest-run queries skip an
interrupted run when the job has a real result.

## Approvals and continuation

Every write is an action (`actions/registry.py`: `mcp_call`, `host_command`) requested through
`ActionService`: validate → policy (`off`, `suggest`, `approve`, `auto`) → pending approval or
execute (hourly cap, dry-run) → a permanent `actions` row. Items of one chat turn and action form
one `approval_batches` row (one card, one Telegram message). Policy and dry-run come only from
settings, never from the request.

In the chat, an approval-required call is deferred by Pydantic AI (`DeferredToolRequests`).
`on_approval` files it; when a person has to decide, the run ends with a `Suspended` output
(messages and settled/pending calls, JSON) stored in `chat_pending_runs` /
`chat_pending_actions`, and the answer is the waiting note. `ChatEngine.on_action` resumes the
run when every action of it is executed, failed, dry-run, rejected or expired: approved calls get
the action's stored result (they are not run again), the others a `ToolDenied` with the reason.
Before that, the stored waiting note is rewritten to the outcome ("Approval #3: … approved and
run, exit code 0.", `meta.outcome`) and published as `chat.message.updated`. The new answer is
stored and published as `chat.message`; Telegram sends it to the chat that asked. The generic `mcp_call` is deferred the same way when it reaches a write tool.

`host_command` (`bash_write`) runs `nsenter --target 1 --mount --uts --ipc --net --pid -- bash
-c CMD` as root; `ChangeShell.probe()` checks at start whether that works, and
`/api/status.change_shell` says why not. Its approval runs in the background so the approve call
returns at once; the result arrives as `action.result` / `approval.progress`.

## MCP client

`mcp_client/` uses the official `mcp` SDK (`<2`: 2.x moved to a new client API): Streamable
HTTP and the older HTTP+SSE (`auto` tries the first and falls back on protocol failures, not
on network errors or 401/403); no stdio; tools only (no prompts, resources or sampling). One
runner task per server owns the SDK's context managers, and calls race against it, because
the SDK leaves a request hanging when a Streamable HTTP server dies. Connect failures back off
2, 4, … 60 s; a timed-out call drops the connection. A tool is read only when the server marks
it `readOnlyHint` and the owner did not override it; the manager enforces the tool allow-list
itself. Results are capped and scrubbed of the server's own token and header values. Above
`mcp.max_inline_tool_tokens` the chat gets the generic `mcp_list_tools` / `mcp_call` pair.

## Storage (SQLite, `data/monitor.db`)

| Table | Holds |
|---|---|
| `kv` | settings (and backups), the automation switch, the chat audit list, small state |
| `jobs`, `job_runs` | job definitions; runs with status, summary, findings, error, tokens, trace turn |
| `chat_sessions`, `chat_messages` | conversations |
| `chat_pending_runs`, `chat_pending_actions` | chat runs waiting for approvals |
| `actions`, `approval_batches` | the permanent audit log and approval groups |
| `telegram_batch_messages` | which Telegram message shows which approval |
| `llm_calls`, `llm_tool_calls` | model requests and tool calls (Logs page) |
| `app_log` | saved warnings and errors of the app log |

Retention: `storage.retention` for chats and runs, `logs.*` for traces; actions are never pruned.

## Security boundaries

The read-only shell is `chroot /host` plus the host's `env -i … prlimit … nice ionice setpriv
--reuid=2147483000 --clear-groups --inh-caps/ambient/bounding-set=-all,+dac_read_search
--no-new-privs -- bash -c CMD` (without `DAC_READ_SEARCH` in the container: no capability and the
`adm`/`systemd-journal` groups). The uid owns no host process, so it can signal nothing; the
host mount is read-only; `DAC_READ_SEARCH` reads but cannot open the Docker socket (connect
needs write permission); `docker` goes through a GET-only socket proxy. Measured on a simulated
host (Docker 29.8, WSL2, no AppArmor; the probe scripts live outside the repo): all reads above
work; writes (EROFS), signals to uid 1000, 65534 and PID 1 (EPERM), the real socket, proxy writes
(403), device/`/proc/sys`/`/sys` writes (EACCES), `nsenter` and `unshare` all fail, also when
the container has `SYS_ADMIN` and `SYS_PTRACE` (the sandbox's effective set stays `0x4`). Not
measured: `nvidia-smi`, a real AppArmor host.

`nsenter` into PID 1 needs both `SYS_ADMIN` and `SYS_PTRACE` (measured: neither alone works).
Only the chat's main model can propose a root command; a person approves it unless the owner
chose `auto`.

## Not built, and why

- **Our own sandbox runtime**: bubblewrap 0.11 and `unshare` fail under Docker's default
  seccomp ("Creating new namespace failed"); with seccomp off they map the sandbox to real root.
  nsjail is not packaged in Debian trixie and needs the same calls. `setpriv` + `chroot` need no
  namespaces.
- **JSON-in-text tool calls** for models without native tool calling: Pydantic AI has none, and
  a wrapper model would be ~250 lines of our own. Such models are not supported.
- **Pydantic AI's MCP toolset**: its `mcp` extra needs `mcp>=2`; we stay on the `mcp` SDK
  through `mcp_client/` (connection, discovery, tool picking, the `mcp_call` action).
- **Our own scheduler, metrics registry or Telegram client**: APScheduler, prometheus_client and
  python-telegram-bot replaced them.
- **Collectors, reports, charts, health scores, the Prometheus query planner**: removed in 0.7;
  the jobs do this with the shell.
- Small gaps kept: tool names the model invents are answered by Pydantic AI itself (no
  `llm_tool_calls` row); an endpoint that rejects `reasoning_effort` fails until the setting is
  `auto`.
