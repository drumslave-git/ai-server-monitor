"""A real MCP server for the tests, run in-process on a local port.

Built with the SDK's own server side (FastMCP) and served by uvicorn on 127.0.0.1, because
the client's streaming transports need an actual socket (an in-memory ASGI transport buffers
whole responses). Both transports are available: Streamable HTTP and the legacy HTTP+SSE.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from typing import Any

import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

SECRET_IN_RESULT = "hunter2-very-secret"


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
    ):
        self.transport = transport
        self.token = token
        self.port = port or free_port()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: list[dict[str, str]] = []
        self.mcp = FastMCP("fake-home", host="127.0.0.1", port=self.port)
        self._register()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def url(self) -> str:
        path = "/mcp" if self.transport == "streamable_http" else "/sse"
        return f"http://127.0.0.1:{self.port}{path}"

    # ------------------------------------------------------------------ the tools

    def _register(self) -> None:
        mcp = self.mcp

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), description="Room temperature.")
        def get_temperature(room: str) -> str:
            self.calls.append(("get_temperature", {"room": room}))
            return f"21.5 C in {room}"

        @mcp.tool(description="Turn the heating of a room on or off.")
        def set_heating(room: str, on: bool) -> str:
            self.calls.append(("set_heating", {"room": room, "on": on}))
            return f"heating in {room} is now {'on' if on else 'off'}"

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        async def slow(seconds: float) -> str:
            self.calls.append(("slow", {"seconds": seconds}))
            await asyncio.sleep(seconds)
            return "finally"

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def boom() -> str:
            raise RuntimeError("the sensor exploded")

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def leak() -> str:
            return f"the key is {SECRET_IN_RESULT} and the token is {self.token}"

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        async def add_tool(ctx: Context) -> str:  # type: ignore[type-arg]
            def extra() -> str:
                return "extra!"

            mcp.add_tool(extra, name="extra", description="Added at runtime.")
            await ctx.session.send_tool_list_changed()
            return "added"

        @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def image() -> list[Any]:
            from mcp.types import ImageContent, TextContent

            return [
                TextContent(type="text", text="caption"),
                ImageContent(type="image", data="iVBORw0KGgo=", mimeType="image/png"),
            ]

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
