"""A real MCP server for the tests, run in-process on a local port.

Built with the SDK's own server side (`MCPServer`) and served by uvicorn on 127.0.0.1, because
the client's streaming transports need an actual socket. Both transports are available:
Streamable HTTP and the legacy HTTP+SSE. `extra_tools=N` adds N more tools (every third one
writes, some with long descriptions) for the UI and the big-server tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.types import ImageContent, TextContent, ToolAnnotations
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SECRET_IN_RESULT = "hunter2-very-secret"
LONG_DESCRIPTION = (
    "Searches every configured indexer for a release and returns each match with its size, "
    "age, seeders, indexer and the quality profile it would satisfy. Use it before adding a "
    "download when the owner asks whether something is available. The search can take up to a "
    "minute on slow indexers; results are sorted by the indexer's own score, then by seeders. "
    "Filters: category (movies, tv, music), minimum seeders, maximum size in GiB and a free-text "
    "query. Torznab categories are mapped automatically."
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeMcp:
    """`start()` / `stop()`; `url` is the endpoint for the chosen transport."""

    def __init__(
        self,
        transport: str = "streamable_http",
        *,
        token: str | None = None,
        port: int | None = None,
        extra_tools: int = 0,
    ):
        self.transport = transport
        self.token = token
        self.port = port or free_port()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: list[dict[str, str]] = []
        self.mcp = MCPServer("fake-home", version="1.0")
        self._register(extra_tools)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        path = "/mcp" if self.transport == "streamable_http" else "/sse"
        return f"http://127.0.0.1:{self.port}{path}"

    # ------------------------------------------------------------------ the tools

    def _register(self, extra: int) -> None:
        mcp = self.mcp
        read = ToolAnnotations(readOnlyHint=True)

        @mcp.tool(annotations=read, description="Room temperature.")
        def get_temperature(room: str) -> str:
            self.calls.append(("get_temperature", {"room": room}))
            return f"21.5 C in {room}"

        @mcp.tool(description="Turn the heating of a room on or off.")
        def set_heating(room: str, on: bool) -> str:
            self.calls.append(("set_heating", {"room": room, "on": on}))
            return f"heating in {room} is now {'on' if on else 'off'}"

        @mcp.tool(annotations=read)
        async def slow(seconds: float) -> str:
            self.calls.append(("slow", {"seconds": seconds}))
            await asyncio.sleep(seconds)
            return "finally"

        @mcp.tool(annotations=read)
        def boom() -> str:
            raise RuntimeError("the sensor exploded")

        @mcp.tool(annotations=read)
        def leak() -> str:
            return f"the key is {SECRET_IN_RESULT} and the token is {self.token}"

        @mcp.tool(annotations=read)
        def image() -> list[Any]:
            return [
                TextContent(type="text", text="caption"),
                ImageContent(type="image", data="iVBORw0KGgo=", mimeType="image/png"),
            ]

        for i in range(extra):

            def make(i: int = i) -> Any:
                def bulk(x: int = 0) -> str:
                    return f"bulk {i}: {x}"

                return bulk

            description = LONG_DESCRIPTION if i % 10 == 3 else f"Bulk tool number {i}."
            mcp.add_tool(
                make(),
                name=f"bulk_{i:02d}",
                description=description,
                annotations=ToolAnnotations(readOnlyHint=i % 3 != 0),
            )

    # ------------------------------------------------------------------ serving

    def _app(self) -> ASGIApp:
        inner: ASGIApp = (
            self.mcp.streamable_http_app()
            if self.transport == "streamable_http"
            else self.mcp.sse_app()
        )

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                self.headers.append(headers)
                if self.token and headers.get("authorization") != f"Bearer {self.token}":
                    await PlainTextResponse("nope", status_code=401)(scope, receive, send)
                    return
            await inner(scope, receive, send)

        return app

    async def start(self) -> None:
        config = uvicorn.Config(
            self._app(),
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            lifespan="on",
            timeout_graceful_shutdown=1,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                return
            if self._task.done():
                self._task.result()
            await asyncio.sleep(0.025)
        raise RuntimeError("fake MCP server did not start")

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        self._server.force_exit = True
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(self._task, 5)
        self._server = self._task = None
