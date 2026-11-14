"""FastAPI application factory."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__
from .actions.api import router as actions_router
from .collectors import Collector
from .config import Bootstrap, LLMSettings, Settings, settings_json_schema
from .context import AppContext
from .llm import LLMClient, LLMConnectionError, LLMError
from .llm.text import strip_reasoning
from .settings_store import SettingsStore, merge_secrets

JsonBody = Annotated[dict[str, Any], Body()]

SECTION_TITLES = {
    "general": "General",
    "llm": "LLM",
    "report": "Reports",
    "storage": "Data retention",
    "collectors": "Collectors",
    "outputs": "Outputs",
    "arr": "*arr & downloads",
}


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _friendly(err: dict[str, Any]) -> str:
    if err["type"] == "string_pattern_mismatch":
        return "Must be a URL starting with http:// or https://"
    if err["type"] == "string_too_short":
        return "Required"
    msg = str(err["msg"])
    return msg.removeprefix("Value error, ")


def _validation_error(exc: ValidationError) -> HTTPException:
    errors = [
        {"loc": [str(p) for p in err["loc"]], "msg": _friendly(err)}  # type: ignore[arg-type]
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    ]
    return HTTPException(status_code=422, detail=errors)


def create_app(
    bootstrap: Bootstrap | None = None,
    *,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    collector_registry: dict[str, type[Collector]] | None = None,
    seed_settings: Settings | None = None,
    arr_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    bootstrap = bootstrap or Bootstrap()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx = AppContext(
            bootstrap,
            llm_transport=llm_transport,
            collector_registry=collector_registry,
            seed_settings=seed_settings,
            arr_transport=arr_transport,
        )
        app.state.ctx = ctx
        await ctx.start()
        try:
            yield
        finally:
            await ctx.stop()

    app = FastAPI(title="AI Server Monitor", version=__version__, lifespan=lifespan)
    app.include_router(actions_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        ok = await get_ctx(request).db.ping()
        return JSONResponse({"status": "ok" if ok else "error"}, status_code=200 if ok else 503)

    @app.get("/api/info")
    async def info(request: Request) -> dict[str, Any]:
        ctx = get_ctx(request)
        stats = ctx.llm.stats
        llm = ctx.settings.llm
        return {
            "version": __version__,
            "uptime_s": round(time.time() - ctx.started_at, 1),
            "schema_version": await ctx.db.schema_version(),
            "host_root": str(ctx.host_env.root),
            "llm": {
                "configured": llm.configured,
                "base_url": llm.base_url,
                "model": llm.model,
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

    # ------------------------------------------------------------------ collectors

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

    # ------------------------------------------------------------------ settings

    @app.get("/api/settings")
    async def get_settings(request: Request) -> dict[str, Any]:
        """Current values (secrets masked), the JSON schema for the form, section titles."""
        return {
            "values": get_ctx(request).settings.public_dict(),
            "schema": settings_json_schema(),
            "sections": SECTION_TITLES,
        }

    @app.put("/api/settings")
    async def put_settings(request: Request, body: JsonBody) -> dict[str, Any]:
        """Replace all settings. Secrets sent back as the mask keep their stored value.
        Applied immediately (collectors and LLM client are restarted)."""
        ctx = get_ctx(request)
        try:
            new = SettingsStore.apply_update(ctx.settings, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        await ctx.apply_settings(new)
        return {"values": ctx.settings.public_dict()}

    @app.post("/api/settings/test-llm")
    async def test_llm(request: Request, body: JsonBody) -> dict[str, Any]:
        """Try the given (possibly unsaved) LLM settings: list models + a tiny completion."""
        ctx = get_ctx(request)
        current = ctx.settings.llm.model_dump(mode="json", context={"reveal_secrets": True})
        try:
            cfg = LLMSettings.model_validate(merge_secrets(body, current))
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        if not cfg.configured:
            return {"ok": False, "error": "Set the base URL and model first."}
        client = LLMClient(cfg.model_copy(update={"max_retries": 0}), transport=llm_transport)
        models: list[str] | None = None
        try:
            try:
                models = await client.list_models()
            except LLMError:
                models = None  # not all servers implement /models
            completion = await client.complete(
                [{"role": "user", "content": "Reply with the single word OK."}],
                temperature=0,
                max_tokens=256,
            )
            return {
                "ok": True,
                "reply": strip_reasoning(completion.content)[:200],
                "latency_s": round(completion.latency_s, 2),
                "models": models[:100] if models else None,
                "model_listed": (cfg.model in models) if models else None,
            }
        except LLMConnectionError as exc:
            return {"ok": False, "error": f"Could not reach {cfg.base_url}: {exc}"}
        except LLMError as exc:
            return {"ok": False, "error": str(exc), "models": models[:100] if models else None}
        finally:
            await client.aclose()

    static_dir = bootstrap.static_dir
    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="spa")

    return app
