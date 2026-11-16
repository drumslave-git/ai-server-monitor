from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from ai_server_monitor.collectors import CollectorManager
from ai_server_monitor.collectors.base import Collector, CollectorResult
from ai_server_monitor.config import LLMSettings, Settings
from ai_server_monitor.db import Database
from ai_server_monitor.digest import WindowObserver
from ai_server_monitor.events import EventBus
from ai_server_monitor.llm import LLMClient
from ai_server_monitor.report.engine import NOT_CONFIGURED, ReportEngine
from ai_server_monitor.scheduler import Scheduler

from .conftest import LLM_URL
from .fake_llm import CANNED_REPORT, sent_messages, stream_response
from .test_digest import healthy_system


class StubSystem(Collector):
    name = "system"

    async def collect(self) -> dict[str, Any]:  # pragma: no cover - results are injected
        return {}


def make_settings(**report: Any) -> Settings:
    return Settings(
        llm={"base_url": LLM_URL, "model": "test-model", "max_retries": 0, "tool_mode": "none"},
        report=report,
    )


class Harness:
    def __init__(self, db: Database, settings: Settings):
        self.settings = settings
        self.events = EventBus()
        self.sub = self.events.subscribe()
        self.llm = LLMClient(settings.llm)
        self.manager = CollectorManager(settings, db, Scheduler(), registry={"system": StubSystem})
        self.manager.states["system"] = "active"
        self.set_system(healthy_system())
        self.engine = ReportEngine(
            settings, db, self.llm, self.manager, self.events, WindowObserver()
        )

    def set_system(self, data: dict[str, Any]) -> None:
        self.manager.latest["system"] = CollectorResult("system", time.time(), ok=True, data=data)

    def event_types(self) -> list[str]:
        out = []
        while not self.sub._queue.empty():
            event = self.sub._queue.get_nowait()
            if event is not None:
                out.append(event.type)
        return out


@pytest.fixture
async def make(db: Database) -> AsyncIterator[Any]:
    made: list[Harness] = []

    def factory(settings: Settings | None = None, **report: Any) -> Harness:
        h = Harness(db, settings or make_settings(**report))
        made.append(h)
        return h

    yield factory
    for h in made:
        await h.llm.aclose()


@respx.mock
async def test_successful_report_is_parsed_stored_and_announced(db: Database, make: Any) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response())
    h = make()
    outcome = await h.engine.run()

    assert outcome.status == "ok" and outcome.report_id
    row = await h.engine.store.get(outcome.report_id)
    assert row is not None
    assert row["status"] == "ok" and row["health"] == "warn" and row["score"] == 74
    assert row["summary"].startswith("Two disks")
    assert [f["severity"] for f in row["findings"]] == ["warn", "warn", "info"]
    assert row["markdown"].startswith("## Overview") and "```json" not in row["markdown"]
    assert row["model"] == "test-model" and row["parsed"] is True
    assert row["tokens_in"] == 900 and row["tokens_out"] == 250
    assert "SERVER DIGEST" in row["digest"] and row["digest_hash"]

    # One streamed request: fixed system prompt + digest + previous-report note.
    sent = sent_messages(route)
    assert [m["role"] for m in sent] == ["system", "user"]
    assert "json block" in sent[0]["content"] or "fenced json" in sent[0]["content"]
    assert "<digest>" in sent[1]["content"] and "Previous report: none" in sent[1]["content"]

    types = h.event_types()
    assert types[0] == "report.started" and types[-1] == "report.done"
    assert types.count("report.token") >= 5


@respx.mock
async def test_previous_summary_is_sent_for_continuity(db: Database, make: Any) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response())
    h = make()
    await h.engine.run()
    await h.engine.run()
    user = sent_messages(route)[1]["content"]
    assert "Previous report (" in user and "health warn, score 74" in user
    assert "Two disks are over 85% full" in user


@respx.mock
async def test_bad_format_falls_back_to_rule_flags(db: Database, make: Any) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=stream_response("The server is fine, I think.")
    )
    h = make()
    h.set_system(
        healthy_system(
            filesystems=[
                {"mount": "/", "total": 100e9, "used": 97e9, "avail": 3e9, "percent": 97.0}
            ]
        )
    )
    outcome = await h.engine.run()
    row = await h.engine.store.get(outcome.report_id or 0)
    assert row is not None and row["status"] == "ok" and row["parsed"] is False
    assert row["health"] == "crit" and row["score"] < 50
    assert row["findings"][0]["title"] == "Disk / 97% full"
    assert row["markdown"] == "The server is fine, I think."


@respx.mock
async def test_unreachable_llm_records_failure_once_without_log_spam(
    db: Database, make: Any, caplog: pytest.LogCaptureFixture
) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))
    h = make()
    caplog.set_level(logging.DEBUG, "ai_server_monitor.report")
    for _ in range(3):
        outcome = await h.engine.run()
        assert outcome.status == "failed" and "refused" in (outcome.message or "")
    rows, total = await h.engine.store.page()
    assert total == 1 and rows[0]["status"] == "failed"  # repeats only move the timestamp
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1  # logged once, not every interval
    assert h.event_types().count("report.failed") == 3
    # Recovery: the next success is a new row and resets the "already logged" state.
    respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response())
    assert (await h.engine.run()).status == "ok"
    assert (await h.engine.store.page())[1] == 2


async def test_unconfigured_llm_is_skipped_with_a_clear_message(db: Database, make: Any) -> None:
    h = make(Settings())  # no base_url / model
    assert not h.settings.llm.configured
    outcome = await h.engine.run()
    again = await h.engine.run()
    assert outcome.status == "skipped" and outcome.message == NOT_CONFIGURED
    assert again.report_id == outcome.report_id
    rows, total = await h.engine.store.page()
    assert total == 1 and rows[0]["status"] == "skipped" and rows[0]["error"] == NOT_CONFIGURED


@respx.mock
async def test_http_error_and_empty_answer_are_failures(db: Database, make: Any) -> None:
    h = make()
    respx.post(f"{LLM_URL}/chat/completions").mock(return_value=httpx.Response(401, text="no key"))
    out = await h.engine.run()
    assert out.status == "failed" and "401" in (out.message or "")
    respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response("  "))
    out = await h.engine.run()
    assert out.status == "failed" and "empty" in (out.message or "")
    assert (await h.engine.store.latest(status="ok")) is None


@respx.mock
async def test_skip_if_unchanged(db: Database, make: Any) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response())
    h = make(skip_if_unchanged=True)
    assert (await h.engine.run()).status == "ok"

    h.set_system(healthy_system(uptime_s=99_000))  # volatile numbers only
    skipped = await h.engine.run()
    assert skipped.status == "skipped" and not skipped.stored
    assert route.call_count == 1 and (await h.engine.store.page())[1] == 1

    h.engine.force_next = True  # manual "run now" ignores the setting, once
    assert (await h.engine.run()).status == "ok" and route.call_count == 2
    assert (await h.engine.run()).status == "skipped"

    h.set_system(  # something relevant changed: a new flag
        healthy_system(
            filesystems=[
                {"mount": "/", "total": 100e9, "used": 90e9, "avail": 10e9, "percent": 90.0}
            ]
        )
    )
    assert (await h.engine.run()).status == "ok" and route.call_count == 3


@respx.mock
async def test_reasoning_text_is_not_stored(db: Database, make: Any) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=stream_response('<think>hmm {"health": "crit"}</think>' + CANNED_REPORT)
    )
    h = make()
    outcome = await h.engine.run()
    row = await h.engine.store.get(outcome.report_id or 0)
    assert row is not None and row["health"] == "warn" and "think" not in row["markdown"]


@respx.mock
async def test_job_timeout_becomes_a_failure(db: Database, make: Any) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return stream_response()

    respx.post(f"{LLM_URL}/chat/completions").mock(side_effect=hang)
    settings = Settings(
        llm=LLMSettings(base_url=LLM_URL, model="m", max_retries=0, timeout="1s"),  # type: ignore[arg-type]
    )
    h = make(settings)
    h.engine.time_limit = lambda: 0.2  # type: ignore[method-assign]
    out = await h.engine.run()
    assert out.status == "failed" and "timed out" in (out.message or "")


async def test_report_rows_follow_retention(db: Database, make: Any) -> None:
    h = make(Settings())
    await h.engine.run()
    from datetime import timedelta

    deleted = await db.prune(timedelta(days=1), now=time.time() + 3 * 86400)
    assert deleted["reports"] == 1


@respx.mock
async def test_output_cut_off_by_the_token_limit_is_flagged(db: Database, make: Any) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=stream_response(CANNED_REPORT[:700], finish="length")
    )
    h = make()
    outcome = await h.engine.run()
    row = await h.engine.store.get(outcome.report_id or 0)
    assert (
        row is not None and "cut off" in row["markdown"] and "max output tokens" in row["markdown"]
    )
    assert row["status"] == "ok"


@respx.mock
async def test_report_that_used_the_whole_limit_on_thinking_is_a_failure(
    db: Database, make: Any
) -> None:
    """A thinking model can spend all of max_output_tokens on reasoning: HTTP 200, no text,
    finish_reason "length". The report must show that as a failure with the limit named, not
    store an empty report."""
    respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=stream_response("", finish="length")
    )
    h = make()
    out = await h.engine.run()
    limit = h.settings.llm.max_output_tokens
    assert out.status == "failed"
    assert f"{limit} tokens" in (out.message or "") and "thinking" in (out.message or "")
    assert (await h.engine.store.latest(status="ok")) is None
    row = await h.engine.store.get(out.report_id or 0)
    assert row is not None and row["status"] == "failed" and "thinking" in row["error"]


def _sent_bodies(route: Any) -> list[dict[str, Any]]:
    import json

    return [json.loads(c.request.content) for c in route.calls]


@respx.mock
async def test_reports_ask_for_no_thinking_by_default_whatever_chat_uses(
    db: Database, make: Any
) -> None:
    """A report summarises an already analysed digest; thinking models loop on it for minutes.
    The report has its own thinking level, so a chat-friendly LLM setting does not change it."""
    route = respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response())
    settings = make_settings()
    settings.llm.reasoning_effort = "high"
    h = make(settings)
    assert (await h.engine.run()).status == "ok"
    assert _sent_bodies(route)[-1]["reasoning_effort"] == "none"


@respx.mock
async def test_report_thinking_level_is_a_setting(db: Database, make: Any) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response())
    assert (await make(reasoning_effort="low").engine.run()).status == "ok"
    assert _sent_bodies(route)[-1]["reasoning_effort"] == "low"
    assert (await make(reasoning_effort="auto").engine.run()).status == "ok"
    assert "reasoning_effort" not in _sent_bodies(route)[-1]  # auto: the model decides


@respx.mock
async def test_report_that_only_thought_fails_in_the_report_too(db: Database, make: Any) -> None:
    """finish_reason "stop" with only reasoning (seen on gemma4:26b): no report is stored."""
    events = [
        {"choices": [{"delta": {"reasoning": "hmm " * 40}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ]
    from .conftest import sse

    respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200, content=sse(*events), headers={"content-type": "text/event-stream"}
        )
    )
    out = await make().engine.run()
    assert out.status == "failed" and "empty" in (out.message or "")
