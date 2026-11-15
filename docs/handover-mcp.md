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

## How it is wired (landed)

- `config.py`: `Settings.mcp` (`McpSettings`, `McpServerSettings`, `McpToolOverride`,
  `McpHeader`); `mcp_client/settings.py` re-exports them. `mcp.dry_run` is its own switch.
- `context.py`: `app.mcp` (an `McpManager`, rebuilt and the old one closed on every settings
  change); `ActionService(mcp=app.mcp)`; read/executed calls are audited with
  `chat.audit.audit_hook(db, "mcp")` (`GET /api/chat/audit?kind=mcp`).
- `actions/registry.py` + `service.py`: `ActionDef` got optional hooks `policy_of(settings, args)`,
  `max_per_hour_of(settings, args)`, `uses_dry_run(settings)`, `run(service, args)` and
  `chat_tool=False` (documented in its docstring; the *arr actions use none of them and are
  unchanged). `mcp_call` is registered through them. api-tools can register `api_call` the same
  way: `policy_of`/`max_per_hour_of` to read its own settings, `run` or the old `execute`.
- `chat/tools.py`: `build_tools(settings, mcp)` adds `build_mcp_tools(mcp)`. `chat/engine.py`:
  `await app.mcp.prepare()` (lazy connect, at most 8 s, dead servers just stay out) and prompt
  notes (`_mcp_notes`: servers that are down; with the generic pair also which servers/tools exist,
  without that the 4B model never thinks of asking). `chat/prompts.py`: `MCP_NOTE`.
- `conntest.py`: kind `mcp` -> `check_mcp_server`; the Settings Test button shows server, version,
  transport; `details.tools[]` feeds the tool picker (`frontend/src/components/McpTools.tsx`),
  which edits `tool_filter` / `tool_overrides` and keeps the test result while you tick.
- Not done / ideas: the "used tools" line shows the raw `server__tool` name (use
  `mcp_client.display_name` where it is rendered); the generic `mcp_call` has kind `write` in
  events even for read calls; resources/prompts/sampling are not supported; connections are not
  kept across settings changes (rebuilt, lazily reconnected).
- Live proof: `docs/samples/mcp-live.md`, screenshot `docs/samples/ui/settings-mcp.png`,
  script `scripts/dev/mcp_demo.py`.
