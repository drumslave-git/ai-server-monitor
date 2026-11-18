"""HTTP API of the chat: one SSE endpoint to ask, plus session history."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..context import AppContext
from ..outputs.sse import SSE_HEADERS
from . import audit
from . import explain as explaining
from .engine import MAX_MESSAGE

router = APIRouter()


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=MAX_MESSAGE)
    session_id: str | None = None
    """Continue this conversation; omit to start a new one."""


class ExplainRequest(BaseModel):
    run_id: int
    """The job run to explain."""
    finding: int | None = Field(default=None, ge=0)
    """Explain this finding of the run (its 0-based index) instead of the run as a whole."""


class ChatMessage(BaseModel):
    id: int
    ts: float
    role: str
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    meta: dict[str, Any] | None = None


class ChatSessionSummary(BaseModel):
    id: str
    created: float
    updated: float
    channel: str
    title: str | None = None
    messages: int
    """User and assistant messages (tool traffic not counted)."""


class ChatSessionList(BaseModel):
    items: list[ChatSessionSummary]
    total: int
    limit: int
    offset: int


class ChatSession(BaseModel):
    id: str
    created: float
    updated: float
    channel: str
    title: str | None = None
    messages: list[ChatMessage]


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    ts: float
    kind: str
    """What kind of call: shell (host commands), api, mcp, ..."""
    session: str | None = None
    """The chat session that made the call."""


class AuditList(BaseModel):
    items: list[AuditEntry]


def _frame(event: dict[str, Any]) -> str:
    data = json.dumps({k: v for k, v in event.items() if k != "type"}, default=str)
    return f"event: {event['type']}\ndata: {data}\n\n"


@router.post(
    "/api/chat",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "SSE stream"}},
)
async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
    """Ask a question. The reply is a server-sent-event stream:
    `session` {session_id}, `tool_call` {id, name, args, kind}, `tool_progress` {id, name,
    elapsed_s} (every few seconds while a tool is still running), `tool_result` {id, name, ok,
    truncated, size, preview}, `token` {text} (the answer, in pieces), then `done`
    {session_id, message_id, steps, usage, stopped} — or `error` {message}.
    For the first answer of a new conversation one more event may follow `done`:
    `title` {session_id, title}, the LLM-written session title (the stream closes right after;
    without it the session keeps the title cut from the first message)."""
    ctx = _ctx(request)
    if not ctx.settings.llm.configured:
        raise HTTPException(
            status_code=409, detail="The LLM is not configured (Settings → Main model)."
        )
    if body.session_id and not await ctx.chat.store.session_exists(body.session_id):
        raise HTTPException(status_code=404, detail=f"no session {body.session_id}")

    async def frames() -> AsyncIterator[str]:
        async for event in ctx.chat.run(body.message, body.session_id):
            yield _frame(event)

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.post(
    "/api/chat/explain",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE stream"},
        404: {"description": "No such run (old runs are pruned) or no such finding"},
        409: {"description": "The run is still running"},
    },
)
async def explain(body: ExplainRequest, request: Request) -> StreamingResponse:
    """Explain a job run, or one of its findings, in a new conversation. The server composes
    the first message from the stored run (job, run, findings with evidence, the commands of
    its trace and their output) and answers it like `/api/chat`, with the same SSE events (the
    `session` event also carries `explain` {label, source}: the label and the whole message); the
    stored first message carries `meta.explain` {label, run_id, job, finding} and the session is
    titled with the label."""
    ctx = _ctx(request)
    try:
        prepared = await explaining.build(ctx, body.run_id, body.finding)
    except explaining.ExplainNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except explaining.ExplainError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def frames() -> AsyncIterator[str]:
        async for event in ctx.chat.explain(prepared):
            if event["type"] == "session":
                # the short label and the composed message, to show before anything is stored
                event = {**event, "explain": {"label": prepared.label, "source": prepared.prompt}}
            yield _frame(event)

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/api/chat/sessions")
async def list_sessions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ChatSessionList:
    items, total = await _ctx(request).chat.store.list_sessions(limit, offset)
    return ChatSessionList.model_validate(
        {"items": items, "total": total, "limit": limit, "offset": offset}
    )


@router.get("/api/chat/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> ChatSession:
    session = await _ctx(request).chat.store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    return ChatSession.model_validate(session)


class RunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=MAX_MESSAGE)
    """The command exactly as shown in the answer."""


class CommandRun(BaseModel):
    message_id: int
    """The stored message of the session that shows it (`meta.run`)."""
    command: str
    exit_code: int | None = None
    """None when it was stopped or could not run."""
    duration_s: float
    stopped: str | None = None
    """'timeout', 'output limit', or 'error' (could not run; `output` says why)."""
    truncated: bool = False
    output: str


@router.post("/api/chat/sessions/{session_id}/run")
async def run_command(session_id: str, body: RunRequest, request: Request) -> CommandRun:
    """Run a command from an answer in the read-only host shell (as the `bash` tool does) and
    add it with its result to the conversation. The model sees it with the next question; no
    model turn starts now."""
    ctx = _ctx(request)
    if not await ctx.chat.store.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    if not body.command.strip():
        raise HTTPException(status_code=422, detail="the command is empty")
    return CommandRun.model_validate(await ctx.chat.run_command(session_id, body.command))


@router.delete("/api/chat/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> Response:
    if not await _ctx(request).chat.store.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    return Response(status_code=204)


@router.get("/api/chat/audit")
async def chat_audit(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=300)] = 50,
    kind: str | None = None,
) -> AuditList:
    """What the chat did on its own (commands run on the host, API and MCP calls), newest
    first. Extra fields per entry depend on the kind (shell: cmd, exit, seconds, bytes)."""
    rows = await audit.recent(_ctx(request).db, limit, kind)
    return AuditList.model_validate({"items": rows})
