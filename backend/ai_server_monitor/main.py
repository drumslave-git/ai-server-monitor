"""FastAPI application factory."""

from __future__ import annotations

import hmac
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__, api_models
from .actions.api import router as actions_router
from .bench.api import router as bench_router
from .chat.api import router as chat_router
from .collectors import Collector
from .config import Bootstrap, LLMSettings, Settings, settings_json_schema
from .conntest import KINDS as TEST_KINDS
from .conntest import run_test
from .context import AppContext
from .digest import collectors_snapshot
from .digest.snapshot import Snapshot
from .llm import LLMClient, LLMConnectionError, LLMError
from .llm.text import strip_reasoning
from .outputs.metrics import CONTENT_TYPE, report_families
from .outputs.sse import SSE_HEADERS, event_stream, parse_types
from .promauto.api import router as prometheus_router
from .settings_store import SettingsStore, merge_secrets
from .status import resource_summary
from .tracing import trace_scope
from .tracing.api import router as logs_router

JsonBody = Annotated[dict[str, Any], Body()]

SECTION_TITLES = {
    "general": "General",
    "llm": "LLM",
    "report": "Reports",
    "chat": "Chat",
    "storage": "Data retention",
    "logs": "Logs",
    "bench": "Benchmarks",
    "collectors": "Collectors",
    "outputs": "Outputs",
    "arr": "*arr & downloads",
    "mcp": "MCP servers",
}


def _build_commit() -> str | None:
    """Short git commit the image was built from, None for dev runs / unknown builds."""
    raw = os.environ.get("ASM_BUILD_COMMIT", "").strip()
    return None if not raw or raw == "unknown" else raw[:7]


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
    telegram_transport: httpx.AsyncBaseTransport | None = None,
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
    app.include_router(prometheus_router)
    app.include_router(logs_router)
    app.include_router(bench_router)

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
            "commit": _build_commit(),
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

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        """Prometheus scrape endpoint (Settings → Outputs → metrics)."""
        ctx = get_ctx(request)
        cfg = ctx.settings.outputs.metrics
        if not cfg.enabled:
            raise HTTPException(status_code=404, detail="metrics are disabled")
        token = cfg.bearer_token.get_secret_value()
        if token and not hmac.compare_digest(
            request.headers.get("authorization", ""), f"Bearer {token}"
        ):
            raise HTTPException(
                status_code=401,
                detail="bearer token required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        latest = await ctx.reports.store.latest()
        text = await ctx.metrics.render(report_families(latest, __version__))
        return Response(text, headers={"content-type": CONTENT_TYPE})

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

    # ------------------------------------------------------------------ reports

    @app.get("/api/status")
    async def status(request: Request) -> api_models.StatusResponse:
        """Dashboard header in one call: latest report, collector states, key numbers."""
        ctx = get_ctx(request)
        now = time.time()
        latest = await ctx.reports.store.latest(status="ok")
        newest = await ctx.reports.store.latest(status=None)
        attempt = None
        if newest is not None and newest["status"] != "ok":
            attempt = {"ts": newest["ts"], "status": newest["status"], "error": newest["error"]}
        job = ctx.scheduler.jobs.get("report")
        raw = collectors_snapshot(ctx.collectors)
        return api_models.StatusResponse(
            version=__version__,
            uptime_s=round(now - ctx.started_at, 1),
            now=now,
            report=api_models.LatestReport.model_validate(latest) if latest else None,
            last_attempt=attempt,
            next_report=api_models.NextReport(
                running=bool(job and job.running),
                next_run=job.next_run if job else None,
                interval_s=ctx.settings.report.interval.total_seconds(),
            ),
            llm_configured=ctx.settings.llm.configured,
            pending_approvals=await ctx.pending_approvals_count(),
            collectors={
                name: api_models.CollectorState.model_validate(
                    {k: v for k, v in entry.items() if k != "result"}
                )
                for name, entry in raw.items()
            },
            resources=api_models.Resources.model_validate(resource_summary(Snapshot(raw, now))),
        )

    @app.get("/api/metrics/recent")
    async def recent_metrics(request: Request) -> dict[str, list[dict[str, Any]]]:
        """The last ~120 metrics.tick payloads per source (system, gpu), oldest first."""
        return {k: list(v) for k, v in get_ctx(request).live.recent.items()}

    @app.get("/api/stream")
    async def stream(request: Request, types: str | None = None) -> StreamingResponse:
        """Server-sent events: report.started/token/done/failed, metrics.tick,
        collector.status. `?types=report,metrics` limits what is sent (prefix match)."""
        ctx = get_ctx(request)
        return StreamingResponse(
            event_stream(ctx.events, parse_types(types)),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    @app.get("/api/reports")
    async def list_reports(
        request: Request,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        status: Annotated[Literal["ok", "failed", "skipped"] | None, Query()] = None,
    ) -> api_models.ReportList:
        """Newest first, without the Markdown body (fetch one report for that)."""
        items, total = await get_ctx(request).reports.store.page(
            limit=limit, offset=offset, status=status
        )
        return api_models.ReportList.model_validate(
            {"items": items, "total": total, "limit": limit, "offset": offset}
        )

    @app.get("/api/reports/digest", response_model=api_models.Digest)
    async def current_digest(
        request: Request, format: Annotated[Literal["json", "text"], Query()] = "json"
    ) -> Any:
        """The digest the next report would be written from (for debugging)."""
        digest = await get_ctx(request).reports.digest()
        if format == "text":
            return PlainTextResponse(digest.text)
        return digest.to_dict()

    @app.post("/api/reports/run", status_code=202)
    async def run_report(request: Request) -> api_models.RunStarted:
        """Write a report now (ignores skip_if_unchanged). 409 if one is running."""
        if not get_ctx(request).run_report_now():
            raise HTTPException(status_code=409, detail="A report is already being written.")
        return api_models.RunStarted()

    @app.get("/api/reports/{report_id}")
    async def get_report(report_id: int, request: Request) -> api_models.Report:
        report = await get_ctx(request).reports.store.get(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail=f"no report {report_id}")
        return api_models.Report.model_validate(report)

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
        Applied immediately (collectors and LLM client are restarted)."""
        ctx = get_ctx(request)
        try:
            new = SettingsStore.apply_update(ctx.settings, body)
        except ValidationError as exc:
            raise _validation_error(exc) from exc
        await ctx.apply_settings(new)
        return api_models.SettingsValues.model_validate({"values": ctx.settings.public_dict()})

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
        client = LLMClient(
            cfg.model_copy(update={"max_retries": 0}),
            transport=llm_transport,
            tracing=ctx.tracing,
        )
        models: list[str] | None = None
        try:
            try:
                models = await client.list_models()
            except LLMError:
                models = None  # not all servers implement /models
            with trace_scope("test"):
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

    @app.post("/api/settings/test/{kind}")
    async def test_connection(
        kind: str,
        request: Request,
        body: JsonBody,
        mode: Annotated[str | None, Query(max_length=20)] = None,
        index: Annotated[int | None, Query(ge=0)] = None,
    ) -> api_models.ConnectionTestResult:
        """Try one connection with the given (possibly unsaved) form values: Prometheus,
        Docker, a Radarr/Sonarr/Whisparr/Prowlarr instance, qBittorrent or Telegram.
        Masked secrets are replaced by the stored ones. `mode=send` (Telegram) sends a test
        message to every allowed chat; `index` is the position of a list item (*arr
        instance) in the form."""
        if kind not in TEST_KINDS:
            raise HTTPException(status_code=404, detail=f"no connection test {kind!r}")
        ctx = get_ctx(request)
        result = await run_test(kind, body, ctx.settings, ctx.host_env, mode=mode, index=index)
        return api_models.ConnectionTestResult(
            ok=result.ok, message=result.message, details=result.details
        )

    static_dir = bootstrap.static_dir
    if static_dir is not None and static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="spa")

    return app
