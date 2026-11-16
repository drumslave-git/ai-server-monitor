"""Benchmarks at several thinking levels: every scenario at each chosen reasoning_effort, the level
stored per attempt, a per-level summary, and answers that never came counted (not retried)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.bench.runner import is_empty_answer, merge_hw, summarize, summarize_levels
from ai_server_monitor.bench.schema import load_scenarios
from ai_server_monitor.bench.service import clean_efforts, plan_attempts, thinking_known_absent
from ai_server_monitor.config import Bootstrap

from .fake_llm import CANNED_REPORT
from .test_bench_run import _no_warmup, make_app, run_bench  # noqa: F401  (autouse: no warm-up)
from .test_chat import FakeLLM, call, say


def _attempt(sid: str, grp: str, effort: str | None, score: float, **extra: Any) -> dict[str, Any]:
    return {
        "scenario_id": sid,
        "grp": grp,
        "effort": effort,
        "score": score,
        "status": "ok",
        "total_ms": 1000,
        "tokens_out": 100,
        "reasoning_chars": 0,
        "answer_issue": None,
        **extra,
    }


# --------------------------------------------------------------------------- planning


def test_plan_runs_each_scenario_at_each_level_with_the_planner_once() -> None:
    scenarios, _ = load_scenarios()
    chat = next(s for s in scenarios if s.input.kind == "chat")
    prom = next(s for s in scenarios if s.input.kind == "prom_plan")
    plan = plan_attempts([chat, prom], ["none", "high"])
    assert [(e, s.id) for e, s in plan] == [
        ("none", chat.id),
        (None, prom.id),  # the planner always asks for no thinking: once, no level
        ("high", chat.id),
    ]
    assert [(e, s.id) for e, s in plan_attempts([chat, prom], None)] == [
        (None, chat.id),
        (None, prom.id),
    ]


def test_levels_are_validated_and_ordered() -> None:
    assert clean_efforts(["high", "auto", "none", "auto"]) == ["auto", "none", "high"]
    assert clean_efforts([]) is None and clean_efforts(None) is None
    from ai_server_monitor.bench.service import BenchNotReady

    with pytest.raises(BenchNotReady):
        clean_efforts(["extreme"])


def test_thinking_absent_only_when_capabilities_are_listed() -> None:
    assert thinking_known_absent({"capabilities": ["completion", "tools"]})
    assert not thinking_known_absent({"capabilities": ["completion", "thinking"]})
    assert not thinking_known_absent({})  # not Ollama: unknown, so all levels stay


# --------------------------------------------------------------------------- summaries


def test_summarize_levels_per_level_numbers() -> None:
    rows = [
        _attempt("a", "chat", "none", 1.0, total_ms=2000, tokens_out=50),
        _attempt("b", "report", "none", 0.5, total_ms=4000, tokens_out=70),
        _attempt(
            "a",
            "chat",
            "high",
            0.0,
            status="error",
            total_ms=90000,
            tokens_out=16384,
            reasoning_chars=40000,
            answer_issue="empty",
            hw={"n": 4, "vram_used": {"avg": 10.0, "peak": 12.0}, "llm_vram": None},
        ),
        _attempt("b", "report", "high", 1.0, answer_issue="limit"),
        _attempt("p", "prom_plan", None, 1.0),  # no level: not part of any level
    ]
    levels = summarize_levels(rows)
    assert list(levels) == ["none", "high"]
    none, high = levels["none"], levels["high"]
    assert none["overall"] == pytest.approx(0.75) and none["attempts"] == 2
    assert none["median_total_ms"] == 3000 and none["empty"] == 0
    assert high["overall"] == pytest.approx(0.5)
    assert high["failed"] == 1 and high["empty"] == 1 and high["limit"] == 1
    assert high["tokens_out"] == 16484 and high["reasoning_chars"] == 40000
    assert high["hw"]["vram_used"]["peak"] == 12.0
    whole = summarize(rows)["summary"]
    assert whole["empty_answers"] == 1 and whole["limit_hits"] == 1
    assert whole["scenarios"]["a"]["levels"]["high"]["empty"] == 1
    assert "levels" not in whole["scenarios"]["p"]


def test_merge_hw_takes_the_highest_peak_and_a_weighted_average() -> None:
    merged = merge_hw(
        [
            {"hw": {"n": 1, "gpu_util": {"avg": 10.0, "peak": 20.0}, "vram_total": 100}},
            {"hw": {"n": 3, "gpu_util": {"avg": 30.0, "peak": 25.0}, "vram_total": 100}},
            {"hw": None},
        ]
    )
    assert merged is not None
    assert merged["gpu_util"] == {"avg": 25.0, "peak": 25.0} and merged["n"] == 4
    assert merge_hw([{"hw": None}]) is None


def test_empty_answer_is_recognised_by_its_message() -> None:
    from ai_server_monitor.tracing.parse import THOUGHT_ONLY, empty_answer_message

    assert is_empty_answer(empty_answer_message(16384)) and is_empty_answer(THOUGHT_ONLY)
    assert not is_empty_answer("HTTP 500: boom") and not is_empty_answer(None)


# --------------------------------------------------------------------------- end to end


def _effort(request: dict[str, Any]) -> str | None:
    return request.get("reasoning_effort")


def test_run_at_levels_sends_each_level_to_the_answer_calls_only(bootstrap: Bootstrap) -> None:
    # per level, in the order of the selection: the report, then the chat (a tool call, an answer)
    per_level = [
        say(CANNED_REPORT),
        call("get_collector_data", {"collector": "systemd", "section": "failed"}),
        say("The failed unit is logrotate.service (exit-code)."),
    ]
    llm = FakeLLM(*per_level * 3)
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        run = run_bench(
            client,
            app,
            ids=["report.smoke_disk", "chat.smoke_failed_unit"],
            repeats=1,
            efforts=["high", "none", "auto"],
        )
        assert run["status"] == "done", run
        attempts = run["attempts"]
        assert len(attempts) == 6
        by_level: dict[str | None, list[str]] = {}
        for a in attempts:
            by_level.setdefault(a["effort"], []).append(a["scenario_id"])
        assert set(by_level) == {"auto", "none", "high"}
        assert all(
            sorted(v) == ["chat.smoke_failed_unit", "report.smoke_disk"] for v in by_level.values()
        )
        # the answer calls took the level (auto = not sent); the routing classifier always asks none
        sent = [_effort(r) for r in llm.requests]
        assert sent.count("high") == 3 and sent.count("none") == 3 and sent.count(None) == 3
        assert llm.route_requests and all(_effort(r) == "none" for r in llm.route_requests)
        levels = run["summary"]["levels"]
        assert list(levels) == ["auto", "none", "high"]
        assert levels["high"]["attempts"] == 2
        assert run["meta"]["levels"]["tested"] == ["auto", "none", "high"]
        assert "chat answers" in run["meta"]["levels"]["applies_to"]
        # the settings snapshot is kept
        assert run["meta"]["chat"]["tool_routing"]


def test_run_without_levels_behaves_as_before(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say(CANNED_REPORT))
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        (attempt,) = run["attempts"]
        assert attempt["effort"] is None
        assert "levels" not in run["summary"] and not run["meta"]["levels"]


def test_unknown_level_is_refused(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM())
    with TestClient(app) as client:
        r = client.post("/api/bench/runs", json={"ids": ["report.smoke_disk"], "efforts": ["max"]})
        assert r.status_code == 422


def thinking_only(request: httpx.Request) -> httpx.Response:
    """A thinking model that spends the whole limit on reasoning: no answer, finish `length`."""
    body = json.loads(request.content)
    if str(body["messages"][0]["content"]).startswith("You route questions"):
        reply = {"choices": [{"index": 0, "message": {"content": "all of them"}}]}
        return httpx.Response(
            200, json={**reply, "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
        )
    frames = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "hmm " * 50}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 16384}},
    ]
    sse = "".join(f"data: {json.dumps(f)}\n\n" for f in frames) + "data: [DONE]\n\n"
    thinking_only.calls = getattr(thinking_only, "calls", 0) + 1  # type: ignore[attr-defined]
    return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})


def test_an_answer_that_never_came_is_a_failed_attempt_and_not_retried(
    bootstrap: Bootstrap,
) -> None:
    thinking_only.calls = 0  # type: ignore[attr-defined]
    app = make_app(bootstrap, FakeLLM(), transport=httpx.MockTransport(thinking_only))
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1, efforts=["high"])
        (attempt,) = run["attempts"]
        assert attempt["status"] == "error" and attempt["score"] == 0
        assert attempt["effort"] == "high" and attempt["answer_issue"] == "empty"
        assert "output limit" in attempt["error"]
        assert attempt["tokens_out"] == 16384  # what it spent counts
        assert attempt["reasoning_chars"] == 200
        assert thinking_only.calls == 1  # type: ignore[attr-defined]  # never retried
        level = run["summary"]["levels"]["high"]
        assert level["empty"] == 1 and level["failed"] == 1 and level["overall"] == 0
        assert run["summary"]["empty_answers"] == 1
