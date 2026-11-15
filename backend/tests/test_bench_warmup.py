"""Cold start: the warm-up request before the first attempt, and reload suspicion."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.bench.probe import ollama_loaded
from ai_server_monitor.bench.service import BenchService
from ai_server_monitor.bench.warmup import reload_suspects, warmup_messages
from ai_server_monitor.config import Bootstrap, LLMSettings

from .fake_llm import CANNED_REPORT
from .test_bench_run import make_app, probe_transport, wait_done
from .test_chat import FakeLLM, say


def ps_transport(states: list[dict[str, Any]]) -> httpx.MockTransport:
    """/api/ps answers, one per request (the last one repeats)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        body = states[min(calls["n"], len(states) - 1)]
        calls["n"] += 1
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


MODEL = {"name": "fake-model:latest", "model": "fake-model:latest", "size": 9, "size_vram": 8}
LOADED = {"models": [MODEL]}
EMPTY: dict[str, Any] = {"models": []}


@pytest.fixture(autouse=True)
def _warmup_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BenchService, "warmup_enabled", True)


def start(
    client: TestClient, app: Any, ps: httpx.AsyncBaseTransport | None, **body: Any
) -> dict[str, Any]:
    bench = app.state.ctx.bench
    bench._probe_transport = probe_transport()
    bench._ps_transport = ps
    run_id = client.post("/api/bench/runs", json=body).json()["run_id"]
    wait_done(client)
    return client.get(f"/api/bench/runs/{run_id}").json()


async def test_ollama_loaded_reads_ps() -> None:
    cfg = LLMSettings(base_url="http://o/v1", model="fake-model")
    assert (await ollama_loaded(cfg, transport=ps_transport([LOADED])))["loaded"] is True
    state = await ollama_loaded(cfg, transport=ps_transport([EMPTY]))
    assert state == {"loaded": False, "size": None, "size_vram": None}
    gone = httpx.MockTransport(lambda r: httpx.Response(404))
    assert await ollama_loaded(cfg, transport=gone) is None


def test_warmup_message_is_the_chat_prompt_plus_ok() -> None:
    system, user = warmup_messages()
    assert user["content"] == "Reply with OK"
    assert len(system["content"]) > 500


def test_warmup_runs_first_is_recorded_and_left_out_of_the_numbers(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("OK"), say(CANNED_REPORT))
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        ps = ps_transport([EMPTY, LOADED, LOADED, LOADED])
        run = start(client, app, ps, ids=["report.smoke_disk"], repeats=1)
        assert run["status"] == "done", run
        first = llm.requests[0]
        assert first["messages"][-1]["content"] == "Reply with OK" and "tools" not in first
        assert first["max_tokens"] <= 32
        cold = run["meta"]["cold_start"]
        assert cold["ok"] and cold["supported"] and cold["loaded_before"] is False
        assert cold["total_ms"] >= 0 and cold["reply"] == "OK"
        assert cold["size_vram"] == 8
        # one attempt, one scored result, none of the warm-up in the sums
        assert len(run["attempts"]) == 1 and run["summary"]["attempts"] == 1
        attempt = run["attempts"][0]
        assert run["summary"]["tokens_in"] == attempt["tokens_in"]
        assert run["overall"] == pytest.approx(1.0)
        listed = client.get("/api/bench/runs").json()["items"][0]
        assert listed["cold_start"]["loaded_before"] is False
        # its LLM call is in the trace as a benchmark call linked to the warm-up
        calls = client.get("/api/logs/llm", params={"purpose": "bench"}).json()["items"]
        assert f"{run['id']}/warmup/0" in {c["link_id"] for c in calls}


def test_warmup_on_a_backend_without_ps_still_runs(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("OK"), say(CANNED_REPORT))
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        no_ps = httpx.MockTransport(lambda r: httpx.Response(404))
        run = start(client, app, no_ps, ids=["report.smoke_disk"], repeats=1)
        cold = run["meta"]["cold_start"]
        assert cold["ok"] and cold["supported"] is False and cold["loaded_before"] is None
        assert run["attempts"][0]["reload_suspected"] is None


def test_warmup_failure_fails_the_run_with_the_reason(bootstrap: Bootstrap) -> None:
    down = httpx.MockTransport(lambda r: httpx.Response(500, text="model failed to load"))
    app = make_app(bootstrap, FakeLLM(), transport=down)
    with TestClient(app) as client:
        run = start(client, app, None, ids=["report.smoke_disk"], repeats=1)
        assert run["status"] == "failed" and "not reachable" in run["error"]
        assert run["attempts"] == []


def test_a_model_unloaded_between_attempts_is_flagged_not_averaged(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("OK"), say(CANNED_REPORT), say(CANNED_REPORT))
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        # ps: before warm-up, after warm-up, before attempt 1 (loaded), before attempt 2 (gone)
        ps = ps_transport([LOADED, LOADED, LOADED, EMPTY])
        run = start(client, app, ps, ids=["report.smoke_disk"], repeats=2)
        first, second = run["attempts"]
        assert first["reload_suspected"] is None
        assert "not loaded" in second["reload_suspected"]
        assert run["summary"]["reload_suspected"] == 1
        detail = client.get(f"/api/bench/runs/{run['id']}/attempts/{second['id']}").json()
        assert "reload_suspected" in detail["detail"]
        assert second["score"] is not None  # still counted, just visible


def test_reload_suspects_by_ttft_outliers() -> None:
    def rows(*ttfts: int | None) -> list[dict[str, Any]]:
        return [{"id": i, "ttft_ms": t} for i, t in enumerate(ttfts, start=1)]

    out = reload_suspects(rows(300, 320, 310, 9000, 305))
    assert list(out) == [4] and "reloaded" in out[4]
    assert reload_suspects(rows(300, 320, 310)) == {}  # too few attempts to judge
    assert reload_suspects(rows(100, 110, 105, 600, 102)) == {}  # slow only relatively
    assert reload_suspects(rows(None, None, 5000, 5000, 5000)) == {}
