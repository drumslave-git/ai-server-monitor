"""FastAPI application factory."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__, api_models
from .actions.api import router as actions_router
from .agent.models import worker_config
from .chat.api import router as chat_router
from .chat.engine import change_shell_offered
from .config import (
    Bootstrap,
    LLMSettings,
    Settings,
    WorkerLLMSettings,
    pattern_problem,
    settings_json_schema,
)
from .conntest import KINDS as TEST_KINDS
from .conntest import run_test
from .context import AppContext
from .hardware import hardware as read_hardware
from .jobs.api import router as jobs_router
from .jobs.status import job_status
from .llm.check import check_model
from .llm.modelinfo import read_model_info
from .mcp_client.api import router as mcp_router
from .mcp_client.api import sort_tested
from .outputs.sse import SSE_HEADERS, event_stream, parse_types
from .settings_store import SettingsStore, merge_secrets
from .tracing.api import router as logs_router

JsonBody = Annotated[dict[str, Any], Body()]

SECTION_TITLES = {
    "general": "General",
    "llm": "Main model",
    "worker": "Worker model",
    "chat": "Chat",
    "jobs": "Jobs",
    "storage": "Data retention",
    "logs": "Logs",
    "approvals": "Approvals",
    "outputs": "Outputs",
    "mcp": "MCP servers",
}


def _build_commit() -> str | None:
    """Short git commit the image was built from, None for dev runs / unknown builds."""
    raw = os.environ.get("ASM_BUILD_COMMIT", "").strip()
    return None if not raw or raw == "unknown" else raw[:7]


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _effective_worker(ctx: AppContext, own: LLMSettings) -> LLMSettings:
    """The given (possibly unsaved) worker settings as the agents would use them."""
    worker = WorkerLLMSettings.model_validate(own.model_dump())
    return worker_config(ctx.settings.model_copy(update={"worker": worker}))


def _friendly(err: dict[str, Any]) -> str:
    if err["type"] == "string_pattern_mismatch":
        problem = pattern_problem(err)
        if problem.startswith("must start"):
            return "Must be a URL starting with http:// or https://"
        return "Required" if problem == "is required" else problem.replace("may", "May", 1)
    if err["type"] == "string_too_short":
        return "Required"
    msg = str(err["msg"])
    return msg.removeprefix("Value error, ")


def _validation_error(exc: ValidationError) -> HTTPException:
    errors = [
        {"loc": [str(p) for p in err["loc"]], "msg": _friendly(err)}  # type: ignore[arg-type]
        for err in exc.errors(include_url=False, include_context=False)  # input: not sent back
    ]
    return HTTPException(status_code=422, detail=errors)


def create_app(
    bootstrap: Bootstrap | None = None,
    *,
    llm_transport: httpx.AsyncBaseTransport | None = None,
    seed_settings: Settings | None = None,
    telegram_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    bootstrap = bootstrap or Bootstrap()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx = AppContext(
            bootstrap,
            llm_transport=llm_transport,
            seed_settings=seed_settings,
            telegram_transport=telegram_transport,
        )
        app.state.ctx = ctx
        app.state.tracing = ctx.tracing
        await ctx.start()
        try:
            yield
        finally:
            await ctx.stop()

    app = FastAPI(title="AI Server Monitor", version=__version__, lifespan=lifespan)
    app.include_router(actions_router)
    app.include_router(chat_router)
    app.include_router(jobs_router)
    app.include_router(logs_router)
    app.include_router(mcp_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        ok = await get_ctx(request).db.ping()
        return JSONResponse({"status": "ok" if ok else "error"}, status_code=200 if ok else 503)

    @app.get("/api/info")
    async def info(request: Request) -> dict[str, Any]:
        ctx = get_ctx(request)
        llm = ctx.settings.llm
        return {
            "version": __version__,
            "commit": _build_commit(),
            "uptime_s": round(time.time() - ctx.started_at, 1),
            "schema_version": await ctx.db.schema_version(),
            "host_root": str(ctx.host_env.root),
            "llm": {
                "configured": llm.configured,
                "base_url": llm.base_url,
                "model": llm.model,
                "worker_model": ctx.models.name("worker"),
            },
            "automation": await ctx.automation_status(),
            "jobs": ctx.schedule.status(),
        }

    @app.get("/api/automation")
    async def automation(request: Request) -> dict[str, Any]:
        """The global pause switch: paused, since when, by whom, and what waits because of it."""
        return await get_ctx(request).automation_status()

    @app.post("/api/automation/pause")
    async def pause_automation(request: Request) -> dict[str, Any]:
        """Stop every scheduled job and cancel the runs in flight. Survives a restart."""
        ctx = get_ctx(request)
        cancelled = await ctx.pause_automation("ui")
        return {**await ctx.automation_status(), "cancelled": cancelled}

    @app.post("/api/automation/resume")
    async def resume_automation(request: Request) -> dict[str, Any]:
        """Let the jobs run again, on their normal schedule (the overdue ones staggered)."""
        ctx = get_ctx(request)
        await ctx.resume_automation("ui")
        return await ctx.automation_status()

    @app.get("/api/status")
    async def status(request: Request) -> api_models.StatusResponse:
        """Dashboard header in one call: the models, approvals and the automation switch."""
        ctx = get_ctx(request)
        now = time.time()
        return api_models.StatusResponse(
            version=__version__,
            uptime_s=round(now - ctx.started_at, 1),
            now=now,
            llm_configured=ctx.settings.llm.configured,
            main_model=ctx.models.name("main"),
            worker_model=ctx.models.name("worker"),
            pending_approvals=await ctx.pending_approvals_count(),
            automation=await ctx.automation_status(),
            **await job_status(ctx),
            change_shell=api_models.ChangeShellStatus(
                policy=ctx.settings.approvals.shell_changes,
                available=ctx.change_shell.status.available,
                offered=change_shell_offered(ctx),
                reason=ctx.change_shell.status.reason,
            ),
        )

    @app.get("/api/hardware")
    async def hardware(request: Request) -> api_models.HardwareResponse:
        """CPU, RAM and NVIDIA GPU use now, for the meters in the page's sidebar."""
        return await read_hardware(get_ctx(request))

    @app.get("/api/stream")
    async def stream(request: Request, types: str | None = None) -> StreamingResponse:
        """Server-sent events (chat, approvals, automation, LLM calls). `?types=action,chat`
        limits what is sent (prefix match)."""
        ctx = get_ctx(request)
        return StreamingResponse(
            event_stream(ctx.events, parse_types(types)),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    # ------------------------------------------------------------------ settings

    @app.get("/api/settings")
    async def get_settings(request: Request) -> api_models.SettingsResponse:
        """Current values (secrets masked), the JSON schema for the form, section titles."""
        return api_models.SettingsResponse.model_validate(
            {
                "values": get_ctx(request).settings.public_dict(),
                "schema": settings_json_schema(),
                "sections": SECTION_TITLES,
            }
        )

    @app.put("/api/settings")
    async def put_settings(request: Request, body: JsonBody) -> api_models.SettingsValues:
        """Replace all settings. Secrets sent back as the mask keep their stored value.
        Applied immediately (models, MCP, approvals and Telegram are restarted)."""
        ctx = get_ctx(request)
        try:
            new = SettingsStore.apply_update(ctx.settings, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        await ctx.apply_settings(new)
        await ctx.mcp_settings_saved()
        return api_models.SettingsValues.model_validate({"values": ctx.settings.public_dict()})

    @app.post("/api/settings/test-llm")
    async def test_llm(
        request: Request, body: JsonBody, profile: Literal["main", "worker"] = "main"
    ) -> dict[str, Any]:
        """Try the given (possibly unsaved) model settings: endpoint reachable, model listed,
        one native tool-call round trip.
        `profile=worker` tests the worker model as the agents would use it (endpoint and key
        of the main model when its own base URL is empty)."""
        ctx = get_ctx(request)
        stored = ctx.settings.worker if profile == "worker" else ctx.settings.llm
        current = stored.model_dump(mode="json", context={"reveal_secrets": True})
        try:
            cfg = LLMSettings.model_validate(merge_secrets(body, current))
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        if profile == "worker":
            if not cfg.model.strip():
                return {"ok": False, "error": "No worker model set: sub-tasks use the main model."}
            cfg = _effective_worker(ctx, cfg)
        if not cfg.configured:
            return {"ok": False, "error": "Set the base URL and model first."}
        return await check_model(cfg, ctx, profile=profile, transport=llm_transport)

    @app.post("/api/settings/llm-model-info")
    async def llm_model_info(
        request: Request, body: JsonBody, profile: Literal["main", "worker"] = "main"
    ) -> api_models.ModelInfoResult:
        """What the model named in the given (possibly unsaved) LLM settings reports about
        itself: its own sampling defaults, capabilities and context length. Ollama only;
        anything else answers `available: false`.
        `profile=worker` asks about the worker model as the agents would use it (endpoint and
        key of the main model when its own base URL is empty, the main model when it has no
        model)."""
        ctx = get_ctx(request)
        stored = ctx.settings.worker if profile == "worker" else ctx.settings.llm
        current = stored.model_dump(mode="json", context={"reveal_secrets": True})
        try:
            cfg = LLMSettings.model_validate(merge_secrets(body, current))
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        if profile == "worker":
            cfg = _effective_worker(ctx, cfg)
        info = await read_model_info(cfg, transport=llm_transport)
        if info is None:
            return api_models.ModelInfoResult(available=False)
        return api_models.ModelInfoResult(available=True, **info.as_dict())

    @app.post("/api/settings/test/{kind}")
    async def test_connection(
        kind: str,
        request: Request,
        body: JsonBody,
        mode: Annotated[str | None, Query(max_length=20)] = None,
        index: Annotated[int | None, Query(ge=0)] = None,
    ) -> api_models.ConnectionTestResult:
        """Try one connection with the given (possibly unsaved) form values: Telegram or an
        MCP server. Masked secrets are replaced by the stored ones. `mode=send`
        (Telegram) sends a test message to every allowed chat; `index` is the position of a
        list item in the form."""
        if kind not in TEST_KINDS:
            raise HTTPException(status_code=404, detail=f"no connection test {kind!r}")
        ctx = get_ctx(request)
        result = await run_test(kind, body, ctx.settings, mode=mode, index=index)
        if kind == "mcp" and result.ok:
            result.message += " " + await sort_tested(ctx, body, result.details)
        return api_models.ConnectionTestResult(
            ok=result.ok, message=result.message, details=result.details
        )

    static_dir = bootstrap.static_dir
    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="spa")

    return app
