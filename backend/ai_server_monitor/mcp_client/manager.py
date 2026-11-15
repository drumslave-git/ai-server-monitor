"""The set of MCP connections of one settings snapshot.

`McpManager` is built from `Settings.mcp` and rebuilt (the old one closed) when settings
change. It owns one lazy `McpConnection` per enabled server, hands the chat the cached tool
lists, and is the only place calls are made from, so the allow-list is enforced here too
(not just in what the chat is shown) and every call can be reported to an audit hook.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .connection import (
    CallOutcome,
    McpCallError,
    McpConnection,
    McpUnavailable,
    ServerInfo,
    ToolInfo,
)
from .settings import McpServerSettings, McpSettings

log = logging.getLogger(__name__)

PREPARE_WAIT = 8.0
"""How long the chat waits at most for servers to connect before asking its question."""

AuditHook = Callable[[dict[str, Any]], Awaitable[None] | None]
"""Called after every tool call as hook(entry); entry has server, tool, arguments (scrubbed),
ok, error, duration_ms, source."""


class McpManager:
    def __init__(
        self,
        settings: McpSettings,
        *,
        audit: AuditHook | None = None,
        backoff_base: float | None = None,
    ):
        self.settings = settings
        self.audit = audit
        self._backoff_base = backoff_base
        self._conns: dict[str, McpConnection] = {}
        self._closed = False

    # ------------------------------------------------------------------ connections

    def _conn(self, cfg: McpServerSettings) -> McpConnection:
        if self._closed:
            raise McpUnavailable("the settings changed while this was running; ask again")
        conn = self._conns.get(cfg.name)
        if conn is None:
            kwargs = {} if self._backoff_base is None else {"backoff_base": self._backoff_base}
            conn = self._conns[cfg.name] = McpConnection(cfg, **kwargs)
        return conn

    def _enabled(self) -> list[McpServerSettings]:
        return [s for s in self.settings.servers if s.enabled]

    async def prepare(self, wait: float = PREPARE_WAIT) -> None:
        """Connect (lazily, in parallel) the enabled servers that have tools chosen, so that
        the chat can offer them. Failures are not errors: those servers just stay out (see
        `unavailable`). Never takes longer than `wait` seconds."""
        wanted = [s for s in self._enabled() if s.tool_filter and s.default_policy != "off"]
        if not wanted:
            return

        async def one(cfg: McpServerSettings) -> None:
            with contextlib.suppress(McpUnavailable):
                await self._conn(cfg).ensure()

        tasks = [asyncio.create_task(one(s)) for s in wanted]
        _, pending = await asyncio.wait(tasks, timeout=wait)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ what the chat sees

    def tools_of(self, name: str) -> list[ToolInfo] | None:
        """The cached tools of a connected server (None = not connected / not enabled)."""
        conn = self._conns.get(name)
        cfg = self.settings.server(name)
        if conn is None or cfg is None or not cfg.enabled or not conn.connected:
            return None
        return conn.tools

    def unavailable(self) -> list[str]:
        """One line per enabled server with chosen tools that cannot be used right now."""
        notes = []
        for cfg in self._enabled():
            if not cfg.tool_filter or cfg.default_policy == "off":
                continue
            conn = self._conns.get(cfg.name)
            if conn is not None and conn.connected:
                continue
            reason = (conn.last_error if conn else None) or "not connected yet"
            notes.append(f"MCP server {cfg.name!r} is unavailable ({reason})")
        return notes

    def status(self) -> list[dict[str, Any]]:
        out = []
        for cfg in self.settings.servers:
            conn = self._conns.get(cfg.name)
            row = conn.status() if conn else {"name": cfg.name, "state": "idle"}
            row["enabled"] = cfg.enabled
            row["selected"] = len(cfg.tool_filter)
            out.append(row)
        return out

    # ------------------------------------------------------------------ calls

    def allowed(self, server: str, tool: str) -> str | None:
        """Why this call is not allowed (None = allowed): unknown/disabled server, tool not
        chosen, or its policy is off."""
        cfg = self.settings.server(server)
        if cfg is None or not cfg.enabled:
            return f"no enabled MCP server named {server!r}"
        if tool not in cfg.tool_filter:
            return f"{tool!r} is not one of the tools chosen for {server!r}"
        if cfg.policy_of(tool) == "off":
            return f"the policy for {server} · {tool} is off"
        return None

    async def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        source: str | None = None,
        timeout: float | None = None,
    ) -> CallOutcome:
        """Call a chosen tool. Raises McpUnavailable / McpCallError with a message that is
        safe to show; the result text is scrubbed of the server's own secrets."""
        started = time.monotonic()
        error: str | None = None
        try:
            why = self.allowed(server, tool)
            if why:
                raise McpCallError(why)
            cfg = self.settings.server(server)
            assert cfg is not None
            conn = self._conn(cfg)
            await conn.ensure()
            outcome = await conn.call(tool, arguments, timeout=timeout)
            outcome.text = conn.scrub(outcome.text)
            if outcome.is_error:
                error = outcome.text or "the tool reported an error"
            return outcome
        except (McpUnavailable, McpCallError) as exc:
            error = str(exc)
            raise
        finally:
            await self._audit(
                server, tool, arguments, error, started, source, self.settings.server(server)
            )

    async def _audit(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        error: str | None,
        started: float,
        source: str | None,
        cfg: McpServerSettings | None,
    ) -> None:
        if self.audit is None:
            return
        entry = {
            "server": server,
            "tool": tool,
            "arguments": _scrub_value(arguments, cfg.secret_values() if cfg else []),
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

    # ------------------------------------------------------------------ discovery / test

    async def discover(
        self, server: str, *, force: bool = True
    ) -> tuple[ServerInfo, list[ToolInfo]]:
        """Connect now (ignoring backoff when `force`) and return what the server offers."""
        cfg = self.settings.server(server)
        if cfg is None:
            raise McpUnavailable(f"no MCP server named {server!r}")
        conn = self._conn(cfg)
        await conn.ensure(force=force)
        if conn.connected:
            await conn.refresh_tools()
        assert conn.info is not None and conn.tools is not None
        return conn.info, conn.tools

    async def close(self) -> None:
        self._closed = True
        conns, self._conns = list(self._conns.values()), {}
        await asyncio.gather(*(c.close() for c in conns), return_exceptions=True)


def _scrub_value(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {k: _scrub_value(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_value(v, secrets) for v in value]
    return value
