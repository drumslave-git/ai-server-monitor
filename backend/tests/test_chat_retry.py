"""The chat's own answer, shown as it arrives, breaks off part-way: the model request is made
again (the profile's `max_retries`, the OpenAI SDK's wait before each), the client gets one
`retry` event to drop the text shown so far, and only the complete answer is stored
(`agent/models.py` `Reopened`). Invented names only."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent.models import HubModel, ModelHub
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .test_chat import FakeLLM, answer, ask, call, llm_rows, say, sse_body
from .test_model_retry import Breaking

ANSWER = "The invented disk is 40% full and nothing else needs a look."


class BreakingLLM(FakeLLM):
    """FakeLLM whose conversation requests numbered in `breaks` (1-based, repeats included)
    get the first half of their streamed reply and then a dropped connection; `breaks=None`:
    every one does. A broken request does not use up its script entry."""

    def __init__(self, *script: dict[str, Any], breaks: set[int] | None):
        super().__init__(*script)
        self.breaks = breaks
        self.conversation = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return super().handler(request)
        body = json.loads(request.content)
        if str(body["messages"][0]["content"]).startswith("You name chat conversations"):
            return super().handler(request)
        self.conversation += 1
        if self.breaks is not None and self.conversation not in self.breaks:
            return super().handler(request)
        self.requests.append(body)
        headers = {"content-type": "text/event-stream"}
        return httpx.Response(200, headers=headers, stream=Breaking(sse_body(self.script[0])))


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The retries the SDK's wait was asked for (1, 2, ...); the test does not wait."""
    asked: list[int] = []

    def no_wait(self: HubModel, retry: int) -> float:
        asked.append(retry)
        return 0.0

    monkeypatch.setattr(HubModel, "_retry_wait", no_wait)
    return asked


def app_with(bootstrap: Bootstrap, llm: FakeLLM, retries: int) -> Any:
    settings = Settings.model_validate(
        {"llm": {"base_url": LLM_URL, "model": "invented-model", "max_retries": retries}}
    )
    return create_app(
        bootstrap, llm_transport=httpx.MockTransport(llm.handler), seed_settings=settings
    )


def chat_calls(app: Any, client: TestClient) -> list[dict[str, Any]]:
    rows = client.portal.call(llm_rows(app, "llm_calls"))  # type: ignore[union-attr]
    return [r for r in rows if r["purpose"] == "chat"]


def stored(client: TestClient, session_id: str) -> list[dict[str, Any]]:
    return client.get(f"/api/chat/sessions/{session_id}").json()["messages"]


def test_a_broken_answer_is_dropped_and_asked_for_again(
    bootstrap: Bootstrap, waits: list[int]
) -> None:
    llm = BreakingLLM(say(ANSWER), breaks={1})
    app = app_with(bootstrap, llm, retries=2)
    with TestClient(app) as client:
        events = ask(client, "how full is the disk?")
        kinds = [e["type"] for e in events if e["type"] in ("token", "retry", "done")]
        at = kinds.index("retry")
        assert kinds[:at] and set(kinds[:at]) == {"token"}  # part of it had been shown
        assert set(kinds[at + 1 : -1]) == {"token"} and kinds[-1] == "done"
        assert kinds.count("retry") == 1
        retry = events[[e["type"] for e in events].index("retry")]
        assert retry == {"type": "retry", "attempt": 2, "of": 3}
        shown_before = answer(events[: events.index(retry)])
        assert shown_before and ANSWER.startswith(shown_before)  # the broken attempt's text
        assert answer(events[events.index(retry) :]) == ANSWER  # the second attempt, whole
        done = events[-1] if events[-1]["type"] == "done" else events[-2]
        assert done["text"] == ANSWER
        messages = stored(client, done["session_id"])
        assert [(m["role"], m["content"]) for m in messages] == [
            ("user", "how full is the disk?"),
            ("assistant", ANSWER),
        ]
        calls = chat_calls(app, client)
    assert waits == [1]  # the SDK's wait before its first retry
    assert [(c["attempt"], c["status"]) for c in calls] == [(1, "error"), (2, "ok")]
    assert calls[0]["group_id"] == calls[1]["group_id"]  # attempt 2 of the same call
    assert "ReadError" in calls[0]["error"]


def test_a_break_after_a_tool_ran_repeats_only_the_model_request(
    bootstrap: Bootstrap, shell: list[str], waits: list[int]
) -> None:
    llm = BreakingLLM(call("bash", {"command": "df -h /"}), say(ANSWER), breaks={2})
    app = app_with(bootstrap, llm, retries=2)
    with TestClient(app) as client:
        events = ask(client, "how full is the disk?")
        kinds = [e["type"] for e in events]
        assert kinds.count("retry") == 1
        assert kinds.index("tool_result") < kinds.index("retry")
        assert answer(events[kinds.index("retry") :]) == ANSWER
        done = next(e for e in events if e["type"] == "done")
        messages = stored(client, done["session_id"])
        assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
        assert messages[-1]["content"] == ANSWER
        calls = chat_calls(app, client)
    assert shell == ["df -h /"]  # the tool ran once
    assert llm.conversation == 3  # the tool call, the broken answer, the answer again
    assert [(c["attempt"], c["status"]) for c in calls] == [(1, "ok"), (1, "error"), (2, "ok")]
    assert calls[1]["group_id"] == calls[2]["group_id"] != calls[0]["group_id"]


def test_an_answer_that_always_breaks_ends_in_error_and_nothing_is_stored(
    bootstrap: Bootstrap, waits: list[int]
) -> None:
    llm = BreakingLLM(say(ANSWER), breaks=None)
    app = app_with(bootstrap, llm, retries=2)
    with TestClient(app) as client:
        events = ask(client, "how full is the disk?")
        retries = [e for e in events if e["type"] == "retry"]
        assert retries == [
            {"type": "retry", "attempt": 2, "of": 3},
            {"type": "retry", "attempt": 3, "of": 3},
        ]
        assert events[-1]["type"] == "error"
        assert events[-1]["message"] == "The language model failed: Connection error."
        assert "done" not in [e["type"] for e in events]
        session_id = events[0]["session_id"]
        assert [m["role"] for m in stored(client, session_id)] == ["user"]
        calls = chat_calls(app, client)
    assert llm.conversation == 3  # the first request and max_retries = 2 more
    assert waits == [1, 2]
    assert [(c["attempt"], c["status"]) for c in calls] == [(n, "error") for n in (1, 2, 3)]
    assert len({c["group_id"] for c in calls}) == 1


def test_without_retries_a_broken_answer_ends_in_error_as_before(
    bootstrap: Bootstrap, waits: list[int]
) -> None:
    llm = BreakingLLM(say(ANSWER), breaks={1})
    app = app_with(bootstrap, llm, retries=0)
    with TestClient(app) as client:
        events = ask(client, "how full is the disk?")
        kinds = [e["type"] for e in events]
        assert "token" in kinds and "retry" not in kinds
        assert events[-1]["type"] == "error"
        assert [m["role"] for m in stored(client, events[0]["session_id"])] == ["user"]
        calls = chat_calls(app, client)
    assert llm.conversation == 1 and waits == []
    assert [(c["attempt"], c["status"]) for c in calls] == [(1, "error")]


async def test_the_wait_is_the_openai_sdks_backoff() -> None:
    settings = Settings.model_validate(
        {"llm": {"base_url": LLM_URL, "model": "invented-model", "max_retries": 3}}
    )
    hub = ModelHub(settings)
    try:
        model = hub.model("main")
        assert isinstance(model, HubModel)
        for retry, top in ((1, 0.5), (2, 1.0), (3, 2.0)):
            wait = model._retry_wait(retry)
            assert top * 0.75 <= wait <= top
    finally:
        await hub.aclose()
