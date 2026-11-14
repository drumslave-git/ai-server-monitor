"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .collectors import Collector
from .config import Settings, load_settings
from .context import AppContext


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def create_app(
    settings: Settings | None = None,
    *,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    collector_registry: dict[str, type[Collector]] | None = None,
) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx = AppContext.build(
            settings, llm_transport=llm_transport, collector_registry=collector_registry
        )
        app.state.ctx = ctx
        await ctx.start()
        try:
            yield
        finally:
            await ctx.stop()

    app = FastAPI(title="AI Server Monitor", version=__version__, lifespan=lifespan)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        ok = await get_ctx(request).db.ping()
        return JSONResponse({"status": "ok" if ok else "error"}, status_code=200 if ok else 503)

    @app.get("/api/info")
    async def info(request: Request) -> dict[str, Any]:
        ctx = get_ctx(request)
        stats = ctx.llm.stats
        return {
            "version": __version__,
            "uptime_s": round(time.time() - ctx.started_at, 1),
            "schema_version": await ctx.db.schema_version(),
            "llm": {
                "base_url": settings.llm.base_url,
                "model": settings.llm.model,
                "tool_mode": ctx.tool_mode.status(),
                "stats": {
                    "requests": stats.requests,
                    "errors": stats.errors,
                    "prompt_tokens": stats.prompt_tokens,
                    "completion_tokens": stats.completion_tokens,
                    "last_latency_s": stats.last_latency_s,
                    "last_error": stats.last_error,
                    "last_success": stats.last_success,
                },
            },
            "jobs": ctx.scheduler.status(),
            "collectors": ctx.collectors.status(),
        }

    @app.get("/api/collectors")
    async def collectors(request: Request) -> dict[str, Any]:
        ctx = get_ctx(request)
        return {
            name: {
                **status,
                "result": r.to_dict() if (r := ctx.collectors.latest.get(name)) else None,
            }
            for name, status in ctx.collectors.status().items()
        }

    @app.get("/api/collectors/{name}")
    async def collector(name: str, request: Request) -> dict[str, Any]:
        ctx = get_ctx(request)
        if name not in ctx.collectors.registry:
            raise HTTPException(status_code=404, detail=f"unknown collector {name!r}")
        result = ctx.collectors.latest.get(name)
        return {**ctx.collectors.status()[name], "result": result.to_dict() if result else None}

    static_dir = settings.server.static_dir
    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="spa")

    return app
