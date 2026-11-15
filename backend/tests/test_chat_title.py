"""Chat session titles written by the LLM after the first answer."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.chat.store import make_title
from ai_server_monitor.chat.title import clean_title
from ai_server_monitor.config import Bootstrap

from .test_chat import FakeLLM, answer, ask, make_app, say

QUESTION = "what is using the most memory on the server right now?"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Memory usage by process", "Memory usage by process"),
        ('"Memory usage by process."', "Memory usage by process"),
        ("«Использование памяти»", "Использование памяти"),
        ("Title: Disk space on disk14TB!", "Disk space on disk14TB"),
        ("**Failed logrotate unit**", "Failed logrotate unit"),
        ("<think>the user asks about memory</think>\nMemory hogs", "Memory hogs"),
        ("```\nMemory hogs\n```", "Memory hogs"),
        ("```text\nMemory hogs\n```\nSome explanation", "Memory hogs"),
        ("Memory hogs 🚀", "Memory hogs"),
        ("Memory hogs…", "Memory hogs"),
        ("Memory hogs\nThis title summarises the memory question.", "Memory hogs"),
        ("'Why is logrotate failing?'", "Why is logrotate failing"),
        ("  Disk   usage \t today  ", "Disk usage today"),
        ("x" * 3 + " " + "y" * 80, "xxx"),
        (
            "one two three four five six seven eight nine ten",
            "one two three four five six seven eight nine ten",
        ),
        ("", None),
        ("   ", None),
        ("<think>still thinking", None),
        ("```\n```", None),
        ("🚀", None),
        ("a", None),
        ("I am sorry, but I cannot create a title for this conversation because it is empty", None),
    ],
)
def test_clean_title(raw: str, expected: str | None) -> None:
    assert clean_title(raw) == expected


def test_clean_title_caps_the_length_at_a_word_boundary() -> None:
    title = clean_title("alpha beta gamma delta epsilon zeta eta theta iota kappa")
    assert title is not None and len(title) <= 60
    long = clean_title("incomprehensibilities " * 4)
    assert long is None or len(long) <= 60
    capped = clean_title("abcdefghij " * 9)  # 9 words, 98 chars
    assert capped is not None and len(capped) <= 60 and not capped.endswith(" ")
    assert capped == ("abcdefghij " * 5).strip()


def sessions(client: TestClient) -> list[dict[str, Any]]:
    return client.get("/api/chat/sessions").json()["items"]


def test_title_is_generated_after_the_answer_and_stored(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("Ollama uses the most memory."))
    llm.title_reply = '"Memory usage by process."'
    with TestClient(make_app(bootstrap, llm, mode="none")) as client:
        events = ask(client, QUESTION)
        kinds = [e["type"] for e in events]
        assert kinds[-2:] == ["done", "title"], kinds  # the answer is complete before the title
        assert events[-1]["title"] == "Memory usage by process"
        assert events[-1]["session_id"] == events[0]["session_id"]
        assert answer(events) == "Ollama uses the most memory."
        assert sessions(client)[0]["title"] == "Memory usage by process"
        # one extra request, outside the conversation's own script, with question and answer
        assert len(llm.requests) == 1 and len(llm.title_requests) == 1
        prompt = llm.title_requests[0]["messages"][-1]["content"]
        assert QUESTION in prompt and "Ollama uses the most memory." in prompt
        assert llm.title_requests[0]["messages"][0]["role"] == "system"
        assert llm.title_requests[0].get("stream") in (None, False)


@pytest.mark.parametrize("reply", ["", "   ", "<think>hmm", "```\n```", "🚀"])
def test_junk_titles_fall_back_to_the_first_message(bootstrap: Bootstrap, reply: str) -> None:
    llm = FakeLLM(say("ok"))
    llm.title_reply = reply
    with TestClient(make_app(bootstrap, llm, mode="none")) as client:
        events = ask(client, QUESTION)
        assert [e["type"] for e in events][-1] == "done"
        assert sessions(client)[0]["title"] == make_title(QUESTION)


def test_llm_failure_keeps_the_fallback_and_the_answer(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("fine"))
    llm.title_reply = lambda body: httpx.Response(500, text="model crashed")
    with TestClient(make_app(bootstrap, llm, mode="none")) as client:
        events = ask(client, QUESTION)
        assert [e["type"] for e in events][-1] == "done"
        assert "error" not in [e["type"] for e in events]
        assert answer(events) == "fine"
        assert sessions(client)[0]["title"] == make_title(QUESTION)


def test_slow_title_is_abandoned_after_the_timeout(
    bootstrap: Bootstrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = FakeLLM(say("fine"))
    app = make_app(bootstrap, llm, mode="none")
    monkeypatch.setattr("ai_server_monitor.chat.engine.TITLE_TIMEOUT_S", 0.3)
    with TestClient(app) as client:

        async def never(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(30)

        monkeypatch.setattr(app.state.ctx.llm, "complete", never)
        started = time.monotonic()
        events = ask(client, QUESTION)
        assert time.monotonic() - started < 5
        assert [e["type"] for e in events][-1] == "done"
        assert sessions(client)[0]["title"] == make_title(QUESTION)


def test_only_the_first_answer_of_a_conversation_gets_a_title(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("one"), say("two"))
    llm.title_reply = "First topic"
    with TestClient(make_app(bootstrap, llm, mode="none")) as client:
        first = ask(client, QUESTION)
        session_id = first[0]["session_id"]
        second = ask(client, "and now the disks?", session_id)
        assert "title" in [e["type"] for e in first]
        assert "title" not in [e["type"] for e in second]
        assert len(llm.title_requests) == 1
        assert sessions(client)[0]["title"] == "First topic"


def test_title_does_not_touch_the_activity_time(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("ok"))
    llm.title_reply = "A title"
    with TestClient(make_app(bootstrap, llm, mode="none")) as client:
        ask(client, QUESTION)
        detail = client.get(f"/api/chat/sessions/{sessions(client)[0]['id']}").json()
        assert detail["title"] == "A title"
        last = detail["messages"][-1]["ts"]
        assert detail["updated"] <= last + 0.001


def test_telegram_sessions_get_titles_too(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("The server is fine."))
    llm.title_reply = "Server health check"
    app = make_app(bootstrap, llm, mode="none")
    with TestClient(app) as client:

        async def run() -> list[dict[str, Any]]:
            engine = app.state.ctx.chat
            return [e async for e in engine.run("how is it?", channel="telegram")]

        events = client.portal.call(run)  # type: ignore[union-attr]
        assert events[-1]["type"] == "title"
        item = sessions(client)[0]
        assert item["channel"] == "telegram" and item["title"] == "Server health check"
