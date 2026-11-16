"""The global "pause automation" switch: scheduler behaviour, persistence, API and benchmarks."""

from __future__ import annotations

import asyncio
import itertools
import time
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.bench.service import BenchService
from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app
from ai_server_monitor.scheduler import Scheduler

from .conftest import LLM_URL
from .fake_llm import CANNED_REPORT
from .test_bench_run import probe_transport, run_bench, wait_done
from .test_chat import FakeLLM, say


async def _until(predicate, timeout: float = 3.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- scheduler


async def test_pause_stops_every_job_and_resume_brings_them_back() -> None:
    sched = Scheduler()
    counts = {"a": 0, "b": 0}

    def make(name: str):  # type: ignore[no-untyped-def]
        async def job() -> None:
            counts[name] += 1

        return job

    for name in counts:
        sched.add(name, make(name), timedelta(milliseconds=20))
    await sched.start()
    try:
        await _until(lambda: counts["a"] >= 2 and counts["b"] >= 2)
        await sched.pause()
        frozen = dict(counts)
        await asyncio.sleep(0.3)  # many intervals
        assert counts == frozen
        assert sched.paused and all(j.paused and j.waiting for j in sched.jobs.values())
        assert all(s["paused"] for s in sched.status())
        sched.resume(stagger_s=0.01)
        await _until(lambda: counts["a"] > frozen["a"] and counts["b"] > frozen["b"])
        assert not any(j.paused for j in sched.jobs.values())
    finally:
        await sched.stop()


async def test_pause_cancels_a_running_job_cleanly() -> None:
    sched = Scheduler()
    cleaned: list[str] = []

    async def slow() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            cleaned.append("cleanup")

    job = sched.add("slow", slow, timedelta(hours=1))
    await sched.start()
    try:
        await _until(lambda: job.running)
        assert await sched.pause() == ["slow"]
        assert not job.running  # unwound before pause() returned
        assert cleaned == ["cleanup"]
        assert job.cancelled == 1 and job.failures == 0 and job.runs == 0
        assert job.last_status == "cancelled" and job.last_error is None
        # still alive: the loop waits at the gate and runs again after the resume
        sched.resume(stagger_s=0)
        await _until(lambda: job.running)
        assert job.runs == 0
    finally:
        await sched.stop()


async def test_started_paused_nothing_runs_and_resume_is_staggered() -> None:
    sched = Scheduler()
    started: dict[str, float] = {}

    def make(name: str):  # type: ignore[no-untyped-def]
        async def job() -> None:
            started.setdefault(name, time.perf_counter())

        return job

    names = [f"j{i}" for i in range(5)]
    for name in names:
        sched.add(name, make(name), timedelta(hours=1))
    sched.hold(time.time() - 5)
    await sched.start()
    try:
        await asyncio.sleep(0.2)
        assert started == {} and all(j.waiting for j in sched.jobs.values())
        assert sched.paused_since is not None
        t0 = time.perf_counter()
        sched.resume(stagger_s=0.15)
        await _until(lambda: len(started) == 5, timeout=5)
        order = sorted(started, key=lambda n: started[n])
        assert order == names  # registration order
        gaps = [started[b] - started[a] for a, b in itertools.pairwise(order)]
        assert all(g > 0.1 for g in gaps), gaps  # not all at once
        assert started[order[0]] - t0 > 0.1
    finally:
        await sched.stop()


async def test_a_manual_trigger_still_runs_while_paused() -> None:
    sched = Scheduler()
    calls: list[int] = []

    async def job() -> None:
        calls.append(1)

    sched.hold()
    sched.add("j", job, timedelta(hours=1))
    await sched.start()
    try:
        await asyncio.sleep(0.1)
        assert calls == []
        sched.trigger("j")
        await _until(lambda: len(calls) == 1)
        await asyncio.sleep(0.1)
        assert calls == [1]  # once, not a loop
    finally:
        await sched.stop()


async def test_pause_then_quick_resume_and_pause_does_not_leak_a_run() -> None:
    sched = Scheduler()
    calls: list[int] = []

    async def job() -> None:
        calls.append(1)

    sched.hold()
    sched.add("j", job, timedelta(hours=1))
    await sched.start()
    try:
        await asyncio.sleep(0.05)
        sched.resume(stagger_s=1.0)
        await sched.pause()
        await asyncio.sleep(1.3)
        assert calls == []
    finally:
        await sched.stop()


# --------------------------------------------------------------------------- app


class StubSystem(Collector):
    name = "system"

    async def collect(self) -> dict[str, Any]:
        return {"hostname": "box", "memory": {"total": 1000, "used": 600, "percent": 60.0}}


def _settings(**extra: Any) -> Settings:
    return Settings.model_validate(
        {
            "llm": {"base_url": LLM_URL, "model": "fake-model", "max_retries": 0},
            **extra,
        }
    )


def test_api_pause_resume_and_info(bootstrap: Bootstrap) -> None:
    app = create_app(
        bootstrap, collector_registry={"system": StubSystem}, seed_settings=_settings()
    )
    with TestClient(app) as client:
        state = client.get("/api/automation").json()
        assert state["paused"] is False and state["since"] is None
        done = client.post("/api/automation/pause").json()
        assert done["paused"] is True and done["by"] == "ui" and done["since"] > 0
        info = client.get("/api/info").json()
        assert info["automation"]["paused"] is True
        assert info["jobs"] and all(j["paused"] for j in info["jobs"])
        # idempotent
        assert client.post("/api/automation/pause").json()["since"] == done["since"]
        resumed = client.post("/api/automation/resume").json()
        assert resumed["paused"] is False
        assert not any(j["paused"] for j in client.get("/api/info").json()["jobs"])


def test_pause_survives_a_restart_and_nothing_runs(bootstrap: Bootstrap) -> None:
    app = create_app(
        bootstrap, collector_registry={"system": StubSystem}, seed_settings=_settings()
    )
    with TestClient(app) as client:
        assert client.post("/api/automation/pause").status_code == 200
        since = client.get("/api/automation").json()["since"]
    # a fresh process on the same data directory
    app = create_app(bootstrap, collector_registry={"system": StubSystem})
    with TestClient(app) as client:
        state = client.get("/api/automation").json()
        assert state["paused"] is True and state["since"] == since
        time.sleep(0.5)  # collect:system and llm_probe are due right away when not paused
        jobs = client.get("/api/info").json()["jobs"]
        assert {j["name"] for j in jobs} >= {"collect:system", "llm_probe", "report"}
        assert all(j["paused"] and j["runs"] == 0 and not j["running"] for j in jobs), jobs
        client.post("/api/automation/resume")
    app = create_app(bootstrap, collector_registry={"system": StubSystem})
    with TestClient(app) as client:
        assert client.get("/api/automation").json()["paused"] is False


def test_pause_cancels_a_report_being_written(bootstrap: Bootstrap) -> None:
    async def never(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(never),
        collector_registry={"system": StubSystem},
        seed_settings=_settings(),
    )
    with TestClient(app) as client:
        events = app.state.ctx.events.subscribe("report")
        assert client.post("/api/reports/run").status_code == 202
        _wait(lambda: _job(client, "report")["running"])
        done = client.post("/api/automation/pause").json()
        assert "report" in done["cancelled"]
        job = _job(client, "report")
        assert not job["running"] and job["last_status"] == "cancelled"
        assert job["cancelled"] == 1 and job["failures"] == 0 and job["last_error"] is None
        # Nothing stored as a failed report, and watchers were told it was cancelled.
        assert client.get("/api/reports").json()["total"] == 0
        types = []
        while True:
            try:
                types.append(events._queue.get_nowait().type)
            except asyncio.QueueEmpty:
                break
        assert "report.started" in types and types[-1] == "report.cancelled"
        assert "report.failed" not in types


def _job(client: TestClient, name: str) -> dict[str, Any]:
    return next(j for j in client.get("/api/info").json()["jobs"] if j["name"] == name)


def _wait(predicate, timeout: float = 5.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met")


# --------------------------------------------------------------------------- benchmarks


@pytest.fixture(autouse=True)
def _no_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(BenchService, "warmup_enabled", False)


def _bench_app(bootstrap: Bootstrap, llm: FakeLLM, seen: list[Any], holder: dict[str, Any]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(holder["app"].state.ctx.automation.paused)
        return llm.handler(request)

    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(handler),
        collector_registry={"system": StubSystem},
        seed_settings=_settings(),
    )
    holder["app"] = app
    return app


def test_bench_pauses_for_the_run_and_restores(bootstrap: Bootstrap) -> None:
    seen: list[Any] = []
    holder: dict[str, Any] = {}
    app = _bench_app(bootstrap, FakeLLM(say(CANNED_REPORT)), seen, holder)
    with TestClient(app) as client:
        assert client.get("/api/automation").json()["paused"] is False
        seen.clear()  # the start-up probe of the tool mode called the model before the run
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        assert run["status"] == "done"
        assert seen and all(seen)  # automation was paused while the model was being called
        assert run["meta"]["automation"] == {"paused": True, "by_run": True, "was_paused": False}
        after = client.get("/api/automation").json()
        assert after["paused"] is False  # back as it was found
        assert not any(j["paused"] for j in client.get("/api/info").json()["jobs"])


def test_bench_leaves_a_pause_the_user_set(bootstrap: Bootstrap) -> None:
    seen: list[Any] = []
    holder: dict[str, Any] = {}
    app = _bench_app(bootstrap, FakeLLM(say(CANNED_REPORT)), seen, holder)
    with TestClient(app) as client:
        client.post("/api/automation/pause")
        since = client.get("/api/automation").json()["since"]
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        assert run["meta"]["automation"] == {"paused": True, "by_run": False, "was_paused": True}
        after = client.get("/api/automation").json()
        assert after["paused"] is True and after["since"] == since and after["by"] == "ui"


def test_bench_without_the_tick_leaves_automation_running(bootstrap: Bootstrap) -> None:
    seen: list[Any] = []
    holder: dict[str, Any] = {}
    app = _bench_app(bootstrap, FakeLLM(say(CANNED_REPORT)), seen, holder)
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1, pause_automation=False)
        assert seen and not any(seen)
        assert run["meta"]["automation"] == {"paused": False, "by_run": False, "was_paused": False}
        assert client.get("/api/automation").json()["paused"] is False


def test_bench_restores_when_the_run_fails(bootstrap: Bootstrap) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(boom),
        collector_registry={"system": StubSystem},
        seed_settings=_settings(),
    )
    with TestClient(app) as client:
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        assert run["meta"]["automation"]["by_run"] is True
        assert client.get("/api/automation").json()["paused"] is False


def test_bench_restores_when_cancelled(bootstrap: Bootstrap) -> None:
    async def never(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(never),
        collector_registry={"system": StubSystem},
        seed_settings=_settings(),
    )
    with TestClient(app) as client:
        app.state.ctx.bench._probe_transport = probe_transport()
        run_id = client.post("/api/bench/runs", json={"ids": ["report.smoke_disk"]}).json()[
            "run_id"
        ]
        _wait(lambda: client.get("/api/automation").json()["paused"])
        assert client.get("/api/automation").json()["by"] == f"bench #{run_id}"
        assert client.post("/api/bench/cancel").json() == {"cancelled": True}
        wait_done(client)
        assert client.get("/api/automation").json()["paused"] is False


def test_a_pause_left_by_a_dead_bench_run_is_lifted_at_startup(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, collector_registry={}, seed_settings=_settings())
    with TestClient(app) as client:
        ctx = app.state.ctx
        client.portal.call(ctx.pause_automation, "bench #7")
        assert client.get("/api/automation").json()["paused"] is True
    app = create_app(bootstrap, collector_registry={})
    with TestClient(app) as client:
        assert client.get("/api/automation").json()["paused"] is False
