from __future__ import annotations

import json

import httpx
import pytest
import respx

from servermon.llm import LLMClient, LLMConnectionError, LLMStatusError

from .conftest import LLM_URL, chat_response, sse


def _delta(text: str | None = None, finish: str | None = None, **extra: object) -> dict:
    delta: dict = {}
    if text is not None:
        delta["content"] = text
    delta.update(extra)
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


@respx.mock
async def test_complete_sends_payload_and_parses(llm: LLMClient) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=chat_response("hello", usage={"prompt_tokens": 12, "completion_tokens": 3})
    )
    result = await llm.complete([{"role": "user", "content": "hi"}], temperature=0)

    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "test-model"
    assert sent["temperature"] == 0
    assert sent["stream"] is False
    assert "tools" not in sent and "tool_choice" not in sent
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"
    assert result.content == "hello"
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (12, 3)
    assert llm.stats.requests == 1 and llm.stats.prompt_tokens == 12


@respx.mock
async def test_complete_parses_tool_calls_including_object_arguments(llm: LLMClient) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=chat_response(
            None,
            tool_calls=[
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "a", "arguments": '{"x": 1}'},
                },
                {"type": "function", "function": {"name": "b", "arguments": {"y": 2}}},
            ],
        )
    )
    result = await llm.complete([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert result.content == ""
    assert [(c.id, c.name, c.parsed_arguments()) for c in result.tool_calls] == [
        ("c1", "a", {"x": 1}),
        ("call_1", "b", {"y": 2}),
    ]
    assert result.usage.estimated  # no usage in the response


@respx.mock
async def test_complete_retries_on_503_then_succeeds(llm: LLMClient) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        side_effect=[httpx.Response(503, text="loading model"), chat_response("ok")]
    )
    assert (await llm.complete([{"role": "user", "content": "x"}])).content == "ok"
    assert route.call_count == 2


@respx.mock
async def test_complete_no_retry_on_401(llm: LLMClient) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    with pytest.raises(LLMStatusError) as info:
        await llm.complete([{"role": "user", "content": "x"}])
    assert info.value.status_code == 401
    assert route.call_count == 1
    assert llm.stats.errors == 1


@respx.mock
async def test_complete_connection_error(llm: LLMClient) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(LLMConnectionError):
        await llm.complete([{"role": "user", "content": "x"}])


@respx.mock
async def test_stream_yields_text_and_usage(llm: LLMClient) -> None:
    body = sse(
        _delta(reasoning_content="thinking..."),
        _delta("Hel"),
        _delta("lo"),
        _delta(finish="stop"),
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
        "[DONE]",
    )
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )
    )
    chunks = [c async for c in llm.stream([{"role": "user", "content": "hi"}])]

    sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    assert "".join(c.text for c in chunks) == "Hello"
    final = chunks[-1]
    assert final.done and final.finish_reason == "stop"
    assert final.usage is not None and final.usage.completion_tokens == 2
    assert not final.usage.estimated
    assert llm.stats.completion_tokens == 2


@respx.mock
async def test_stream_falls_back_when_stream_options_rejected(llm: LLMClient) -> None:
    ok = httpx.Response(200, content=sse(_delta("hi"), "[DONE]"))
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        side_effect=[httpx.Response(400, text="unknown field stream_options"), ok]
    )
    chunks = [c async for c in llm.stream([{"role": "user", "content": "hello there"}])]
    assert "stream_options" not in json.loads(route.calls.last.request.content)
    assert chunks[-1].usage is not None and chunks[-1].usage.estimated
    # Remembered for later calls
    route.side_effect = None
    route.return_value = httpx.Response(200, content=sse(_delta("x"), "[DONE]"))
    _ = [c async for c in llm.stream([{"role": "user", "content": "again"}])]
    assert "stream_options" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_stream_error_event_raises(llm: LLMClient) -> None:
    body = sse(_delta("partial"), {"error": {"message": "out of memory"}})
    respx.post(f"{LLM_URL}/chat/completions").mock(return_value=httpx.Response(200, content=body))
    received: list[str] = []
    with pytest.raises(LLMStatusError, match="out of memory"):
        async for chunk in llm.stream([{"role": "user", "content": "hi"}]):
            received.append(chunk.text)
    assert received == ["partial"]


@respx.mock
async def test_base_url_trailing_slash_and_no_api_key(llm_cfg) -> None:  # type: ignore[no-untyped-def]
    cfg = llm_cfg.model_copy(update={"base_url": LLM_URL + "/", "api_key": ""})
    from pydantic import SecretStr

    cfg = cfg.model_copy(update={"api_key": SecretStr("")})
    client = LLMClient(cfg)
    route = respx.post(f"{LLM_URL}/chat/completions").mock(return_value=chat_response("ok"))
    try:
        await client.complete([{"role": "user", "content": "x"}])
    finally:
        await client.aclose()
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_extra_body_passthrough(llm_cfg) -> None:  # type: ignore[no-untyped-def]
    cfg = llm_cfg.model_copy(update={"extra_body": {"top_k": 20}})
    client = LLMClient(cfg)
    route = respx.post(f"{LLM_URL}/chat/completions").mock(return_value=chat_response("ok"))
    try:
        await client.complete([{"role": "user", "content": "x"}])
    finally:
        await client.aclose()
    assert json.loads(route.calls.last.request.content)["top_k"] == 20
