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
from .cards import Answer
from .engine import MAX_MESSAGE, CardClosed

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


class ChatCard(BaseModel):
    """A card of questions the model asked the owner (see `POST /api/chat/cards/{id}/answer`)."""

    id: int
    session_id: str
    call_id: str
    status: str
    """open | answered"""
    questions: list[dict[str, Any]]
    """Each {text, kind (single | multi | text), options}."""
    answers: list[Answer] | None = None
    created: float
    answered: float | None = None
    answered_via: str | None = None
    """web | telegram"""


class CardAnswer(BaseModel):
    answers: list[Answer]
    """One per question: the options chosen (single: at most one) and/or the free text (a text
    question's answer, or the own answer of a choice question)."""


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
    cards: list[ChatCard] = []
    """The question cards of the conversation, open or answered."""


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
    truncated, size, preview}, `token` {text} (the answer, in pieces), `retry` {attempt, of}
    (the answer broke off and is asked for again: drop its text shown since the last
    `tool_call`/`tool_result`; attempt 2 of `of` = 1 + `max_retries`), `card` {card} (the model
    asked the owner questions: the open card, see `POST /api/chat/cards/{id}/answer`), then `done`
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
    cards = await _ctx(request).chat.cards.of_session(session_id)
    return ChatSession.model_validate({**session, "cards": cards})


@router.post(
    "/api/chat/cards/{card_id}/answer",
    responses={
        404: {"description": "No such card"},
        409: {"description": "The card was answered already (here or in Telegram)"},
        422: {"description": "The answers do not fit the questions"},
    },
)
async def answer_card(card_id: int, body: CardAnswer, request: Request) -> ChatCard:
    """Answer a card the model asked. The run waiting for it goes on (the answer appears in the
    conversation: `chat.message` event); the card closes in Telegram too."""
    try:
        card = await _ctx(request).chat.answer_card(
            card_id, [a.model_dump() for a in body.answers], "web"
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no card {card_id}") from exc
    except CardClosed as exc:
        raise HTTPException(status_code=409, detail="The card was answered already.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ChatCard.model_validate(card)


class RunRequest(BaseModel):
    command: str = Field(min_length=1, max_length=MAX_MESSAGE)
    """The command exactly as shown in the answer, with its comment lines (the mark)."""


@router.post(
    "/api/chat/sessions/{session_id}/run",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/event-stream": {}}, "description": "SSE stream"},
        404: {"description": "No such session"},
    },
)
async def run_command(session_id: str, body: RunRequest, request: Request) -> StreamingResponse:
    """Run a command from an answer; the click is the owner's approval. A command marked
    `# [read-only]` runs in the read-only shell, any other as root through the change shell (a
    `host_command` action decided by `user:run-button`). SSE: first `run` {command, mode
    (read-only | write), action_id, exit_code, duration_s, stopped ('error': could not run,
    `output` says why), truncated, output}, then the model's answer to it with the events of
    `/api/chat` (session, tool_call, token, done | error)."""
    ctx = _ctx(request)
    if not await ctx.chat.store.session_exists(session_id):
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    if not body.command.strip():
        raise HTTPException(status_code=422, detail="the command is empty")

    async def frames() -> AsyncIterator[str]:
        async for event in ctx.chat.run_command(session_id, body.command):
            yield _frame(event)

    return StreamingResponse(frames(), media_type="text/event-stream", headers=SSE_HEADERS)


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
