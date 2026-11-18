# Change-shell lane: hand-over (2026-11-18)

Approval-gated changing commands from the chat, and chat runs that continue by themselves
after an approval.

## What was built

- `agent/change_shell.py` `ChangeShell`: root bash on the host via util-linux
  `nsenter --target 1 --mount --uts --ipc --net --pid -- /bin/bash -c CMD` (the container has
  `pid: host`). `probe()` once at start; `/api/status.change_shell` = {policy, available, offered,
  reason}. `agent/shell.py` `run_process` is shared with the read-only shell.
- `actions/host_command.py`: action `host_command` {command, reason}; policy
  `approvals.shell_changes` (off | approve (default) | auto); result {exit_code, output (head+tail,
  4000 chars), duration_s, stopped, truncated} in the permanent `actions` audit (reason column =
  the model's reason). Timeout = `chat.command_timeout`.
- `bash_write` (agent/tools.py): chat main model only, approval-required (Pydantic AI deferred
  call). Not given to jobs or the `delegate` worker. Offered only if policy != off and available.
- Runtime: `on_approval` may return `PendingApproval`; the run then ends with a `Suspended`
  output (messages + settled/pending calls, JSON-serialisable). `resume_results` +
  `AgentConfig.resolved` resume it: approved calls return the action's result (not run again),
  rejected/expired ones are `ToolDenied` with the reason.
- Chat (`chat/engine.py`): a suspended run is stored (migration 0017 `chat_pending_runs`,
  `chat_pending_actions`); the answer is the waiting note. `ChatEngine.on_action` (an action
  notifier) resumes when every action of the run is executed/failed/dry_run/rejected/expired,
  stores the new answer (meta `continued: true`) and publishes `chat.message` {session_id,
  message_id, source, text}. Telegram sends it to the chat when source is `telegram:<id>`. The
  same path serves MCP write tools (not the generic `mcp_call` meta tool, which files itself).

## Compose lines (added commented out to docker/compose.example.yaml)

    cap_add:
      - DAC_READ_SEARCH
      - SYS_ADMIN      # setns() into the host's namespaces
      - SYS_PTRACE     # open /proc/1/ns/* of a process with more capabilities

## Probes (Docker 29.8 / WSL2; PID 1 = the Docker VM; scripts in the notes folder)

nsenter into PID 1 by capability set: default ✗, +SYS_PTRACE ✗, +SYS_ADMIN ✗ ("reassociate
to namespaces failed"), +SYS_ADMIN +SYS_PTRACE ✓ (uid 0, hostname docker-desktop).

Real code (`probe_change.py`: ChangeShell + ActionService, policy approve):

| step | with SYS_ADMIN+SYS_PTRACE | without |
|---|---|---|
| probe | available | not available: "cannot enter the host's namespaces (…): add SYS_ADMIN and SYS_PTRACE …" |
| request `touch /tmp/marker` | pending, file absent | (tool not offered; status says why) |
| approve | executed, exit 0, ran as uid 0 on docker-desktop | — |
| on the host | `/tmp/marker` exists (root), then removed | — |

Read-only `bash` with DAC_READ_SEARCH + SYS_ADMIN + SYS_PTRACE granted (`probe_shell.py`):
every W check OK, every F check still blocked: writes (EROFS), kill of uid 1000 / 65534 / pid 1
(EPERM), real docker socket (denied), proxy write (403), device/proc/sys writes (EACCES),
`mkdir /tmp` (EROFS), and new: `nsenter -t 1 …` ("cannot open /proc/1/ns/ipc: Permission
denied"), `unshare -r` (EPERM). The sandbox's CapEff stays 0x4.

## Frontend (not done here; for the frontend worker)

- Regenerate `src/api-types.ts` (new: `StatusResponse.change_shell`, settings
  `approvals.shell_changes`).
- Chat page: listen for `chat.message` on `/api/stream?types=chat` and append the message when
  `session_id` is open (else it is there on reload). The waiting note is the assistant message
  with `meta.waiting_for` (action ids).
- Approval card for `host_command`: show `args.command` (monospace) and `reason`; the result
  after execution has `exit_code` and `output`.
- Dashboard/Settings: show `change_shell.reason` when `available` is false.
