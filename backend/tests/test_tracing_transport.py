"""The recording layer, exercised through the real LLMClient (which this change does not edit):
whatever the client sends and receives must show up in the trace."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest

from ai_server_monitor.config import LLMSettings
from ai_server_monitor.db import Database
from ai_server_monitor.llm import LLMClient, LLMError
from ai_server_monitor.tracing import Tracing, trace_call, trace_scope

from .conftest import LLM_URL, chat_response, sse
from .tracing_helpers import API_KEY, make_tracing

Handler = Callable[[httpx.Request], httpx.Response]


class Rig:
    def __init__(self, tracing: Tracing, client: LLMClient, calls: list[httpx.Request]):
        self.tracing = tracing
        self.client = client
        self.requests = calls

    async def rows(self) -> list[dict[str, Any]]:
        await self.tracing.flush()
        items, _, _ = await self.tracing.store.list_calls(limit=100)
        out = [await self.tracing.store.get_call(i["id"]) for i in reversed(items)]
        return [o for o in out if o is not None]


@pytest.fixture
async def make_rig(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Callable[..., Any]]:
    clients: list[LLMClient] = []

    async def build(handler: Handler, **cfg: Any) -> Rig:
        tracing = await make_tracing(db)
        seen: list[httpx.Request] = []

        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        settings = LLMSettings(
            base_url=LLM_URL, model="test-model", api_key=API_KEY, max_retries=2, **cfg
        )
        client = LLMClient(
            settings, transport=tracing.wrap_transport(httpx.MockTransport(recording))
        )

        async def no_sleep(attempt: int) -> None:
            return None

        monkeypatch.setattr(client, "_backoff", no_sleep)
        clients.append(client)
        return Rig(tracing, client, seen)

    yield build
    for client in clients:
        await client.aclose()


MESSAGES = [
    {"role": "system", "content": "You write server reports."},
    {"role": "user", "content": "Digest: disk / 88% full"},
]
TOOLS = [{"type": "function", "function": {"name": "promql", "parameters": {"type": "object"}}}]


async def test_non_streaming_completion_is_recorded_as_sent_and_received(make_rig: Any) -> None:
    rig: Rig = await make_rig(
        lambda r: chat_response(
            "All good.", tool_calls=None, usage={"prompt_tokens": 31, "completion_tokens": 4}
        )
    )
    with trace_scope("probe"):
        completion = await rig.client.complete(
            MESSAGES, tools=TOOLS, tool_choice="auto", temperature=0.3, max_tokens=500
        )
    assert completion.content == "All good."
    (row,) = await rig.rows()
    assert row["purpose"] == "probe" and row["status"] == "ok" and row["http_status"] == 200
    assert row["messages"] == MESSAGES and row["tools"] == TOOLS
    assert row["content"] == "All good." and row["finish_reason"] == "stop"
    assert (row["prompt_tokens"], row["completion_tokens"], row["usage_estimated"]) == (
        31,
        4,
        False,
    )
    assert row["params"]["temperature"] == 0.3 and row["params"]["max_tokens"] == 500
    assert row["params"]["tool_choice"] == "auto" and row["params"]["stream"] is False
    assert row["model"] == "test-model" and row["base_url"] == LLM_URL and row["stream"] is False
    assert row["total_ms"] is not None and row["ttft_ms"] is None  # nothing streams
    assert row["request_bytes"] == len(rig.requests[0].content)
    assert row["attempt"] == 1 and row["error"] is None


async def test_api_key_and_headers_are_never_stored(make_rig: Any, db: Database) -> None:
    rig: Rig = await make_rig(lambda r: chat_response("ok", usage={"prompt_tokens": 1}))
    await rig.client.complete(MESSAGES)
    assert rig.requests[0].headers["authorization"] == f"Bearer {API_KEY}"  # really sent
    await rig.tracing.flush()
    for row in await db.fetchall("SELECT * FROM llm_calls"):
        assert API_KEY not in json.dumps({k: str(v) for k, v in dict(row).items()})
    (stored,) = await rig.rows()
    assert "authorization" not in json.dumps(stored).lower()


async def test_url_credentials_are_dropped_from_the_stored_base_url(make_rig: Any) -> None:
    rig: Rig = await make_rig(lambda r: chat_response("ok"))
    rig.client._http.base_url = httpx.URL("http://user:pw@llm.test/v1/")
    await rig.client.complete(MESSAGES)
    (row,) = await rig.rows()
    assert row["base_url"] == "http://llm.test/v1"


async def test_non_streaming_tool_calls_and_think_text(make_rig: Any) -> None:
    calls = [
        {"id": "c1", "type": "function", "function": {"name": "promql", "arguments": '{"q":"up"}'}}
    ]
    rig: Rig = await make_rig(
        lambda r: chat_response("<think>should I query?</think>Querying.", tool_calls=calls)
    )
    await rig.client.complete(MESSAGES, tools=TOOLS)
    (row,) = await rig.rows()
    assert row["content"] == "Querying." and row["reasoning"] == "should I query?"
    assert row["tool_calls"] == [{"id": "c1", "name": "promql", "arguments": '{"q":"up"}'}]
    assert row["tool_calls_count"] == 1 and row["usage_estimated"] is True
    assert row["prompt_tokens"] > 0 and row["completion_tokens"] > 0


async def test_streaming_assembles_text_reasoning_tools_usage_and_ttft(make_rig: Any) -> None:
    body = sse(
        {"model": "gemma", "choices": [{"delta": {"reasoning_content": "Let me think. "}}]},
        {"choices": [{"delta": {"reasoning": "More thought."}}]},
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c9",
                                "function": {"name": "promql", "arguments": '{"q":'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"up"}'}}]}}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 33}},
        "[DONE]",
    )
    rig: Rig = await make_rig(lambda r: httpx.Response(200, content=body))
    with trace_scope("chat", link_kind="chat", link_id="s1", step=2, turn="t1"):
        chunks = [c async for c in rig.client.stream(MESSAGES, tools=TOOLS)]
    assert chunks[-1].done
    (row,) = await rig.rows()
    assert row["stream"] is True and row["status"] == "ok"
    assert row["content"] == "Hello"
    assert row["reasoning"] == "Let me think. More thought."
    assert row["tool_calls"] == [{"id": "c9", "name": "promql", "arguments": '{"q":"up"}'}]
    assert row["finish_reason"] == "tool_calls"
    assert (row["prompt_tokens"], row["completion_tokens"]) == (120, 33)
    assert row["ttft_ms"] is not None and 0 <= row["ttft_ms"] <= row["total_ms"]
    assert row["params"]["response_model"] == "gemma"
    assert (row["link_kind"], row["link_id"], row["link_step"], row["turn"]) == (
        "chat",
        "s1",
        2,
        "t1",
    )
    assert row["purpose"] == "chat"
    # the request really asked for a stream with usage
    assert row["params"]["stream"] is True and row["params"]["stream_options"] == {
        "include_usage": True
    }


async def test_streaming_without_usage_is_estimated_and_flagged(make_rig: Any) -> None:
    body = sse({"choices": [{"delta": {"content": "x" * 80}}]}, "[DONE]")
    rig: Rig = await make_rig(lambda r: httpx.Response(200, content=body))
    _ = [c async for c in rig.client.stream(MESSAGES)]
    (row,) = await rig.rows()
    assert row["usage_estimated"] is True and row["completion_tokens"] == 20
    assert row["purpose"] == "other"  # no scope set: still recorded


async def test_http_error_is_recorded_with_body_and_each_retry_is_a_row(make_rig: Any) -> None:
    attempts = iter([503, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(attempts)
        if status == 200:
            return chat_response("finally", usage={"prompt_tokens": 2, "completion_tokens": 1})
        return httpx.Response(status, json={"error": {"message": "model is loading"}})

    rig: Rig = await make_rig(handler)
    with trace_scope("report"), trace_call() as group:
        completion = await rig.client.complete(MESSAGES)
    assert completion.content == "finally" and group.attempt == 3
    rows = await rig.rows()
    assert [(r["attempt"], r["status"], r["http_status"]) for r in rows] == [
        (1, "error", 503),
        (2, "error", 503),
        (3, "ok", 200),
    ]
    assert "model is loading" in rows[0]["error"] and rows[0]["error"].startswith("HTTP 503")
    assert len({r["group_id"] for r in rows}) == 1 and rows[0]["group_id"] == group.id
    assert all(r["purpose"] == "report" for r in rows)


async def test_non_retryable_error_status(make_rig: Any) -> None:
    rig: Rig = await make_rig(lambda r: httpx.Response(401, json={"error": "bad key"}))
    with pytest.raises(LLMError):
        await rig.client.complete(MESSAGES)
    (row,) = await rig.rows()
    assert row["status"] == "error" and row["http_status"] == 401 and "bad key" in row["error"]
    assert row["content"] == "" and row["finish_reason"] is None


async def test_connection_failure_is_recorded(make_rig: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    rig: Rig = await make_rig(handler)
    with pytest.raises(LLMError):
        await rig.client.complete(MESSAGES)
    rows = await rig.rows()
    assert len(rows) == 3  # first try and two retries
    assert all(r["status"] == "error" and r["http_status"] is None for r in rows)
    assert "ConnectError" in rows[0]["error"] and "connection refused" in rows[0]["error"]


async def test_error_inside_a_stream_is_recorded(make_rig: Any) -> None:
    body = sse(
        {"choices": [{"delta": {"content": "partial"}}]},
        {"error": {"message": "out of memory"}},
    )
    rig: Rig = await make_rig(lambda r: httpx.Response(200, content=body))
    with pytest.raises(LLMError):
        _ = [c async for c in rig.client.stream(MESSAGES)]
    (row,) = await rig.rows()
    assert row["status"] == "error" and "out of memory" in row["error"]
    assert row["content"] == "partial"


async def test_abandoned_stream_is_marked_cancelled(make_rig: Any) -> None:
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            await asyncio.sleep(30)
            yield b"data: [DONE]\n\n"  # pragma: no cover

    rig: Rig = await make_rig(lambda r: httpx.Response(200, stream=Slow()))

    async def consume() -> None:
        async for chunk in rig.client.stream(MESSAGES):
            if chunk.text:
                await asyncio.sleep(30)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    (row,) = await rig.rows()
    assert row["status"] == "cancelled" and row["content"] == "first"


async def test_attempt_wait_between_retries_shows_as_queue_time(make_rig: Any) -> None:
    attempts = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        if next(attempts) == 200:
            return chat_response("ok", usage={"prompt_tokens": 1, "completion_tokens": 1})
        return httpx.Response(503, text="busy")

    rig: Rig = await make_rig(handler)
    # a real pause between attempts
    original = rig.client._backoff

    async def pause(attempt: int) -> None:
        await asyncio.sleep(0.15)

    rig.client._backoff = pause  # type: ignore[method-assign]
    with trace_call():
        await rig.client.complete(MESSAGES)
    rig.client._backoff = original  # type: ignore[method-assign]
    first, second = await rig.rows()
    assert first["queued_ms"] < 100
    assert second["queued_ms"] >= 120


async def test_scope_collects_call_ids_for_late_linking(make_rig: Any) -> None:
    rig: Rig = await make_rig(lambda r: chat_response("ok", usage={"prompt_tokens": 1}))
    with trace_scope("report") as scope:
        await rig.client.complete(MESSAGES)
        await rig.client.complete(MESSAGES)
    await rig.client.complete(MESSAGES)  # outside any scope
    await rig.tracing.flush()
    assert len(scope.call_ids) == 2
    await rig.tracing.attach(scope, "report", 7)
    items, total, _ = await rig.tracing.store.list_calls(link_kind="report", link_id="7")
    assert total == 2 and {i["id"] for i in items} == set(scope.call_ids)


async def test_nested_scopes_inherit_and_override(make_rig: Any) -> None:
    rig: Rig = await make_rig(lambda r: chat_response("ok", usage={"prompt_tokens": 1}))
    with trace_scope("chat", link_kind="chat", link_id="s1", turn="t1") as outer:
        with trace_scope(step=3):
            await rig.client.complete(MESSAGES)
        with trace_scope("chat_title"):
            await rig.client.complete(MESSAGES)
    rows = await rig.rows()
    assert [(r["purpose"], r["link_step"], r["link_id"], r["turn"]) for r in rows] == [
        ("chat", 3, "s1", "t1"),
        ("chat_title", None, "s1", "t1"),
    ]
    assert len(outer.call_ids) == 2


async def test_only_chat_completions_are_recorded(make_rig: Any, db: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m1"}]})
        return chat_response("ok", usage={"prompt_tokens": 1})

    rig: Rig = await make_rig(handler)
    assert await rig.client.list_models() == ["m1"]
    assert await rig.rows() == []
    await rig.client.complete(MESSAGES)
    assert len(await rig.rows()) == 1


async def test_disabled_tracing_records_nothing_and_calls_still_work(make_rig: Any) -> None:
    rig: Rig = await make_rig(lambda r: chat_response("ok", usage={"prompt_tokens": 1}))
    rig.tracing.config.enabled = False
    assert (await rig.client.complete(MESSAGES)).content == "ok"
    assert await rig.rows() == []


async def test_a_broken_store_never_breaks_the_llm_call(
    make_rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig: Rig = await make_rig(lambda r: chat_response("ok", usage={"prompt_tokens": 1}))

    async def boom(record: Any) -> int:
        raise RuntimeError("disk full")

    monkeypatch.setattr(rig.tracing.store, "insert_call", boom)
    assert (await rig.client.complete(MESSAGES)).content == "ok"
    await rig.tracing.flush()  # swallowed and logged, not raised


async def test_secrets_in_the_conversation_are_scrubbed(make_rig: Any, db: Database) -> None:
    rig: Rig = await make_rig(lambda r: chat_response(f"your key is {API_KEY}"))
    await rig.client.complete(
        [{"role": "user", "content": f"use {API_KEY} for the request"}], tools=TOOLS
    )
    (row,) = await rig.rows()
    assert API_KEY not in json.dumps(row)
    assert "[redacted]" in row["messages"][0]["content"] and "[redacted]" in row["content"]


async def test_event_published_when_a_call_is_stored(make_rig: Any) -> None:
    from ai_server_monitor.events import EventBus

    rig: Rig = await make_rig(lambda r: chat_response("ok", usage={"prompt_tokens": 1}))
    bus = EventBus()
    rig.tracing.events = bus
    sub = bus.subscribe("llm")
    with trace_scope("report"):
        await rig.client.complete(MESSAGES)
    await rig.tracing.flush()
    event = await asyncio.wait_for(sub.get(), 1)
    assert event.type == "llm.call" and event.data["purpose"] == "report"
    assert event.data["status"] == "ok" and event.data["id"] > 0
