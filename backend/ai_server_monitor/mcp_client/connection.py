"""One connection to one MCP server, built on the official `mcp` SDK.

The SDK's transports are async context managers that own task groups, so a connection
lives in one background task (the *runner*): it enters the transport and the session,
runs `initialize` + `tools/list`, reports "ready", and then just waits until it is told to
close (or the transport dies). Calls are made from other tasks on the same session.

Connections are lazy and self-healing: `ensure()` connects when needed; after a failure the
next attempt is delayed with exponential backoff (a clear "unreachable, retrying in Ns"
error in the meantime) so a dead server costs the chat nothing.

Credentials: the bearer token and header values are only ever handed to the transport.
Error texts are built from failure kinds and server-authored messages, never from request
URLs or headers, and run through the secret scrubber on top.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import anyio
import httpx
from mcp import ClientSession, McpError, types
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from .settings import McpServerSettings

log = logging.getLogger(__name__)
# The SDK logs whole JSON-RPC messages (tool arguments, results) at DEBUG: keep them out of the
# service log even when the service itself runs at debug level.
logging.getLogger("mcp").setLevel(logging.INFO)

CLIENT_NAME = "ai-server-monitor"
SSE_READ_TIMEOUT = 300.0
CLOSE_TIMEOUT = 5.0
BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0


class McpUnavailable(Exception):
    """The server cannot be used right now; the message says why and goes to the model/UI."""


class McpCallError(Exception):
    """A call failed (timeout, protocol error, server error); the message is safe to show."""


@dataclass(frozen=True)
class ServerInfo:
    name: str
    version: str
    protocol: str
    instructions: str | None = None


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any]
    title: str | None = None
    read_only: bool | None = None
    """The server's `readOnlyHint` (None = not stated, which counts as "may write")."""
    destructive: bool | None = None

    @classmethod
    def from_sdk(cls, tool: types.Tool) -> ToolInfo:
        hints = tool.annotations
        return cls(
            name=tool.name,
            description=(tool.description or "").strip(),
            input_schema=dict(tool.inputSchema or {}),
            title=(hints.title if hints and hints.title else None) or tool.title,
            read_only=hints.readOnlyHint if hints else None,
            destructive=hints.destructiveHint if hints else None,
        )


@dataclass
class CallOutcome:
    is_error: bool
    text: str
    structured: Any = None
    extra: list[str] = field(default_factory=list)
    """Notes about content that is not text (images, audio, resources)."""

    def payload(self, limit: int = 6000) -> dict[str, Any]:
        """The result as the model (or the action log) sees it: text capped at `limit`
        characters (head kept), structured content only when there is no text, plus notes
        about non-text parts."""
        out: dict[str, Any] = {}
        text = self.text
        if len(text) > limit:
            text = f"{text[:limit]}…[truncated, {len(self.text) - limit} more characters]"
        if text:
            out["text"] = text
        elif self.structured is not None:
            out["structured"] = self.structured
        if self.extra:
            out["not_shown"] = self.extra
        return out or {"text": "(the tool returned nothing)"}


def outcome_from_result(result: types.CallToolResult) -> CallOutcome:
    parts: list[str] = []
    extra: list[str] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        elif isinstance(block, types.ImageContent):
            extra.append(f"[image {block.mimeType}, {_b64_size(block.data)} bytes not shown]")
        elif isinstance(block, types.AudioContent):
            extra.append(f"[audio {block.mimeType}, {_b64_size(block.data)} bytes not shown]")
        elif isinstance(block, types.EmbeddedResource):
            resource = block.resource
            if isinstance(resource, types.TextResourceContents):
                parts.append(resource.text)
            else:
                extra.append(f"[binary resource {resource.uri} not shown]")
        elif isinstance(block, types.ResourceLink):
            extra.append(f"[resource link {block.uri}]")
    return CallOutcome(
        is_error=bool(result.isError),
        text="\n".join(parts).strip(),
        structured=result.structuredContent,
        extra=extra,
    )


def _b64_size(data: str) -> int:
    return len(data) * 3 // 4


# --------------------------------------------------------------------------- errors


def leaves(exc: BaseException) -> list[BaseException]:
    """The real exceptions inside (nested) exception groups, as anyio task groups raise."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in leaves(sub)]
    return [exc]


def _is_network_error(exc: BaseException) -> bool:
    return isinstance(exc, httpx.TransportError | OSError | TimeoutError)


def describe_failure(exc: BaseException, url: str) -> str:
    """A plain-words reason for a failed connect/call. Built from the failure kind, never
    from the exception text (which can contain URLs with credentials)."""
    found = [e for e in leaves(exc) if not isinstance(e, asyncio.CancelledError)] or [exc]
    for e in found:
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            if code in (401, 403):
                return (
                    f"The server rejected the credentials (HTTP {code}). "
                    "Check the token and headers."
                )
            if code == 404:
                return "The server answered HTTP 404: check the URL path (is it the MCP endpoint?)."
            return f"The server answered HTTP {code}."
        if isinstance(e, McpError):
            return f"The server returned an error: {e.error.message}"
    for e in found:
        if _is_network_error(e):
            if isinstance(e, TimeoutError | httpx.TimeoutException):
                return "The server did not answer in time."
            from ..conntest import describe_error  # lazy: conntest is a sibling module

            return describe_error(e, url)
    if any(
        isinstance(e, anyio.ClosedResourceError | anyio.BrokenResourceError | anyio.EndOfStream)
        for e in found
    ):
        return "The connection to the server was lost."
    kind = type(found[0]).__name__
    return f"The MCP exchange with the server failed ({kind}). Is this really an MCP server URL?"


def _wants_fallback(exc: BaseException) -> bool:
    """After a failed Streamable HTTP attempt: is the legacy SSE transport worth a try?
    Not for network failures (same host) or rejected credentials (same credentials)."""
    for e in leaves(exc):
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
            return False
        if _is_network_error(e):
            return False
    return True


def _client_version() -> str:
    try:
        from importlib.metadata import version

        return version("ai-server-monitor")
    except Exception:
        return "0"


# --------------------------------------------------------------------------- connection

ToolsChanged = Callable[[str], None]


class McpConnection:
    def __init__(
        self,
        cfg: McpServerSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        backoff_base: float = BACKOFF_BASE,
        backoff_max: float = BACKOFF_MAX,
        on_tools_changed: ToolsChanged | None = None,
    ):
        self.cfg = cfg
        self._clock = clock
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._on_tools_changed = on_tools_changed
        self._lock = asyncio.Lock()
        self._runner: asyncio.Task[None] | None = None
        self._closing = asyncio.Event()
        self._session: ClientSession | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self.info: ServerInfo | None = None
        self.tools: list[ToolInfo] | None = None
        """Cached `tools/list` (None = never fetched)."""
        self.tools_version = 0
        """Bumped whenever the cached tool list was replaced."""
        self.transport: str | None = None
        """The transport that worked: streamable_http | sse."""
        self.last_error: str | None = None
        self.failures = 0
        self._retry_at = 0.0

    # ------------------------------------------------------------------ state

    @property
    def connected(self) -> bool:
        return self._session is not None and self._runner is not None and not self._runner.done()

    @property
    def state(self) -> str:
        if self.connected:
            return "connected"
        if self.last_error:
            return "unreachable"
        return "idle"

    def retry_in(self) -> float:
        return max(0.0, self._retry_at - self._clock())

    def status(self) -> dict[str, Any]:
        return {
            "name": self.cfg.name,
            "state": self.state,
            "transport": self.transport,
            "server": (
                {"name": self.info.name, "version": self.info.version} if self.info else None
            ),
            "tools": len(self.tools) if self.tools is not None else None,
            "error": self.last_error,
            "retry_in_s": round(self.retry_in()) if not self.connected and self.last_error else 0,
        }

    # ------------------------------------------------------------------ connect

    async def ensure(self, *, force: bool = False) -> None:
        """Connect if needed. Raises McpUnavailable (with the reason) when that fails or
        when a recent failure's backoff has not passed yet (unless `force`)."""
        async with self._lock:
            if self.connected:
                return
            if self._runner is not None:  # died since the last call
                await self._stop_runner()
            if not force and self._retry_at > self._clock() and self.last_error:
                raise McpUnavailable(self._unreachable())
            await self._connect()

    def _unreachable(self) -> str:
        return (
            f"MCP server {self.cfg.name!r} is unreachable: {self.last_error} "
            f"(retrying in {max(1, round(self.retry_in()))}s)"
        )

    async def _connect(self) -> None:
        timeout = self.cfg.timeout.total_seconds()
        self._closing = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._runner = asyncio.create_task(self._run(ready), name=f"mcp:{self.cfg.name}")
        try:
            # connect + initialize + tools/list (the auto fallback gets a second attempt)
            async with asyncio.timeout(timeout * 2 + 1):
                await ready
        except BaseException as exc:
            await self._stop_runner()
            if isinstance(exc, asyncio.CancelledError):
                raise
            message = (
                "The server did not answer in time."
                if isinstance(exc, TimeoutError) and not ready.done()
                else describe_failure(exc, self.cfg.url)
            )
            self._failed(message)
            raise McpUnavailable(self._unreachable()) from None
        self.failures = 0
        self.last_error = None
        self._retry_at = 0.0

    def _failed(self, message: str, *, backoff: bool = True) -> None:
        self.last_error = self.scrub(message)
        if backoff:
            self.failures += 1
            delay = min(self._backoff_base * 2 ** (self.failures - 1), self._backoff_max)
            self._retry_at = self._clock() + delay
        else:
            self._retry_at = 0.0

    async def _run(self, ready: asyncio.Future[None]) -> None:
        kinds = ["streamable_http", "sse"] if self.cfg.transport == "auto" else [self.cfg.transport]
        first: BaseException | None = None
        for kind in kinds:
            try:
                await self._lifetime(kind, ready)
                return
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
                raise
            except BaseException as exc:
                if ready.done():  # it worked, then the transport died
                    self._session = None
                    self._failed(describe_failure(exc, self.cfg.url), backoff=False)
                    log.info("mcp %s: connection lost: %s", self.cfg.name, self.last_error)
                    return
                first = first or exc
                if kind == kinds[-1] or not _wants_fallback(exc):
                    break
        assert first is not None
        if not ready.done():
            ready.set_exception(first)

    async def _lifetime(self, kind: str, ready: asyncio.Future[None]) -> None:
        cfg = self.cfg
        timeout = cfg.timeout.total_seconds()
        headers = cfg.request_headers()
        async with AsyncExitStack() as stack:
            if kind == "streamable_http":
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers=headers, timeout=httpx.Timeout(timeout, read=SSE_READ_TIMEOUT)
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(cfg.url, http_client=client)
                )
            else:
                read, write = await stack.enter_async_context(
                    sse_client(
                        cfg.url, headers=headers, timeout=timeout, sse_read_timeout=SSE_READ_TIMEOUT
                    )
                )
            session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=timeout),
                    message_handler=self._on_message,
                    client_info=types.Implementation(name=CLIENT_NAME, version=_client_version()),
                )
            )
            init = await session.initialize()
            tools = await _list_all_tools(session)
            self._session = session
            self.transport = kind
            self.info = ServerInfo(
                name=init.serverInfo.name,
                version=init.serverInfo.version,
                protocol=str(init.protocolVersion),
                instructions=init.instructions,
            )
            self._set_tools(tools)
            if not ready.done():
                ready.set_result(None)
            await self._closing.wait()

    async def _stop_runner(self) -> None:
        runner, self._runner = self._runner, None
        self._session = None
        self._closing.set()
        if runner is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(runner), CLOSE_TIMEOUT)
        except (TimeoutError, asyncio.CancelledError):
            runner.cancel()
            with contextlib.suppress(BaseException):
                await runner
        except BaseException:
            pass  # the runner reports its own failures

    async def close(self) -> None:
        async with self._lock:
            await self._stop_runner()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        self._background.clear()

    async def drop(self, reason: str) -> None:
        """Throw the connection away (a call timed out or broke it); the next use reconnects."""
        async with self._lock:
            await self._stop_runner()
            self._failed(reason, backoff=False)

    # ------------------------------------------------------------------ tools

    def _set_tools(self, tools: list[ToolInfo]) -> None:
        self.tools = tools
        self.tools_version += 1
        if self._on_tools_changed is not None:
            try:
                self._on_tools_changed(self.cfg.name)
            except Exception:
                log.exception("mcp tools-changed callback failed")

    async def refresh_tools(self) -> list[ToolInfo]:
        session = self._session
        if session is None:
            raise McpUnavailable(f"MCP server {self.cfg.name!r} is not connected")
        tools = await _list_all_tools(session)
        self._set_tools(tools)
        return tools

    async def _on_message(self, message: Any) -> None:
        if isinstance(message, Exception):
            log.debug("mcp %s: transport reported %s", self.cfg.name, type(message).__name__)
            return
        root = getattr(message, "root", None)
        if isinstance(root, types.ToolListChangedNotification):
            # Not awaited here: this runs inside the session's receive loop, which must
            # stay free to deliver the answer to the very request we are about to send.
            task = asyncio.create_task(self._refresh_quietly(), name=f"mcp-refresh:{self.cfg.name}")
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _refresh_quietly(self) -> None:
        try:
            await self.refresh_tools()
        except Exception as exc:
            log.info("mcp %s: tools/list refresh failed (%s)", self.cfg.name, type(exc).__name__)

    # ------------------------------------------------------------------ calls

    async def call(
        self, tool: str, arguments: dict[str, Any], *, timeout: float | None = None
    ) -> CallOutcome:
        """Call a tool. Raises McpUnavailable (not connected) or McpCallError."""
        session = self._session
        if session is None or not self.connected:
            raise McpUnavailable(f"MCP server {self.cfg.name!r} is not connected")
        runner = self._runner
        assert runner is not None
        limit = timeout if timeout is not None else self.cfg.timeout.total_seconds()
        # Raced against the runner: when the transport dies the SDK leaves the request
        # waiting for an answer that never comes, so we notice the death ourselves.
        pending = asyncio.ensure_future(
            session.call_tool(tool, arguments, read_timeout_seconds=timedelta(seconds=limit))
        )
        try:
            done, _ = await asyncio.wait(
                {pending, runner}, timeout=limit + 1, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            pending.cancel()
            raise
        if pending not in done:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            if runner in done:
                raise McpCallError(self.last_error or "The connection to the server was lost.")
            await self.drop(f"{tool} timed out after {limit:.0f}s")
            raise McpCallError(f"{tool} timed out after {limit:.0f}s")
        try:
            result = pending.result()
        except McpError as exc:
            if exc.error.code == httpx.codes.REQUEST_TIMEOUT:
                await self.drop(f"{tool} timed out after {limit:.0f}s")
                raise McpCallError(f"{tool} timed out after {limit:.0f}s") from None
            if exc.error.code == types.CONNECTION_CLOSED:
                message = self.last_error or "The connection to the server was lost."
                await self.drop(message)
                raise McpCallError(self.scrub(message)) from None
            raise McpCallError(
                self.scrub(f"The server returned an error: {exc.error.message}")
            ) from None
        except Exception as exc:
            message = describe_failure(exc, self.cfg.url)
            await self.drop(message)
            raise McpCallError(self.scrub(message)) from None
        return outcome_from_result(result)

    # ------------------------------------------------------------------ helpers

    def scrub(self, text: str) -> str:
        for secret in sorted(self.cfg.secret_values(), key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        return text


async def _list_all_tools(session: ClientSession) -> list[ToolInfo]:
    tools: list[ToolInfo] = []
    cursor: str | None = None
    for _ in range(20):  # a server that never ends its pages is not worth waiting for
        page = await session.list_tools(cursor)
        tools.extend(ToolInfo.from_sdk(t) for t in page.tools)
        cursor = page.nextCursor
        if not cursor:
            break
    return tools


def schema_tokens(schema: Any) -> int:
    """Rough token count (4 characters each) of a JSON-serializable definition."""
    return round(len(json.dumps(schema, separators=(",", ":"), default=str)) / 4)
