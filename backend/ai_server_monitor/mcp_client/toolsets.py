"""MCP servers as Pydantic AI toolsets (`pydantic_ai.mcp.MCPToolset`, built on FastMCP).

Nothing here is long-lived: every agent run builds its own `McpRun` from the current settings
and Pydantic AI connects the servers when the run starts and closes them when it ends.

- The tools the owner chose (`tool_filter`) are given to the model directly, named
  `<server>_<tool>` (server names have no `_`, so the split is unambiguous).
- A tool is read when the server marks it `readOnlyHint` and the owner did not override it;
  everything else writes. In the chat a write tool is approval-required (Pydantic AI defers
  it and the chat files it as an `mcp_call` action); in a job it files the action itself and
  answers the model, since nobody waits on a scheduled run.
- `Reachable` is the one thing the library does not do: a server that cannot be reached, or
  dies during the run, gives no tools instead of failing the run; its reason is kept.
- Results are scrubbed of the server's own token and header values, and every call goes to
  the audit hook (the chat's audit list); the agent runtime caps them, records them in the
  Logs page and wraps them as untrusted data like any other tool result.
"""

from __future__ import annotations

import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from pydantic_ai import RunContext
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset

from .settings import McpServerSettings, McpSettings

log = logging.getLogger(__name__)

SEPARATOR = "_"
Kind = Literal["read", "write"]
AuditHook = Callable[[dict[str, Any]], Awaitable[None] | None]
"""Called after every tool call with server, tool, arguments (scrubbed), ok, error,
duration_ms, source."""

WRITE_NOTE = (
    "WRITE: this changes something on {server}. Only call it when the owner clearly asked for "
    "it; it goes through the owner's approval policy, so report exactly what the result says."
)


def split_name(name: str) -> tuple[str, str]:
    """`server_tool` → (server, tool)."""
    server, _, tool = name.partition(SEPARATOR)
    return server, tool


def kind_of(cfg: McpServerSettings, tool: str, annotations: dict[str, Any] | None) -> Kind:
    """read only if the owner says so, or (without an override) the server marks it read-only."""
    override = cfg.override(tool)
    if override is not None and override.kind != "auto":
        return override.kind
    return "read" if (annotations or {}).get("readOnlyHint") is True else "write"


def def_kind(cfg: McpServerSettings, tool_def: ToolDefinition) -> Kind:
    return kind_of(cfg, tool_def.name, (tool_def.metadata or {}).get("annotations"))


def server_toolset(cfg: McpServerSettings, process: Any = None) -> MCPToolset[Any]:
    """The library's toolset for one server, from its settings. `auto` lets FastMCP choose the
    transport from the URL (`…/sse` → HTTP+SSE, else Streamable HTTP)."""
    url = str(cfg.url)
    token = cfg.token.get_secret_value().strip() or None
    headers = {h.name: h.value.get_secret_value() for h in cfg.headers} or None
    timeout = cfg.timeout.total_seconds()
    common: dict[str, Any] = {
        "id": f"mcp-{cfg.name}",
        "init_timeout": timeout,
        "read_timeout": timeout,
        "tool_error_behavior": "failed",
        "process_tool_call": process,
    }
    if cfg.transport == "auto":
        return MCPToolset(url, auth=token, headers=headers, **common)
    transport_cls = SSETransport if cfg.transport == "sse" else StreamableHttpTransport
    return MCPToolset(transport_cls(url, headers=headers, auth=token), **common)


def transport_label(cfg: McpServerSettings) -> str:
    sse = cfg.transport == "sse" or (
        cfg.transport == "auto" and str(cfg.url).rstrip("/").endswith("/sse")
    )
    return "HTTP+SSE" if sse else "Streamable HTTP"


def failure_text(exc: BaseException, cfg: McpServerSettings) -> str:
    """A connect or call failure in words, without the server's secrets."""
    leaves: list[BaseException] = [exc]
    while isinstance(leaves[0], BaseExceptionGroup) and leaves[0].exceptions:
        leaves = list(leaves[0].exceptions)
    text = str(leaves[0]).strip() or type(leaves[0]).__name__
    return scrub(text, cfg.secret_values())


def scrub(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    return value


@dataclass
class Reachable(WrapperToolset[Any]):
    """Connects its server when the run starts; a server that cannot be reached (or fails
    while the run closes it) gives no tools and keeps the reason in `down`."""

    cfg: McpServerSettings | None = None
    down: str | None = None
    seen: dict[str, ToolDefinition] = field(default_factory=dict)
    view: Any = None
    """What the agent gets: the chosen tools, prefixed, writes guarded."""
    _depth: int = 0
    _up: bool = False

    async def __aenter__(self) -> Reachable:
        self._depth += 1
        if self._depth == 1:
            started = time.monotonic()
            try:
                await self.wrapped.__aenter__()
                self._up, self.down = True, None
            except Exception as exc:
                assert self.cfg is not None
                took = time.monotonic() - started
                self.down = f"{failure_text(exc, self.cfg)} (after {took:.1f} s)"
                log.warning("MCP server %r is unavailable: %s", self.cfg.name, self.down)
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        self._depth -= 1
        if self._depth == 0 and self._up:
            self._up = False
            try:
                await self.wrapped.__aexit__(*args)
            except Exception as exc:  # the server died during the run: nothing to close
                log.info("MCP server close failed: %s", type(exc).__name__)
        return None

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, Any]:
        if not self._up:
            return {}
        try:
            tools = await self.wrapped.get_tools(ctx)
        except Exception as exc:
            assert self.cfg is not None
            self.down = failure_text(exc, self.cfg)
            return {}
        self.seen = {name: tool.tool_def for name, tool in tools.items()}
        return tools


@dataclass
class McpRun:
    """The MCP servers of one agent run: `toolsets` for the agent, `kind(name)` for the chat's
    labels, `unavailable()` for the notes."""

    servers: list[Reachable]

    @property
    def toolsets(self) -> list[AbstractToolset[Any]]:
        return [s.view for s in self.servers]

    def kind(self, name: str) -> Kind | None:
        server, tool = split_name(name)
        for reach in self.servers:
            if reach.cfg is not None and reach.cfg.name == server and tool in reach.seen:
                return def_kind(reach.cfg, reach.seen[tool])
        return None

    def unavailable(self) -> list[str]:
        return [
            f"MCP server {r.cfg.name!r} is unavailable this run ({r.down})."
            for r in self.servers
            if r.down is not None and r.cfg is not None
        ]


WriteCall = Callable[[McpServerSettings, str, dict[str, Any]], Awaitable[Any]]
"""Jobs: files a write call as an action and returns what the model is told."""


class McpServers:
    """The MCP part of one settings snapshot (`context.py` rebuilds it on every save)."""

    def __init__(self, settings: McpSettings, *, audit: AuditHook | None = None):
        self.settings = settings
        self.audit = audit

    def _enabled(self) -> list[McpServerSettings]:
        return [s for s in self.settings.servers if s.enabled and s.tool_filter]

    def run(
        self,
        source: str,
        *,
        write_call: WriteCall | None = None,
        only: set[str] | None = None,
        sink: Callable[[str], None] | None = None,
    ) -> McpRun:
        """Fresh toolsets for one run. Without `write_call` (the chat) write tools are
        approval-required; with it (jobs) a write tool call goes to `write_call` instead.
        `only`: just these servers. `sink` gets the text of every result (a job's evidence)."""
        servers = []
        for cfg in self._enabled():
            if only is not None and cfg.name not in only:
                continue
            reach = Reachable(server_toolset(cfg, self._process(cfg, source, sink)), cfg=cfg)
            chosen = set(cfg.tool_filter)
            view: AbstractToolset[Any] = reach.filtered(
                lambda ctx, td, chosen=chosen: td.name in chosen
            ).prepared(_describe(cfg))
            if write_call is None:
                view = view.approval_required(
                    lambda ctx, td, args, cfg=cfg: def_kind(cfg, td) == "write"
                )
            else:
                view = JobWrites(view, cfg=cfg, write_call=write_call)
            reach.view = view.prefixed(cfg.name)
            servers.append(reach)
        return McpRun(servers)

    def _process(
        self, cfg: McpServerSettings, source: str, sink: Callable[[str], None] | None = None
    ) -> Any:
        secrets = cfg.secret_values()

        async def process(
            ctx: RunContext[Any], call_tool: Any, name: str, args: dict[str, Any]
        ) -> Any:
            started, error = time.monotonic(), None
            try:
                result = scrub(await call_tool(name, args), secrets)
                if sink is not None:
                    sink(result if isinstance(result, str) else json.dumps(result, default=str))
                return result
            except Exception as exc:
                error = failure_text(exc, cfg)
                raise
            finally:
                await self.record(cfg, name, args, error, started, source)

        return process

    async def record(
        self,
        cfg: McpServerSettings,
        tool: str,
        args: dict[str, Any],
        error: str | None,
        started: float,
        source: str,
    ) -> None:
        if self.audit is None:
            return
        entry = {
            "server": cfg.name,
            "tool": tool,
            "arguments": scrub(args, cfg.secret_values()),
            "ok": error is None,
            "error": error,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "source": source,
        }
        try:
            outcome = self.audit(entry)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            log.exception("mcp audit hook failed")

    async def call(self, server: str, tool: str, args: dict[str, Any], source: str) -> Any:
        """One call outside an agent run (an approved `mcp_call` action). Raises with a
        message that is safe to show; the result is scrubbed."""
        cfg = self.settings.server(server)
        if cfg is None:
            raise RuntimeError(f"no MCP server named {server!r}")
        started, error = time.monotonic(), None
        try:  # (`direct_call_tool` does not go through `process_tool_call`)
            return scrub(
                await server_toolset(cfg).direct_call_tool(tool, args), cfg.secret_values()
            )
        except Exception as exc:
            error = failure_text(exc, cfg)
            raise RuntimeError(error) from None
        finally:
            await self.record(cfg, tool, args, error, started, source)


def _describe(cfg: McpServerSettings) -> Callable[..., Awaitable[list[ToolDefinition]]]:
    """Write tools get the WRITE note in front of the server's own (whole) description."""

    async def prepare(ctx: RunContext[Any], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        out = []
        for td in defs:
            if def_kind(cfg, td) == "write":
                note = WRITE_NOTE.format(server=cfg.name)
                td = replace(td, description=f"{note}\n{td.description or ''}")
            out.append(td)
        return out

    return prepare


@dataclass
class JobWrites(WrapperToolset[Any]):
    """In a job run, a write tool is not called: `write_call` files it as an action (the
    server's policy decides) and its answer goes to the model."""

    cfg: McpServerSettings | None = None
    write_call: WriteCall | None = None

    async def call_tool(self, name: str, tool_args: dict[str, Any], ctx: Any, tool: Any) -> Any:
        assert self.cfg is not None and self.write_call is not None
        if def_kind(self.cfg, tool.tool_def) == "write":
            return await self.write_call(self.cfg, name, tool_args)
        return await super().call_tool(name, tool_args, ctx, tool)
