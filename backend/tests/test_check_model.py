"""check_model (the Settings Test button) against a fake OpenAI endpoint."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from ai_server_monitor.config import LLMSettings
from ai_server_monitor.llm.check import CHECK_TOOL, check_model

from .conftest import LLM_URL
from .test_chat import call, completion, say


def _endpoint(
    *replies: dict[str, Any],
    models: list[str] | None = None,
    on_chat: Callable[[dict[str, Any]], None] | None = None,
) -> httpx.MockTransport:
    script = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path.endswith("/models")
            if models is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(
                200, json={"object": "list", "data": [{"id": m, "object": "model"} for m in models]}
            )
        body = json.loads(request.content)
        if on_chat:
            on_chat(body)
        return completion(script.pop(0) if script else say("(script ended)"))

    return httpx.MockTransport(handler)


def _cfg(model: str = "m", base_url: str = LLM_URL) -> LLMSettings:
    return LLMSettings(base_url=base_url, model=model, api_key="sk-test")


async def test_a_model_that_calls_the_tool_and_answers_is_ok() -> None:
    requests: list[dict[str, Any]] = []
    transport = _endpoint(
        call(CHECK_TOOL, {}),
        say("done"),
        models=["m", "other"],
        on_chat=requests.append,
    )
    result = await check_model(_cfg(), transport=transport)
    assert result["ok"] is True and result["tool_call"] is True
    assert result["model_listed"] is True and result["models"] == ["m", "other"]
    assert result["reply"] == "done"
    assert [t["function"]["name"] for t in requests[0]["tools"]] == [CHECK_TOOL]
    # the second request carries the tool result back to the model
    assert any(m["role"] == "tool" for m in requests[1]["messages"])


async def test_a_model_that_answers_without_a_tool_call_cannot_run_the_product() -> None:
    result = await check_model(_cfg(), transport=_endpoint(say("hello there")))
    assert result["ok"] is False and "cannot call tools" in result["error"]
    assert result["reply"] == "hello there"
    assert not result.get("tool_call")


async def test_a_model_missing_from_the_list_is_reported_before_any_chat() -> None:
    requests: list[dict[str, Any]] = []
    transport = _endpoint(say("x"), models=["a", "b"], on_chat=requests.append)
    result = await check_model(_cfg("gone"), transport=transport)
    assert result["ok"] is False and "no model named 'gone'" in result["error"]
    assert result["model_listed"] is False and result["models"] == ["a", "b"]
    assert requests == []


async def test_an_unreachable_endpoint_is_reported() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await check_model(_cfg(), transport=httpx.MockTransport(refuse))
    assert result["ok"] is False and result["error"].startswith("Could not reach")
    assert LLM_URL in result["error"]
