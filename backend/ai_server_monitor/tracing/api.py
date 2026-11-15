"""HTTP API of the Logs page. The router reads `request.app.state.tracing` (a `Tracing`).

GET /api/logs/llm                    list (filters: purpose, status, since/until, link, q ...)
GET /api/logs/llm/stats              totals and the purposes seen
GET /api/logs/llm/{id}               one call, everything stored
GET /api/logs/llm/{id}/timeline      its chat turn / linked calls with the tool calls between
GET /api/logs/llm/{id}/export        the call as a downloadable JSON file
GET /api/logs/tools/{id}             one tool call with arguments and result
GET /api/logs/app                    app log (memory ring or stored WARNING+), filtered
GET /api/logs/app/loggers            logger names seen in the ring
GET /api/logs/app/stream             live tail (SSE, event `log`)
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..outputs.sse import SSE_HEADERS
from .applog import levelno
from .service import Tracing

router = APIRouter(prefix="/api/logs", tags=["logs"])

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


def _tracing(request: Request) -> Tracing:
    return request.app.state.tracing


class LlmCallSummary(BaseModel):
    id: int
    ts: float
    purpose: str
    link_kind: str | None = None
    link_id: str | None = None
    link_step: int | None = None
    turn: str | None = None
    group_id: str | None = None
    attempt: int = 1
    model: str | None = None
    base_url: str | None = None
    stream: bool = False
    status: Literal["ok", "error", "cancelled"]
    http_status: int | None = None
    error: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_estimated: bool = False
    queued_ms: int | None = None
    ttft_ms: int | None = None
    total_ms: int | None = None
    tokens_per_s: float | None = None
    messages_count: int | None = None
    tools_count: int | None = None
    tools_tokens: int | None = None
    request_bytes: int | None = None
    response_chars: int | None = None
    tool_calls_count: int = 0
    request_preview: str | None = None
    response_preview: str | None = None
    truncated: list[str] = Field(default_factory=list)
    bodies: bool = True
    size_bytes: int = 0


class LlmCallList(BaseModel):
    items: list[LlmCallSummary]
    total: int
    limit: int
    offset: int
    complete: bool = True
    """False when a text search covered only the newest rows."""


class LlmCall(LlmCallSummary):
    params: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    content: str | None = None
    reasoning: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ToolCallInfo(BaseModel):
    id: int
    ts: float
    link_kind: str | None = None
    link_id: str | None = None
    link_step: int | None = None
    turn: str | None = None
    tool: str
    ok: bool
    duration_ms: int | None = None
    result_size: int | None = None
    error: str | None = None
    truncated: bool = False
    size_bytes: int = 0


class ToolCallDetail(ToolCallInfo):
    args: Any = None
    result: Any = None


class TimelineEntry(BaseModel):
    type: Literal["llm", "tool"]
    id: int
    ts: float
    link_step: int | None = None
    # LLM calls
    purpose: str | None = None
    model: str | None = None
    status: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    queued_ms: int | None = None
    ttft_ms: int | None = None
    total_ms: int | None = None
    tool_calls_count: int | None = None
    response_preview: str | None = None
    attempt: int | None = None
    # tool calls
    tool: str | None = None
    ok: bool | None = None
    duration_ms: int | None = None
    result_size: int | None = None
    error: str | None = None


class Timeline(BaseModel):
    selected: int
    entries: list[TimelineEntry]


class PurposeCount(BaseModel):
    purpose: str
    count: int


class TraceStats(BaseModel):
    calls: int
    failed: int
    tool_calls: int
    bytes: int
    oldest: float | None = None
    purposes: list[PurposeCount]
    enabled: bool
    store_bodies: bool
    retention_days: float
    max_total_mb: int


class AppLogRecord(BaseModel):
    id: int | None = None
    seq: int | None = None
    ts: float
    level: str
    levelno: int
    logger: str
    message: str
    exc: str | None = None


class AppLogList(BaseModel):
    items: list[AppLogRecord]
    source: Literal["memory", "stored"]
    newest: int | None = None
    """Memory: the sequence number of the newest record at the time of the call."""


@router.get("/llm")
async def list_llm_calls(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    purpose: str | None = None,
    status: Literal["ok", "error", "cancelled"] | None = None,
    since: float | None = None,
    until: float | None = None,
    link_kind: str | None = None,
    link_id: str | None = None,
    turn: str | None = None,
    model: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> LlmCallList:
    """Newest first. `q` searches the stored text (messages, answer, reasoning, tool calls) of
    the newest 2,000 matching rows."""
    items, total, complete = await _tracing(request).store.list_calls(
        limit=limit,
        offset=offset,
        purpose=purpose,
        status=status,
        since=since,
        until=until,
        link_kind=link_kind,
        link_id=link_id,
        turn=turn,
        model=model,
        q=q,
    )
    return LlmCallList(
        items=[LlmCallSummary.model_validate(i) for i in items],
        total=total,
        limit=limit,
        offset=offset,
        complete=complete,
    )


@router.get("/llm/stats")
async def llm_stats(request: Request) -> TraceStats:
    tracing = _tracing(request)
    stats = await tracing.store.stats()
    cfg = tracing.config
    return TraceStats(
        **stats,
        enabled=cfg.enabled,
        store_bodies=cfg.store_bodies,
        retention_days=cfg.retention_days,
        max_total_mb=cfg.max_total_mb,
    )


@router.get("/llm/{call_id}")
async def get_llm_call(request: Request, call_id: int) -> LlmCall:
    call = await _tracing(request).store.get_call(call_id)
    if call is None:
        raise HTTPException(404, "No such LLM call (it may have been pruned).")
    return LlmCall.model_validate(call)


@router.get("/llm/{call_id}/timeline")
async def llm_timeline(request: Request, call_id: int) -> Timeline:
    timeline = await _tracing(request).store.timeline(call_id)
    if timeline is None:
        raise HTTPException(404, "No such LLM call (it may have been pruned).")
    return Timeline.model_validate(timeline)


@router.get("/llm/{call_id}/export", response_class=Response)
async def export_llm_call(request: Request, call_id: int) -> Response:
    call = await _tracing(request).store.get_call(call_id)
    if call is None:
        raise HTTPException(404, "No such LLM call (it may have been pruned).")
    return Response(
        json.dumps(call, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="llm-call-{call_id}.json"'},
    )


@router.get("/tools/{tool_id}")
async def get_tool_call(request: Request, tool_id: int) -> ToolCallDetail:
    tool = await _tracing(request).store.get_tool(tool_id)
    if tool is None:
        raise HTTPException(404, "No such tool call (it may have been pruned).")
    return ToolCallDetail.model_validate(tool)


@router.get("/app")
async def app_log(
    request: Request,
    source: Literal["memory", "stored"] = "memory",
    level: Level | None = None,
    logger: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    before: Annotated[int | None, Query(ge=0)] = None,
) -> AppLogList:
    """Newest first. `memory` is the last few thousand records since the process started,
    `stored` the persisted WARNING-and-above records. `before` pages back (a `seq` for memory,
    an `id` for stored)."""
    tracing = _tracing(request)
    scrub = tracing.scrubber()
    minimum = levelno(level)
    if source == "memory":
        records = tracing.applog.ring.query(
            limit=limit, min_levelno=minimum, logger=logger, q=q, before=before
        )
        newest = tracing.applog.ring.last_seq
    else:
        records = await tracing.store.list_logs(
            limit=limit, before=before, min_levelno=minimum, logger=logger, q=q
        )
        newest = None
    if scrub is not None:
        records = [
            {
                **r,
                "message": scrub.text(r["message"]),
                "exc": scrub.text(r["exc"]) if r.get("exc") else None,
            }
            for r in records
        ]
    return AppLogList(
        items=[AppLogRecord.model_validate(r) for r in records], source=source, newest=newest
    )


@router.get("/app/loggers")
async def app_loggers(request: Request) -> list[str]:
    return _tracing(request).applog.ring.loggers()


@router.get(
    "/app/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE stream"}},
)
async def app_log_stream(
    request: Request,
    level: Level | None = None,
    logger: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """Live tail: `event: log` frames carrying {records: [...], skipped}. At most two frames a
    second and 100 records per frame; `skipped` counts what the rate limit left out."""
    tracing = _tracing(request)
    scrub = tracing.scrubber()
    return StreamingResponse(
        tracing.applog.tail(
            min_levelno=levelno(level),
            logger=logger,
            q=q,
            after=after,
            scrub=scrub.text if scrub is not None else None,
        ),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
