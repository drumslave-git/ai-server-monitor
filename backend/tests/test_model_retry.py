"""A model answer whose stream breaks off after the headers is retried with the profile's
`max_retries` (the OpenAI SDK's own retries; `agent/models.py` `WholeBody`), and the call log
numbers the attempts. The chat's own answer, shown as it arrives, is asked for again after a
`retry` event (`Reopened`; the chat's side in `test_chat_retry.py`)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from ai_server_monitor.agent.models import ModelHub
from ai_server_monitor.agent.runtime import AgentConfig, AgentTool, run_agent
from ai_server_monitor.config import Settings
from ai_server_monitor.db import Database
from ai_server_monitor.tracing import Tracing

from .conftest import LLM_URL
from .test_chat import call, say, sse_body
from .tracing_helpers import make_tracing


class Breaking(httpx.AsyncByteStream):
    """The first part of an answer, then the connection drops (what the call log showed:
    HTTP 200, then `ReadError` while the body was read)."""

    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.body[: len(self.body) // 2]
        raise httpx.ReadError("connection reset by peer")


class Endpoint(httpx.AsyncBaseTransport):
    """Plays one reply per model request; the HTTP requests numbered in `breaks` (1-based,
    retries included) get an answer that breaks off; `breaks=None`: every one does."""

    def __init__(self, *script: dict[str, Any], breaks: set[int] | None):
        self.script = list(script)
        self.breaks = breaks
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        body = sse_body(self.script[0])
        headers = {"content-type": "text/event-stream"}
        if self.breaks is None or self.requests in self.breaks:
            return httpx.Response(200, headers=headers, stream=Breaking(body))
        self.script.pop(0)
        return httpx.Response(200, headers=headers, content=body)


async def make(db: Database, endpoint: Endpoint, retries: int) -> tuple[ModelHub, Tracing]:
    tracing = await make_tracing(db)
    settings = Settings.model_validate(
        {"llm": {"base_url": LLM_URL, "model": "invented-model", "max_retries": retries}}
    )
    return ModelHub(settings, tracing=tracing, transport=endpoint), tracing


async def rows(tracing: Tracing) -> list[dict[str, Any]]:
    await tracing.flush()
    items, _, _ = await tracing.store.list_calls(limit=100)
    out = [await tracing.store.get_call(i["id"]) for i in reversed(items)]
    return [o for o in out if o is not None]


def counter(ran: list[dict[str, Any]]) -> AgentTool:
    async def handler(args: dict[str, Any]) -> str:
        ran.append(args)
        return "uptime 3 days"

    return AgentTool(
        name="uptime",
        description="How long the host is up.",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


async def test_a_broken_answer_is_requested_again_and_the_log_shows_attempt_2(
    db: Database,
) -> None:
    ran: list[dict[str, Any]] = []
    endpoint = Endpoint(call("uptime", {}), say("Up for 3 days, all fine."), breaks={2})
    hub, tracing = await make(db, endpoint, retries=2)
    cfg = AgentConfig(
        model=hub.model("worker"),
        instructions="test",
        tools=[counter(ran)],
        tracing=tracing,
        trace={"purpose": "job", "link_kind": "job", "link_id": "demo:uptime"},
    )
    try:  # the answer after the tool call (HTTP request 2) breaks off once
        events = [event async for event in run_agent(cfg, "check uptime")]
    finally:
        await hub.aclose()
    assert ran == [{}]  # the tool ran once: only the model request was repeated
    assert events[-1]["output"] == "Up for 3 days, all fine."
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text == "Up for 3 days, all fine."  # nothing of the broken attempt
    assert endpoint.requests == 3
    got = [(r["attempt"], r["status"]) for r in await rows(tracing)]
    assert got == [(1, "ok"), (1, "error"), (2, "ok")]
    logged = await rows(tracing)
    assert "ReadError" in logged[1]["error"]
    assert logged[1]["group_id"] == logged[2]["group_id"] != logged[0]["group_id"]
    assert all(r["purpose"] == "job" for r in logged)


async def test_an_answer_that_always_breaks_fails_after_the_configured_retries(
    db: Database,
) -> None:
    endpoint = Endpoint(say("never complete"), breaks=None)
    hub, tracing = await make(db, endpoint, retries=1)
    cfg = AgentConfig(model=hub.model("main"), instructions="test", tracing=tracing)
    try:
        with pytest.raises(Exception) as info:
            async for _ in run_agent(cfg, "hello"):
                pass
    finally:
        await hub.aclose()
    # as before the change: the job stores "ModelConnectionError: Connection error."
    assert f"{type(info.value).__name__}: {info.value}" == "ModelConnectionError: Connection error."
    assert endpoint.requests == 2  # the first request and max_retries = 1 repeat
    logged = await rows(tracing)
    assert [(r["attempt"], r["status"]) for r in logged] == [(1, "error"), (2, "error")]
    assert all(r["error"] == "ReadError: connection reset by peer" for r in logged)


async def test_the_chat_answer_shown_as_it_arrives_is_asked_again_after_a_retry_event(
    db: Database,
) -> None:
    endpoint = Endpoint(say("A long answer, shown word by word."), breaks={1})
    hub, tracing = await make(db, endpoint, retries=2)
    cfg = AgentConfig(model=hub.model("main"), instructions="test", tracing=tracing, live=True)
    try:
        events = [event async for event in run_agent(cfg, "hello")]
    finally:
        await hub.aclose()
    kinds = [e["type"] for e in events]
    at = kinds.index("retry")
    assert events[at] == {"type": "retry", "attempt": 2, "of": 3}
    assert set(kinds[:at]) == {"token"}  # part of the answer had already been passed on
    after = "".join(e["text"] for e in events[at:] if e["type"] == "token")
    assert after == events[-1]["output"] == "A long answer, shown word by word."
    assert endpoint.requests == 2
    logged = await rows(tracing)
    assert [(r["attempt"], r["status"]) for r in logged] == [(1, "error"), (2, "ok")]
    assert logged[0]["group_id"] == logged[1]["group_id"]
