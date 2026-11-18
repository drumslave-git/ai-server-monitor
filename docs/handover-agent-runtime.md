# Agent runtime lane: hand-over (2026-11-18)

Status: phases 1+2 built and wired. Chat = one Pydantic AI agent on the main model with
`bash`, `delegate` and the MCP tools; `delegate` runs the worker model with only `bash`.
Probe scripts, Dockerfiles and raw results: `side-session-notes/local_808ae114-…/agent-runtime-probe/`.

## 1. Sandbox (agent/shell.py) — measured

Simulation: Docker 29.8 (WSL2 5.15, no AppArmor), container from `python:3.13-slim`, `pid: host`,
`apparmor=unconfined`, bridge network; fake Ubuntu 24.04 host tree `:ro` at `/host` (bash,
iproute2, docker CLI, util-linux, a real journal file 0640 root:systemd-journal, 0700 root dirs),
the VM's real `/proc`, `/sys`, `/dev` and docker socket bind-mounted read-only inside it,
`tecnativa/docker-socket-proxy:v0.4.1`, decoy processes as uid 1000 and 65534 on the host pid ns.

Rejected (measured): **bubblewrap 0.11** fails at start under default caps+seccomp ("Creating new
namespace failed"); `unshare -m`/`-U` fail the same (Docker's seccomp). With seccomp off it either
maps the sandbox uid to real root (reached the real docker socket) or stays uid 0 (`kill -0 1`
worked, real socket reachable). **nsjail**: not packaged in Debian trixie, same namespace calls.

Chosen: `chroot /host` + the host's util-linux `env -i … prlimit … nice ionice setpriv
--reuid=2147483000 --regid=… --clear-groups --inh/ambient/bounding-set=-all,+dac_read_search
--no-new-privs -- bash -c CMD`. Without the capability: `-all` and `--groups=adm,systemd-journal`.
DAC_READ_SEARCH reads every file and writes none; it cannot open the docker socket (connect needs
write permission). The uid owns no host process, so nothing can be signalled (the host's `nobody`
could signal `nobody` processes: measured, F5 of the 1A run). No namespaces → default seccomp ok.

Results of the real `HostShell.run` (probe_shell.py; `cap` = with `cap_add: DAC_READ_SEARCH`):

| # | command | cap | default caps |
|---|---|---|---|
| W1 | `du -sh` 0700 root dir | 2.9M | denied + compose note |
| W2 | `cat` 0600 root file | OK | denied + compose note |
| W3 | `journalctl -D /var/log/journal` | OK | OK (adm/systemd-journal) |
| W4 | `ps -eo user,pid,args` host procs | OK | OK |
| W5 | `ss -ltn`; `/proc/1/net/tcp` | container netns only; host table readable | same |
| W6 | `nvidia-smi` | NOT MEASURED (no GPU) | — |
| W7 | `docker ps/logs/inspect/stats --no-stream/system df/images` (proxy name resolved to IP) | all OK | all OK |
| W8 | pipes, `2>&1`, subshells, `$( )`, `> /dev/null` | OK | OK |
| W9 | identity | uid 2147483000, CapEff=CapAmb=0x4, NoNewPrivs 1 | uid …, caps 0 |
| F1-3 | write/rm/touch host files | EROFS | EROFS/EACCES |
| F4-6 | `kill -0` uid 1000, uid 65534, pid 1 | EPERM ×3 | EPERM ×3 |
| F7 | real docker socket | permission denied | permission denied |
| F8 | `docker rm` via proxy | 403 | 403 |
| F9-14 | open for write `/dev/sda`, `/dev/kmsg`, `/dev/mem`, `/proc/sys/vm/drop_caches`, `/sys/…/thp/enabled`, `/proc/<pid>/oom_score_adj` | EACCES ×6 | EACCES ×6 |
| F15 | `mkdir /tmp/x` | EROFS | EROFS |

Process names of other users' sockets (`ss -p`) need CAP_SYS_PTRACE = can attach to any host
process: not granted. The prompt tells the model where host ports are (`/proc/1/net/tcp`, `docker ps`).

## 2. What was built

- `agent/models.py` `ModelHub`: main (`settings.llm`) and worker (`settings.worker`; empty model
  = main model, empty base URL = main endpoint+key) as `OpenAIChatModel` on
  `OpenAICompatibleProvider(openai_client=AsyncOpenAI(http_client=<our httpx client>))`; the httpx
  transport is the tracing transport, so every agent request lands in `llm_calls` with tokens.
  Ollama URLs get Pydantic AI's Ollama profile; all: `max_tokens`, no strict tool schemas.
- `agent/runtime.py` `run_agent`: Pydantic AI `run_stream_events` → our event dicts; progress
  events; tool calls → `llm_tool_calls` (sub-agent ones as `worker:bash`, LLM calls purpose
  `subagent`, same turn); step limit via `PrepareTools` (tools hidden past `max_steps`, note in
  instructions) + `UsageLimits` backstop; approval-required tools → `DeferredToolRequests` →
  `on_approval` → `ToolDenied(message)` → the model continues in the same run.
- `agent/tools.py`: `bash`, `delegate`, `mcp_agent_tools` (MCP stays on `mcp_client/`).
- `chat/engine.py` rewritten on `run_agent`; titles via a tool-less Pydantic AI agent.
- Settings: `worker` section ("Worker model"), `llm` titled "Main model"; `chat.max_steps` 15
  (old stored 6 upgraded), `chat.worker_max_steps` 20, `chat.command_timeout` 5m; removed
  `chat.shell`, `disk_cache_ttl`, `tool_routing`, `tool_routing_tokens`.
- Deleted: `hostinspect/` (except `output.py` → `agent/output.py`), `chat/host_tools.py`,
  `chat/routing.py`, `chat/prompts.py`, `chat/humanize.py`, the old chat loop and non-MCP tools.
- compose: `cap_add: [DAC_READ_SEARCH]`.

## 3. What Pydantic AI could not do / kept by hand

- JSON-in-text tool calls: no built-in. Not built (estimate: a WrapperModel converting history
  and tool defs to text, plus a StreamedResponse for streaming, ~250 lines). json/none modes now
  answer without tools from the digest. QUESTION for the master.
- MCP toolset: Pydantic AI 2.70's `mcp` extra needs `fastmcp-slim` → `mcp>=2`; we pin `mcp<2`.
- `ModelSettings.thinking` sends nothing for OpenAI-compatible/Ollama profiles (measured); we send
  `openai_reasoning_effort`. The old client dropped `reasoning_effort` after a 400; Pydantic AI
  does not (an endpoint that rejects it fails until reasoning effort is set to auto).
- Approvals: Pydantic AI defers and can resume any time later from serialised messages (no
  process waits). We resolve the deferral at once ("filed, waiting for approval") and the
  approval service runs the call on approval as before. A chat that continues with the executed
  result after approval is not built (needs stored messages + an ActionService hook).
- Unknown tool names: Pydantic AI answers the model itself; no `llm_tool_calls` row for them.
- Still on `llm/client.py`: reports, digest, Prometheus planner, the tool-mode probe and the
  Settings test button (next lane removes most of it).

## 4. Public interface (for the jobs lane)

    from ai_server_monitor.agent.runtime import AgentConfig, AgentTool, ToolError, run_agent
    from ai_server_monitor.agent.models import ModelHub        # app.models
    from ai_server_monitor.agent.tools import bash_tool, delegate_tool
    from ai_server_monitor.agent.shell import HostShell        # HostShell(app.host_env.root, docker_host=…)

    @dataclass
    class AgentTool:
        name: str; description: str; parameters: dict        # JSON schema
        handler: Callable[[dict], Awaitable[Any]]          # raise ToolError for failures
        requires_approval: bool = False

    @dataclass
    class AgentConfig:
        model: Model                                       # app.models.model("main" | "worker")
        instructions: str
        tools: Sequence[AgentTool] = ()
        model_settings: ModelSettings | None = None
        max_steps: int = 15
        tool_timeout_s: float = 3600.0
        result_chars: int = 6000
        scrub: Scrubber | None = None                      # agent.safety.Scrubber(settings)
        tracing: Any = None                                # app.tracing
        trace: dict = {}                                   # purpose, link_kind, link_id, turn
        trace_tool_prefix: str = ""
        output_type: Any = str                             # a pydantic model = structured finish
        on_approval: Callable[[AgentTool, dict], Awaitable[str]] | None = None

    async def run_agent(cfg, prompt, *, history=None) -> AsyncIterator[dict]
    # events: token, tool_call, tool_progress, tool_result, then
    # {"type": "end", "output", "steps", "stopped", "usage"}; model errors raise AgentRunError.
