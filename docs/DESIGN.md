# AI Server Monitor — Design

One container on the monitored host: a FastAPI backend (`backend/ai_server_monitor/`) and a
React/Vite SPA (`frontend/`, served by the backend). The rule since 0.7: **nothing is collected
by code; a language model does all the monitoring** through a read-only shell, and the code only
schedules, stores, shows and guards. The one reading the code does itself is the hardware meters
of the sidebar (`GET /api/hardware`: CPU and RAM through psutil, the whole machine's also inside
the container; GPUs through one `nvidia-smi` call in the read-only shell), read when the page
asks and never stored. Where a maintained library does the job, it is used instead of our own
code.

## Components

| Part | Code | Library |
|---|---|---|
| Agent loop: tools, step limit, approvals, tracing hooks | `agent/runtime.py` | Pydantic AI |
| Models: main and worker on any OpenAI-compatible endpoint | `agent/models.py` (`ModelHub`) | Pydantic AI `OpenAIChatModel`, `openai`, `httpx` |
| Read-only shell sandbox | `agent/shell.py` | the host's util-linux (`setpriv`, `prlimit`), `chroot` |
| Root shell for approved changes | `agent/change_shell.py` | util-linux `nsenter` |
| Tools: `bash`, `delegate`, `bash_write` | `agent/tools.py` | Pydantic AI |
| MCP servers: toolsets, domains, classification, the MCP tools page's API | `mcp_client/` | Pydantic AI `MCPToolset` (FastMCP 4, `mcp` SDK 2, `httpx2`) |
| Jobs: store, builtins, prompts, runner, API, chat tools | `jobs/` | — |
| Schedule: jobs + housekeeping, pause | `schedule.py`, `automation.py` | APScheduler 3 |
| Chat engine and sessions | `chat/` | — |
| Approvals and audit (the action layer) | `actions/` | — |
| Telegram bot | `outputs/telegram.py` | python-telegram-bot |
| Hardware meters: CPU, RAM, NVIDIA GPUs, read on request | `hardware.py` | psutil, `nvidia-smi` in the read-only shell |
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
  offered) and the MCP tools of its domains (see MCP client). **`delegate`** runs the worker model with `bash` only
  (`chat.worker_max_steps`). **Topic jobs** get `bash`; **summaries** get `bash` and
  `delegate`. Jobs and the worker never get a writing tool.
- Ollama URLs get Pydantic AI's Ollama profile; reasoning effort is sent as
  `openai_reasoning_effort` (Pydantic AI's `thinking` setting sends nothing to these endpoints).

**Explain** (`chat/explain.py`): `POST /api/chat/explain` (run id, optional finding index)
composes the first message of a new chat from what is stored for the run: the job and its
prompt, the run, all findings with evidence and the `verified` mark (the chosen one first, as the
subject), the `bash` calls of the run's trace turn with their output, and for a summary the job
runs of its queue run in the order they ran (`queue_results`: the job results it was given; none
for runs from before 0.10.0). It is fitted to 40 % of the
main model's context (Ollama's `/api/show` context length, else 32k tokens): the subject stays
whole, outputs are cut in the middle to a fair share, then the oldest outputs the findings do not
quote are left out, each with a note. Outputs are wrapped as untrusted data. The chat engine runs
it as an ordinary turn; the stored question carries `meta.explain.label`, which is the session
title and what the UI shows (the source folded under it). The turn must look before it answers:
`LookFirst` (an output validator) sends an answer given before any `bash`/`delegate` call back
once; a forced `tool_choice` is not used because Ollama ignores it and some endpoints reject
`required`. Run times in the data are in the host's time zone. Telegram's result messages (of a
summary, or of a job in no summary) have an Explain button (signed callback `x:<run>:<sig>`) that runs the same turn in that chat.

## Jobs and scheduling

`jobs` holds the definitions (builtins are seeded from `jobs/builtins.py`; unedited ones follow
later releases, edited ones keep the owner's version until *Reset*); `job_runs` holds the runs.
`JobRunner` runs a job as one agent run with `output_type=JobResult` (status, summary, findings)
under a per-job lock and a global semaphore (`jobs.max_concurrent_runs`) with a time limit.
Every failure is stored as `error` with the reason; the overall headline turns it into plain
words (`jobs/status.py`); the overall status is the worst of the enabled summaries' newest runs.

A **summary** is a queue: one schedule and an ordered list of jobs (`summary_members`, by
`position`). A job in a summary has no schedule of its own (it loses it when it joins, and a
scheduled job cannot be added); a job can be in several summaries; a summary cannot be a member.
`JobRunner` runs the summary's enabled members one after another, each run stored with
`job_runs.queue_run_id` pointing at the summary run, then the summary itself. Its input is the
results of this queue run and the previous summary. Its result (`SummaryResult`) also has
`topics`: one entry per job of this queue run (slug, status, one clause). `TopicCheck` sends a
list with a missing job or a status other than that job's run back once; what is stored has every
job with its real status (`job_runs.topics`), shown under the headline on the Dashboard and in
Telegram. `jobs.notify` (`always`, `changed`, `warn_crit`, `never`) decides what goes to Telegram:
a summary's and a job's in no summary; a run inside a queue run sends nothing. Builtins: 14 topic
jobs and the summaries `summary:health` and `summary:daily`; the builtins of 0.9.1 that 0.10.0
replaced are `retired` (hidden from the list and the scheduler, their runs readable). List
fields sent as a JSON string are read from their first JSON array (trailing characters ignored);
an unreadable one is answered with what was sent, what is expected and an example.

`schedule.py` is APScheduler's `AsyncIOScheduler` in memory, rebuilt from the `jobs` table after
every change (`sync_jobs`): interval or cron triggers in the host's time zone, `coalesce`,
`max_instances=1`. Housekeeping (pruning, approval sweeps, the hourly-cap queue) is jittered and
skips runs missed while paused. Pause pauses the scheduler and cancels scheduled runs in flight.
A stop stores runs in flight as `interrupted (the service stopped)`; after the start those jobs
are moved to run about a minute later (summaries last; only jobs with a schedule: an
interrupted summary reruns its whole queue, a job in a summary waits for its next run), and the
latest-run queries skip an
interrupted run when the job has a real result.

## Approvals and continuation

Every write is an action (`actions/registry.py`: `mcp_call`, `host_command`) requested through
`ActionService`: validate → policy (`off`, `suggest`, `approve`, `auto`) → pending approval or
execute (hourly cap, dry-run) → a permanent `actions` row. Items of one chat turn and action form
one `approval_batches` row (one card, one Telegram message). Policy and dry-run come only from
settings, never from the request.

An approval can cover the rest of the run (`decide_batch(..., rest=True)`, `POST
/api/approvals/{id}` with `rest`): the kinds of the approved items (`ActionDef.covers`; for
`mcp_call` the server and tool, none for `host_command`) are remembered for their source and
`run_key`, and later requests of those kinds run when they are made and join that batch, each
its own audit row decided by "<who>: rest of the run". This is held in memory by
`ActionService`: it ends with the run, and with a restart. A chat run resumed after an approval
keeps the `run_key` of its batch, so "the run" is one answer; the next question asks again.
The agent loop has no own limit on rounds of calls settled at once: the step limit ends them.

In the chat, an approval-required call is deferred by Pydantic AI (`DeferredToolRequests`).
`on_approval` files it; when a person has to decide, the run ends with a `Suspended` output
(messages and settled/pending calls, JSON) stored in `chat_pending_runs` /
`chat_pending_actions`, and the answer is the waiting note. `ChatEngine.on_action` resumes the
run when every action of it is executed, failed, dry-run, rejected or expired: approved calls get
the action's stored result (they are not run again), the others a `ToolDenied` with the reason.
Before that, the stored waiting note is rewritten to the outcome ("Approval #3: … approved and
run, exit code 0.", `meta.outcome`) and published as `chat.message.updated`. The new answer is
stored and published as `chat.message`; Telegram sends it to the chat that asked.

`host_command` (`bash_write`) runs `nsenter --target 1 --mount --uts --ipc --net --pid -- bash
-c CMD` as root; `ChangeShell.probe()` checks at start whether that works, and
`/api/status.change_shell` says why not. Its approval runs in the background so the approve call
returns at once; the result arrives as `action.result` / `approval.progress`.

## MCP client

**Servers** (Settings → MCP servers): remote servers only, Streamable HTTP or the older
HTTP+SSE; no stdio programs; tools only (no prompts, resources or sampling). The client is
Pydantic AI's `MCPToolset` on FastMCP 4 (`mcp` SDK 2, `httpx2`); `mcp_client/toolsets.py`
builds one per server from its settings (`server_toolset`). `auto` takes the transport FastMCP
infers from the URL (`infer_transport_type_from_url`: a path ending in `/sse` is HTTP+SSE,
anything else Streamable HTTP); nothing is tried first and there is no fallback. Token and
extra headers go on the transport; `timeout` is the connect and per-call timeout.

Nothing is long-lived: every agent run (chat turn, job run) and every approved call connects
the servers it needs when it starts and closes them when it ends; all servers of a run are
entered in parallel (`mcp_client/turn.py`), so a server that does not answer costs its timeout
once per run (a chat turn connects every enabled server, also those whose domains stay
deferred; a job run only the servers of its domains). `Reachable` (the one wrapper the library has no equivalent for) turns a server
that cannot be reached, or dies during the run, into no tools plus a note for the model and the
turn log, instead of a failed run. `HttpSeen` is the HTTP client FastMCP uses for each server
(through the transport's `httpx_client_factory`): the MCP SDK reduces an error status to
"Server returned an error response" and a failed connection loses its cause on the way up, so
this client records the last thing that went wrong (the status with method and path, refused /
DNS / TLS / timeout, or a request left unanswered; a 405 to the optional GET stream or to
DELETE is the spec's "not offered" and is not recorded). The Settings **Test**, the notes and
the listings say that ("the server answered HTTP 401 Unauthorized to POST /mcp").

**Tools**: the model sees a tool as `<server>_<tool>` (server names have no `_`), with the
server's whole description (nothing is cut; there is no token budget and no generic
list/call pair). Only the tools the owner switched on (`tool_filter`, also switched on the MCP
tools page) are offered. A tool is *read* when the server marks it `readOnlyHint` and the owner
did not override its kind; everything else is a *write*: its description starts with a WRITE
note, and in the chat it is approval-required (Pydantic AI defers it, the chat files it as an
`mcp_call` action under the server's policy, see Approvals); in a job a write call is filed as
an action and the model is told so (nobody waits on a scheduled run). Results are scrubbed of
the server's own token and header values; every call goes to the chat audit list, the Logs page
and the untrusted-data wrapping like any other tool result. `mcp.dry_run` records approved
writes without sending them.

**Domains** (`mcp_client/domains.py`): a domain is an area a person asks about (an
application, a device), not a server; none is predefined, every domain can be renamed,
described or deleted (not while a job uses it). Every tool of a configured server is recorded
as last listed (`mcp_tools`, `mcp_listings`). Tools without a domain are sorted by one
structured call of the main model (Logs purpose `mcp_sort`): after a Settings Test, after a
Settings save (in the background; a run that starts meanwhile waits for it), at the start of a
run when a listing shows new tools, and when the owner presses *Sort into domains…* on the MCP
tools page (`POST /api/mcp/sort`; the owner's own placements and domain texts, `set_by='owner'`,
stay unless the owner asks to redo them). A tool whose sorting failed keeps no domain and the reason
("Not sorted" on the page) and is not offered. Deleting a domain sends its tools back to
sorting. A server removed in Settings loses its rows; a server renamed is a new server, so its
tools are sorted afresh and the owner's placements of the old name are gone. `GET
/api/mcp/tools` says `sorting: true` while a sort runs.

**Chat turns**: the worker model picks the request's domains (one structured call, Logs purpose
`mcp_classify`, with the domain descriptions, their tool names and the date line). Each domain is
one Pydantic AI `Toolset` capability: the picked ones and the ones loaded earlier in the session
(read from the stored answers' `meta.mcp_domains.loaded`) are given directly, every other domain
is deferred, so the library lists it in the instructions and adds `load_capability`, with which
the model loads it during the turn (capability id `domain-<name>`; a renamed domain is not kept
as loaded by the session, since the stored name no longer matches). A failed classification gives every domain as loadable. A
run resumed after an approval gets the same domains it had (`Suspended.domains`). The turn log
(`meta.mcp_domains`: picked, offered, loaded, unsorted, unreachable, classify_error) is the quiet
"Domains: …" line under the answer. Which capabilities a run loaded is read with
`pydantic_ai._deferred_capabilities.parse_loaded_capabilities`, a **private** import
(`agent/runtime.py`): a Pydantic AI upgrade can move it; `tests/test_mcp_turns.py` fails then.

**Jobs**: a job has a domain list (`job_domains`; Jobs → Domains, with a *Classify* button that
asks the worker model). A run gets exactly the enabled tools of those domains, all given
directly, nothing classified or loadable; builtin jobs start with none. If the tools are not
enough the model returns `missing_tools` and the run ends as `error` with "missing tools: …"
(the instructions tell it not to work around it through `bash`). `job_runs.mcp` keeps the run's
domains, unsorted tools and unreachable servers.

**The MCP tools page** (`frontend/src/pages/McpTools.tsx`): one card per domain with its tools
(on/off, read/write, server, the whole description, *Move to…* another or a new domain),
rename/describe/delete per domain, "Not sorted" on top when a sorting failed, a note per server
that could not be listed, search and filters (server, read/write, on/off), *Select all* /
*Deselect all* per card and for the page (under a search or filter they act on the tools shown
and say "shown"). Cards start open; the ones the owner collapses are remembered in that browser
by domain name (localStorage, `asm.mcp-tools.collapsed`); a search or filter opens every card
with matches without changing what is remembered.

**Changes since 0.7** (when the client was our own runner on the `mcp` 1.x SDK): transport by
URL instead of probing; one connection per run instead of a kept connection with backoff; names
`server_tool` instead of `server__tool`; whole descriptions instead of 400 characters; the
token budget (`mcp.max_inline_tool_tokens`) and the generic `mcp_list_tools` / `mcp_call` pair
are gone, so is the Tokens column in Settings; domains, classification, job domains and the
page are new. Own code in `mcp_client/` (all lines of its `.py` files): 1,391 at `8e70ddb`
(0.7.3, before the rework), 1,484 after 0.8.4, of which `domains.py`, `turn.py` and `api.py`
are new functions the old client did not have.

**Every run knows the date** (`agent/prompts.py` `now_line`): chat turns, job runs, the domain
classification and the model connection test get "Now: <weekday date time>, time zone …" in
the owner's zone (Settings → General, the host's by default) as a dynamic instruction, so every
model request has the time it runs at.

## Storage (SQLite, `data/monitor.db`)

| Table | Holds |
|---|---|
| `kv` | settings (and backups), the automation switch, the chat audit list, small state |
| `jobs`, `job_runs` | job and summary definitions; runs with status, summary, findings, error, tokens, trace turn, the summary run they were part of |
| `summary_members` | the ordered jobs of each summary |
| `chat_sessions`, `chat_messages` | conversations |
| `chat_pending_runs`, `chat_pending_actions` | chat runs waiting for approvals |
| `actions`, `approval_batches` | the permanent audit log and approval groups |
| `telegram_batch_messages` | which Telegram message shows which approval |
| `llm_calls`, `llm_tool_calls` | model requests and tool calls (Logs page) |
| `app_log` | saved warnings and errors of the app log |
| `mcp_domains`, `mcp_tools`, `mcp_listings` | MCP domains; every listed tool with its domain, who placed it and a sorting error; the last listing of each server |
| `job_domains` | the MCP domains of each job (`job_runs.mcp`: what a run had) |

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

systemd: `systemctl` fails in the sandbox (the system bus closes the connection after `Hello`
for the passwd-less uid; `/run/systemd/private` is 0700 root), so the systemd jobs read
systemd's Varlink socket (`io.systemd.Manager.Describe`, `io.systemd.Unit.List`; systemd ≥ 257)
and the journal. That socket is 0666 and also has `Reload`/`Reexecute`, which systemd checks
only through polkit (`reload-daemon`, admin authentication by default): the sandbox's guarantee
there rests on the host's polkit policy (measured 2026-11-18 on the live host, systemd 259).

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
- **A kept MCP connection, reconnects, backoff or a cache of failed servers**: one connection
  per run is what Pydantic AI's toolset does; a dead server costs its timeout per run.
- **Our own scheduler or Telegram client**: APScheduler and python-telegram-bot replaced them.
- **Collectors, reports, charts, health scores, the query planner**: removed in 0.7;
  the jobs do this with the shell.
- Small gaps kept: over HTTP+SSE an MCP call cut off by its timeout sometimes ends as
  "Connection closed" instead of "timed out" (a race in the transport); tool names the model invents are answered by Pydantic AI itself (no
  `llm_tool_calls` row); an endpoint that rejects `reasoning_effort` fails until the setting is
  `auto`.
