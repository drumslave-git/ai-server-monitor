"""Benchmark runs end to end: the service, the runner, storage and the HTTP API, with a scripted
fake LLM (no network: the backend probe, the judge and the LLM all go through mock transports)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from functools import partial
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.bench.service import BenchService
from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .fake_llm import CANNED_REPORT
from .test_chat import FakeLLM, call, say

OLLAMA_PROBE = {
    "/api/version": {"version": "0.13.5"},
    "/api/show": {
        "details": {
            "parameter_size": "7.9B",
            "quantization_level": "Q4_K_M",
            "family": "gemma",
            "format": "gguf",
        },
        "model_info": {"gemma.context_length": 131072, "general.architecture": "gemma"},
    },
}


@pytest.fixture(autouse=True)
def _no_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests script the model's answers one by one: no cold-start request in them."""
    monkeypatch.setattr(BenchService, "warmup_enabled", False)


def probe_transport(routes: dict[str, Any] | None = None) -> httpx.MockTransport:
    routes = OLLAMA_PROBE if routes is None else routes

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


class StubSystem(Collector):
    name = "system"

    async def collect(self) -> dict[str, Any]:
        return {"hostname": "box", "memory": {"total": 1000, "used": 600, "percent": 60.0}}


def stalling(llm: FakeLLM, *, after: int) -> httpx.MockTransport:
    """The fake LLM, but request number `after` + 1 and later never answer (until cancelled)."""
    seen = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["n"] += 1
        if seen["n"] > after:
            await asyncio.sleep(60)
        return llm.handler(request)

    return httpx.MockTransport(handler)


def make_app(
    bootstrap: Bootstrap,
    llm: FakeLLM,
    *,
    mode: str = "native",
    bench: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Any:
    settings = Settings.model_validate(
        {
            "llm": {
                "base_url": LLM_URL,
                "model": "fake-model",
                "tool_mode": mode,
                "max_retries": 0,
                "temperature": 0.3,
                "top_p": 0.9,
            },
            "bench": bench or {},
        }
    )
    return create_app(
        bootstrap,
        llm_transport=transport or httpx.MockTransport(llm.handler),
        collector_registry={"system": StubSystem},
        seed_settings=settings,
    )


def wait_done(client: TestClient, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not client.get("/api/bench/status").json()["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("the benchmark run did not finish")


def run_bench(client: TestClient, app: Any, **body: Any) -> dict[str, Any]:
    app.state.ctx.bench._probe_transport = probe_transport()
    response = client.post("/api/bench/runs", json=body)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    wait_done(client)
    detail = client.get(f"/api/bench/runs/{run_id}")
    assert detail.status_code == 200
    return detail.json()


# --------------------------------------------------------------------------- scenarios


def test_report_scenario_runs_through_the_real_engine(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say(CANNED_REPORT))
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        assert run["status"] == "done", run
        assert run["overall"] == pytest.approx(1.0)
        (attempt,) = run["attempts"]
        assert attempt["status"] == "ok" and attempt["score"] == pytest.approx(1.0)
        assert attempt["tokens_in"] and attempt["tokens_out"]
        full = client.get(f"/api/bench/runs/{run['id']}/attempts/{attempt['id']}").json()
        assert all(c["passed"] for c in full["checks"])
        assert "Disk / filling up" in full["answer"]
        # The model saw the snapshot's data (91 % disk, the failed unit), not the live server.
        digest = llm.requests[0]["messages"][1]["content"]
        assert "logrotate" in digest and "91" in digest and "bench-host" in digest


def test_chat_scenario_uses_real_tools_over_the_snapshot(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(
        call("get_collector_data", {"collector": "systemd", "section": "failed"}),
        say("The failed unit is logrotate.service (exit-code)."),
    )
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["chat.smoke_failed_unit"], repeats=1)
        (attempt,) = run["attempts"]
        assert attempt["score"] == pytest.approx(1.0), attempt
        # (the tool-routing classifier call counts as an LLM call of the attempt)
        assert attempt["tool_calls"] == 1 and attempt["llm_calls"] == 3
        assert attempt["tool_mode"] == "native"
        # the tool result the model got came from the frozen snapshot
        tool_msg = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")
        assert "logrotate.service" in tool_msg["content"]
        # the request carried the real tool schemas, including the host tools on any OS
        names = {t["function"]["name"] for t in llm.requests[0]["tools"]}
        assert {"get_collector_data", "run_command", "disk_usage", "api_call"} <= names


def test_json_protocol_scenario_counts_repairs(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(
        say(
            '{"tool": "get_collector_data", '
            '"args": {"collector": "system", "section": "filesystems"}}'
        ),
        say("The root filesystem is 91% full."),
    )
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["robust.smoke_json_tools"], repeats=1)
        (attempt,) = run["attempts"]
        assert attempt["tool_mode"] == "json"
        assert attempt["score"] == pytest.approx(1.0), attempt
        # forced json mode: the request has no native `tools`, the protocol is in the prompt
        assert "tools" not in llm.requests[0]
        assert '"tool"' in llm.requests[0]["messages"][0]["content"]


def test_failing_checks_score_below_one_and_explain_themselves(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("I have no idea."))
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["chat.smoke_failed_unit"], repeats=2)
        assert run["overall"] == 0.0 or run["overall"] < 0.5
        full = client.get(f"/api/bench/runs/{run['id']}/attempts/{run['attempts'][0]['id']}").json()
        failed = [c for c in full["checks"] if not c["passed"]]
        assert any("was not called" in c["reason"] for c in failed)
        assert len(run["attempts"]) == 2


# --------------------------------------------------------------------------- metadata


def test_run_records_backend_model_and_settings(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM(say(CANNED_REPORT)))
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        assert run["model"] == "fake-model" and run["base_url"] == LLM_URL
        assert run["backend_kind"] == "ollama" and run["backend_version"] == "0.13.5"
        assert run["params"] == "7.9B" and run["quantization"] == "Q4_K_M"
        assert run["context_length"] == 131072
        assert run["meta"]["llm"]["temperature"] == 0.3
        # top_p / top_k as resolved for the run: a number stays a number, "auto" stays "auto"
        assert run["meta"]["llm"]["top_p"] == 0.9 and run["meta"]["llm"]["top_k"] == "auto"
        assert run["meta"]["tool_mode"] == "native"
        assert run["app_version"] and run["scenario_hash"]
        assert run["finished"] >= run["started"]
        listed = client.get("/api/bench/runs").json()["items"]
        assert listed[0]["id"] == run["id"] and listed[0]["temperature"] == 0.3


def test_unknown_backend_still_records_what_it_can(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM(say(CANNED_REPORT)))
    with TestClient(app) as client:
        app.state.ctx.bench._probe_transport = probe_transport({})
        client.post("/api/bench/runs", json={"ids": ["report.smoke_disk"], "repeats": 1})
        wait_done(client)
        run = client.get("/api/bench/runs").json()["items"][0]
        assert run["backend_kind"] == "openai-compatible" and run["params"] is None
        assert run["status"] == "done"


# --------------------------------------------------------------------------- traces


def test_attempt_llm_calls_are_linked_to_the_run_in_the_trace(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM(say(CANNED_REPORT)))
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        attempt = client.get(
            f"/api/bench/runs/{run['id']}/attempts/{run['attempts'][0]['id']}"
        ).json()
        assert attempt["trace_ids"]
        calls = client.get("/api/logs/llm", params={"purpose": "bench"}).json()
        assert calls["total"] == len(attempt["trace_ids"])
        link = f"{run['id']}/report.smoke_disk/1"
        assert {c["link_id"] for c in calls["items"]} == {link}
        assert {c["link_kind"] for c in calls["items"]} == {"bench"}


# --------------------------------------------------------------------------- control


def test_not_configured_and_busy_and_unknown(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, seed_settings=Settings(), collector_registry={"system": StubSystem})
    with TestClient(app) as client:
        response = client.post("/api/bench/runs", json={})
        assert response.status_code == 400 and "not configured" in response.json()["detail"]
    llm = FakeLLM()
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        assert client.post("/api/bench/runs", json={"ids": ["nope.nothing"]}).status_code == 400
        assert client.post("/api/bench/runs", json={"repeats": 99}).status_code == 422


def test_cancel_stops_the_run_and_keeps_finished_attempts(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say(CANNED_REPORT))
    app = make_app(bootstrap, llm, transport=stalling(llm, after=1))
    with TestClient(app) as client:
        app.state.ctx.bench._probe_transport = probe_transport()
        run_id = client.post(
            "/api/bench/runs", json={"ids": ["report.smoke_disk"], "repeats": 3}
        ).json()["run_id"]
        deadline = time.time() + 20
        while time.time() < deadline:
            progress = client.get("/api/bench/status").json()["progress"]
            if progress and progress["done"] >= 1:
                break
            time.sleep(0.05)
        assert client.post("/api/bench/runs", json={}).status_code == 409  # busy
        assert client.delete(f"/api/bench/runs/{run_id}").status_code == 409  # in progress
        assert client.post("/api/bench/cancel").json() == {"cancelled": True}
        run = client.get(f"/api/bench/runs/{run_id}").json()
        assert run["status"] == "cancelled"
        assert len(run["attempts"]) == 1 and run["overall"] is not None
        assert client.post("/api/bench/cancel").json() == {"cancelled": False}


def test_attempt_over_its_time_budget_is_a_timeout(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say(CANNED_REPORT))
    app = make_app(
        bootstrap,
        llm,
        transport=stalling(llm, after=0),
        bench={"attempt_timeout": "1s"},
    )
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        (attempt,) = run["attempts"]
        assert attempt["status"] == "timeout" and attempt["score"] == 0
        assert "timed out" in attempt["error"]
        assert run["status"] == "done" and run["summary"]["failures"] == 1


def test_llm_error_is_an_error_attempt_not_a_crashed_run(bootstrap: Bootstrap) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model crashed")

    settings = Settings.model_validate(
        {"llm": {"base_url": LLM_URL, "model": "m", "tool_mode": "native", "max_retries": 0}}
    )
    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(boom),
        seed_settings=settings,
        collector_registry={"system": StubSystem},
    )
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        assert run["status"] == "done"
        (attempt,) = run["attempts"]
        assert attempt["status"] == "error" and attempt["score"] == 0
        assert "model crashed" in attempt["error"] or "500" in attempt["error"]
        assert run["summary"]["failures"] == 1


def test_delete_and_export(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM(say(CANNED_REPORT)))
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        exported = client.get(f"/api/bench/runs/{run['id']}/export")
        assert exported.status_code == 200
        assert "attachment" in exported.headers["content-disposition"]
        doc = json.loads(exported.text)
        assert doc["run"]["model"] == "fake-model"
        assert doc["attempts"][0]["answer"] and doc["attempts"][0]["checks"]
        assert client.delete(f"/api/bench/runs/{run['id']}").status_code == 204
        assert client.get(f"/api/bench/runs/{run['id']}").status_code == 404
        assert client.get("/api/bench/runs").json()["items"] == []


def test_interrupted_runs_are_marked_failed_at_startup(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM())
    with TestClient(app) as client:
        ctx = app.state.ctx
        run_id = client.portal.call(partial(ctx.bench.store.create_run, scope="all", repeats=1))
        assert client.portal.call(ctx.bench.store.get_run, run_id)["status"] == "running"
        client.portal.call(ctx.bench.store.recover)
        run = client.get(f"/api/bench/runs/{run_id}").json()
        assert run["status"] == "failed" and "restarted" in run["error"]


# --------------------------------------------------------------------------- catalog


def test_catalog_hides_smoke_scenarios_by_default(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM())
    with TestClient(app) as client:
        shown = client.get("/api/bench/catalog").json()
        assert [g["key"] for g in shown["groups"]] == ["report", "chat", "prom_plan", "robust"]
        assert not any(s["id"].endswith("smoke_disk") for s in shown["scenarios"])
        with_smoke = client.get("/api/bench/catalog", params={"smoke": True}).json()
        assert any(s["id"] == "report.smoke_disk" for s in with_smoke["scenarios"])
        smoke = next(s for s in with_smoke["scenarios"] if s["id"] == "report.smoke_disk")
        assert smoke["checks"][0]["describe"]
        assert shown["repeats"] == 3


_ = Callable
