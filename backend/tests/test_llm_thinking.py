"""Thinking models: no call may lose its answer to reasoning, and when it still happens it is a
clear failure. Settings (reasoning_effort, max_output_tokens) and the short helper calls."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_server_monitor.config import Bootstrap, LLMSettings, Settings, settings_json_schema
from ai_server_monitor.main import create_app
from ai_server_monitor.settings_store import upgrade_stored

from .conftest import LLM_URL, chat_response, sse
from .test_chat import FakeLLM, ask, make_app, say
from .test_chat_title import QUESTION

# --------------------------------------------------------------------------- settings


def test_defaults_leave_room_for_thinking_and_do_not_force_a_level() -> None:
    llm = LLMSettings()
    assert llm.max_output_tokens >= 16384
    assert llm.reasoning_effort == "auto"


def test_reasoning_effort_accepts_the_api_values_only() -> None:
    for value in ("auto", "none", "low", "medium", "high"):
        assert LLMSettings(reasoning_effort=value).reasoning_effort == value  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        LLMSettings(reasoning_effort="max")  # type: ignore[arg-type]


def test_the_old_default_output_limit_is_replaced_but_a_chosen_one_is_kept() -> None:
    cleaned, dropped = upgrade_stored({"llm": {"max_output_tokens": 4096, "model": "m"}})
    assert cleaned == {"llm": {"model": "m"}} and dropped
    kept, dropped = upgrade_stored({"llm": {"max_output_tokens": 8192}})
    assert kept == {"llm": {"max_output_tokens": 8192}} and not dropped


def test_settings_schema_shows_the_new_fields_with_help() -> None:
    props = settings_json_schema()["$defs"]["LLMSettings"]["properties"]
    assert props["reasoning_effort"]["enum"] == ["auto", "none", "low", "medium", "high"]
    assert "reasoning_effort" in props["reasoning_effort"]["description"]
    assert props["max_output_tokens"]["default"] == 16384


# --------------------------------------------------------------------------- short helper calls


def test_chat_title_asks_for_no_thinking_and_has_room(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("Ollama uses the most memory."))
    llm.title_reply = "Memory usage"
    with TestClient(make_app(bootstrap, llm, mode="none")) as client:
        ask(client, QUESTION)
    (request,) = llm.title_requests
    assert request["reasoning_effort"] == "none" and request["max_tokens"] >= 200


def test_tool_routing_asks_for_no_thinking_and_has_room(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("Ollama."))
    llm.route_reply = '{"groups": ["host"]}'
    app = make_app(bootstrap, llm, chat={"tool_routing": "always"})
    with TestClient(app) as client:
        ask(client, "what uses the most memory?")
    (request,) = llm.route_requests
    assert request["reasoning_effort"] == "none" and request["max_tokens"] >= 400


@respx.mock
def test_settings_test_asks_for_no_thinking_and_has_room(bootstrap: Bootstrap) -> None:
    respx.get(f"{LLM_URL}/models").mock(return_value=httpx.Response(404))
    route = respx.post(f"{LLM_URL}/chat/completions").mock(return_value=chat_response("OK"))
    with TestClient(create_app(bootstrap, collector_registry={})) as client:
        result = client.post(
            "/api/settings/test-llm", json={"base_url": LLM_URL, "model": "thinker"}
        ).json()
    assert result["ok"] is True
    sent = json.loads(route.calls.last.request.content)
    assert sent["reasoning_effort"] == "none" and sent["max_tokens"] >= 1024


@respx.mock
def test_settings_test_reports_a_model_that_only_thought(bootstrap: Bootstrap) -> None:
    respx.get(f"{LLM_URL}/models").mock(return_value=httpx.Response(404))
    respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "", "reasoning": "hmm"},
                        "finish_reason": "length",
                    }
                ]
            },
        )
    )
    with TestClient(create_app(bootstrap, collector_registry={})) as client:
        result = client.post(
            "/api/settings/test-llm", json={"base_url": LLM_URL, "model": "thinker"}
        ).json()
    assert result["ok"] is False and "thinking" in result["error"]


# --------------------------------------------------------------------------- chat shows the failure


def test_chat_says_so_when_the_model_spent_its_whole_limit_thinking(bootstrap: Bootstrap) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        events = [
            {"choices": [{"delta": {"reasoning": "let me think " * 50}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
            "[DONE]",
        ]
        return httpx.Response(
            200, content=sse(*events), headers={"content-type": "text/event-stream"}
        )

    settings = Settings.model_validate(
        {
            "llm": {
                "base_url": LLM_URL,
                "model": "m",
                "tool_mode": "none",
                "max_retries": 0,
                "max_output_tokens": 2000,
            },
            "chat": {"tool_routing": "off"},
        }
    )
    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(handler),
        collector_registry={},
        seed_settings=settings,
    )
    with TestClient(app) as client:
        events = ask(client, "hello")
    assert events[-1]["type"] == "error"
    message = events[-1]["message"]
    assert "2000 tokens" in message and "thinking" in message
    assert not [e for e in events if e["type"] == "done"]  # no "(no answer)" posing as a reply
    assert len(seen) == 1  # and no silent retry


# --------------------------------------------------------------------------- benchmarks


async def test_warmup_that_only_thinks_still_counts_as_loaded() -> None:
    from ai_server_monitor.bench.warmup import warm_up
    from ai_server_monitor.llm import LLMClient

    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        events = [
            {"choices": [{"delta": {"reasoning": "hmm"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
            "[DONE]",
        ]
        return httpx.Response(
            200, content=sse(*events), headers={"content-type": "text/event-stream"}
        )

    cfg = LLMSettings(base_url=LLM_URL, model="thinker", max_retries=0)
    client = LLMClient(cfg, transport=httpx.MockTransport(handler))
    try:
        no_ps = httpx.MockTransport(lambda r: httpx.Response(404))
        record, _ = await warm_up(client, cfg, ps_transport=no_ps, timeout_s=5)
    finally:
        await client.aclose()
    assert record["ok"] is True and record["tries"] == 1
    assert sent[0]["reasoning_effort"] == "none" and sent[0]["max_tokens"] <= 32


@respx.mock
async def test_judge_that_only_thought_gives_no_grade_and_says_why() -> None:
    from ai_server_monitor.bench import judge
    from ai_server_monitor.config import BenchJudgeSettings

    route = respx.post("http://judge.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"}
                ]
            },
        )
    )
    cfg = BenchJudgeSettings(enabled=True, base_url="http://judge.test/v1", model="j")
    score, reason = await judge.grade(cfg, task="t", rubric="r", answer="a", tools=[])
    assert score is None and "thinking" in reason and str(judge.MAX_TOKENS) in reason
    assert json.loads(route.calls.last.request.content)["max_tokens"] == judge.MAX_TOKENS >= 2048
