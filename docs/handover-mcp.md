# MCP client: design and the shared-file edits still to make

State after phase 1: the package `backend/ai_server_monitor/mcp_client/` is complete and
tested (`backend/tests/test_mcp_client.py`, real local MCP server over both transports).
Nothing outside the package, `tests/` and `pyproject.toml` (`mcp>=1.27,<2`) is touched yet.

## Decisions

- **SDK**: the official `mcp` package, pinned `>=1.27,<2`. It is built on httpx/anyio like the
  rest of the app and handles the protocol details that a hand-rolled JSON-RPC client would get
  wrong (session ids, SSE resumption, protocol version headers, list_changed). Cost: ~12 extra
  wheels (cryptography, pyjwt, jsonschema, sse-starlette, python-multipart, ...), all with
  manylinux wheels. `mcp` 2.x moved to `httpx2` and a new client API, hence `<2`.
- **Transports**: Streamable HTTP and legacy HTTP+SSE (`auto` tries the first, falls back to
  the second on protocol-level failures, not on network errors or 401/403). No stdio.
- **Connection lifetime**: one background "runner" task per server owns the SDK context
  managers; calls race against the runner, because when a streamable-HTTP server dies the SDK
  leaves the request hanging (measured: 30 s) instead of failing it.
- **Backoff**: connect failures back off 2, 4, ... 60 s; until then calls fail at once with
  "unreachable ... retrying in Ns". A timed-out call drops the connection (no backoff).
- **Schema size**: built-in chat tools are ~1300 tokens in total. A typical MCP tool costs
  25-60 tokens here, real ones (Home Assistant, Grafana) usually 100-300. Default
  `max_inline_tool_tokens = 1200`: above it the chat gets `mcp_list_tools` + `mcp_call` only.
  `0` forces the pair.
- **Safety**: read = server says `readOnlyHint: true` and the user did not override it; the
  rest is write. Writes are only ever requested via `ActionService.request("mcp_call", ...)`.
  The manager also enforces the allow-list itself. Tool results are capped, scrubbed of the
  server's own token/header values, and (by the engine) wrapped as untrusted.
- Prompts/resources/sampling/elicitation from servers are not used (tools only).

## Shared-file edits (phase 2)

1. **config.py**: move `McpHeader`, `McpToolOverride`, `McpServerSettings`, `McpSettings` from
   `mcp_client/settings.py` into config.py (they use `_Model`, `Secret`, `Duration`, `HttpUrl`,
   `Name`, `Policy`) and add `mcp: McpSettings = McpSettings()` to `Settings`; keep
   `mcp_client/settings.py` as `from ..config import ...` re-exports. `servers` carries
   `json_schema_extra={"test": "mcp"}`.
2. **context.py**: `self.mcp = McpManager(settings.mcp, audit=...)` in `__init__` and in
   `_start_services` (close the previous one in `_stop_services`: `await self.mcp.close()`).
   Pass it to `ActionService` (below).
3. **actions/service.py + registry.py**: the service hard-codes `settings.arr.actions.<key>`
   and `execute(clients, args)`. Add to `ActionDef` optional `policy_of(settings, args) -> str`,
   `max_per_hour_of(settings, args) -> int` (MCP: 0), `uses_dry_run(settings) -> bool`
   (MCP: `settings.mcp.dry_run`; it must NOT follow `arr.dry_run`, which defaults to true) and
   `run(service, args)` (MCP: `execute(service.mcp, args)`); `request`/`_run` use them when set.
   Give `ActionService` an `mcp` attribute. Register `mcp_call` with
   `mcp_client.actions` (`McpCallArgs`, `policy_of`, `policy_key`, `target`, `check`,
   `execute`, `ACTION_NAME`, `DESCRIPTION`). Do not add it to `chat/tools._write_tools()` (the
   MCP adapter already produces the chat side).
4. **chat/tools.py `build_tools`**: `build_tools(settings, mcp=None)` appends
   `build_mcp_tools(mcp)` when given. **chat/engine.py `_answer`**: `await app.mcp.prepare()`
   before `build_tools` (lazy connect, never blocks more than 8 s), pass `app.mcp`; optionally
   add `app.mcp.unavailable()` lines to the system prompt.
5. **"used tools" line** (engine `tool_call` events / UI): show `display_name(name)`
   (`server__tool` -> `server · tool`); the UI can split on the first `__`.
6. **Read-call audit**: `McpManager(audit=hook)`; hook gets `{server, tool, arguments(scrubbed),
   ok, error, duration_ms, source}`. Wire it to whatever chat-tools uses for command audit.
7. **conntest.py `run_test`**: kind `mcp`: validate `McpServerSettings` from the merged body
   (`headers[].value` and `token` are secrets: un-mask against the stored server by name),
   `r = await check_mcp_server(cfg)`, return `ConnResult(r.ok, r.message, r.details)`.
   `details.tools[]` = `{name, title, description, read_only, destructive, kind, schema_tokens,
   selected}` for the tool picker; `details.schema_tokens_total`.
8. **Frontend**: Settings section "MCP servers" (list form from schema) with Test button and a
   tool picker fed by `details.tools` (checkbox -> `tool_filter`; per-tool policy/kind selects ->
   `tool_overrides`); help text says: remote servers only, no stdio, tools only.
9. **Docker image / README**: nothing but the new dependency.
