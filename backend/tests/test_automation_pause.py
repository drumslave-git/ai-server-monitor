"""The schedule (APScheduler) and the global "pause automation" switch."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app
from ai_server_monitor.schedule import Schedule

from .conftest import LLM_URL
from .jobfakes import ScriptedModels, finish, tool


async def _until(predicate, timeout: float = 4.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def _settings(**extra: Any) -> Settings:
    return Settings.model_validate(
        {"llm": {"base_url": LLM_URL, "model": "fake-model", "max_retries": 0}, **extra}
    )


# --------------------------------------------------------------------------- the schedule


async def test_housekeeping_runs_and_a_paused_schedule_holds_it_until_resume() -> None:
    runs: list[float] = []

    async def work() -> None:
        runs.append(time.monotonic())

    sched = Schedule(paused=True)
    sched.add_housekeeping("work", work, timedelta(hours=1), initial_delay=timedelta(0), jitter_s=0)
    sched.start()
    try:
        await asyncio.sleep(0.4)
        assert runs == [] and sched.paused
        assert sched.status()[0]["paused"] and sched.status()[0]["next_run"] is None
        sched.resume()
        await _until(lambda: len(runs) == 1)  # the missed run happens once on resume
        [status] = sched.status()
        assert status["runs"] == 1 and status["last_status"] == "ok" and not sched.paused
        assert status["next_run"] > time.time() + 3000  # then hourly
    finally:
        sched.stop()


async def test_failures_and_timeouts_are_recorded() -> None:
    async def boom() -> None:
        raise RuntimeError("disk on fire")

    async def slow() -> None:
        await asyncio.sleep(5)

    sched = Schedule()
    sched.add_housekeeping("boom", boom, timedelta(hours=1), initial_delay=timedelta(0), jitter_s=0)
    sched.add_housekeeping(
        "slow", slow, timedelta(hours=1), initial_delay=timedelta(0), timeout_s=0.2, jitter_s=0
    )
    sched.start()
    try:
        await _until(lambda: all(s["runs"] == 1 for s in sched.status()))
        by_name = {s["name"]: s for s in sched.status()}
        assert by_name["boom"]["last_status"] == "error"
        assert "disk on fire" in by_name["boom"]["last_error"]
        assert by_name["slow"]["last_status"] == "timeout"
    finally:
        sched.stop()


async def test_sync_jobs_follows_the_table() -> None:
    ran: list[str] = []

    async def run_job(slug: str) -> None:
        ran.append(slug)

    jobs = [
        {"slug": "a", "enabled": True, "interval_s": 1800, "cron": None},
        {"slug": "b", "enabled": True, "interval_s": None, "cron": "0 6 * * *"},
        {"slug": "c", "enabled": False, "interval_s": 600, "cron": None},
    ]
    sched = Schedule()
    sched.start()
    try:
        sched.sync_jobs(jobs, run_job)
        assert {s["name"] for s in sched.status()} == {"job:a", "job:b"}
        first = sched.next_run("a")
        assert first is not None and 1700 < first - time.time() <= 1800
        assert sched.next_run("b") is not None and sched.next_run("c") is None
        # unchanged jobs keep their next run; a changed trigger replaces it; removed ones go
        sched.sync_jobs(jobs, run_job)
        assert sched.next_run("a") == first
        jobs[0]["interval_s"] = 60
        del jobs[1]
        sched.sync_jobs(jobs, run_job)
        assert sched.next_run("a") - time.time() <= 60  # type: ignore[operator]
        assert {s["name"] for s in sched.status()} == {"job:a"}
        sched.pause()
        assert sched.next_run("a") is None
        assert ran == []
    finally:
        sched.stop()


# --------------------------------------------------------------------------- the app


def test_api_pause_resume_and_info(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, seed_settings=_settings())
    with TestClient(app) as client:
        state = client.get("/api/automation").json()
        assert state["paused"] is False and state["since"] is None
        assert client.get("/api/jobs/summary").json()["next_run"] is not None
        done = client.post("/api/automation/pause").json()
        assert done["paused"] is True and done["by"] == "ui" and done["since"] > 0
        info = client.get("/api/info").json()
        assert info["automation"]["paused"] is True
        names = {j["name"] for j in info["jobs"]}
        assert {"prune", "trace_prune", "approvals:sweep", "job:summary", "job:storage"} <= names
        assert all(j["paused"] for j in info["jobs"])
        assert client.get("/api/jobs/summary").json()["next_run"] is None
        # idempotent
        assert client.post("/api/automation/pause").json()["since"] == done["since"]
        resumed = client.post("/api/automation/resume").json()
        assert resumed["paused"] is False
        assert not any(j["paused"] for j in client.get("/api/info").json()["jobs"])


def test_pause_survives_a_restart_and_nothing_runs(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, seed_settings=_settings())
    with TestClient(app) as client:
        assert client.post("/api/automation/pause").status_code == 200
        since = client.get("/api/automation").json()["since"]
    # a fresh process on the same data directory
    app = create_app(bootstrap)
    with TestClient(app) as client:
        state = client.get("/api/automation").json()
        assert state["paused"] is True and state["since"] == since
        time.sleep(1.5)  # the approval jobs would be due by now
        jobs = client.get("/api/info").json()["jobs"]
        assert {j["name"] for j in jobs} >= {"approvals:sweep", "approvals:drain"}
        assert all(j["paused"] and j["runs"] == 0 and not j["running"] for j in jobs), jobs
        client.post("/api/automation/resume")
    app = create_app(bootstrap)
    with TestClient(app) as client:
        assert client.get("/api/automation").json()["paused"] is False


def test_schedule_follows_job_changes(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, seed_settings=_settings())
    with TestClient(app) as client:
        body = {"slug": "certs", "name": "Certificates", "prompt": "Check TLS certificates.",
                "interval_s": 7200}  # fmt: skip
        created = client.post("/api/jobs", json=body).json()
        assert 7100 < created["next_run"] - time.time() <= 7200
        names = lambda: {j["name"] for j in client.get("/api/info").json()["jobs"]}  # noqa: E731
        assert "job:certs" in names()
        cron = client.put("/api/jobs/certs", json={"cron": "15 3 * * *"}).json()
        assert cron["next_run"] is not None and cron["interval_s"] is None
        assert client.put("/api/jobs/certs", json={"enabled": False}).json()["next_run"] is None
        assert "job:certs" not in names()
        client.put("/api/jobs/storage", json={"enabled": False})
        assert "job:storage" not in names()
        client.delete("/api/jobs/certs")
        assert "job:certs" not in names()


def test_pause_cancels_a_scheduled_run_and_manual_runs_still_work(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    app = create_app(bootstrap, seed_settings=_settings())
    with TestClient(app) as client:
        ctx = app.state.ctx
        models.script = ["<sleep>"]

        async def scheduled_then_pause() -> list[str]:
            task = asyncio.create_task(ctx._scheduled_run("docker"))
            await _until(lambda: bool(ctx.jobs._running))
            cancelled = await ctx.pause_automation("api")
            await asyncio.gather(task, return_exceptions=True)
            return cancelled

        cancelled = client.portal.call(scheduled_then_pause)  # type: ignore[union-attr]
        assert cancelled == ["job:docker"]
        assert client.get("/api/automation").json()["jobs_cancelled"] == ["job:docker"]
        [stopped] = client.get("/api/jobs/docker/runs").json()["items"]
        assert stopped["status"] == "error" and stopped["trigger"] == "schedule"
        assert stopped["error"] == "cancelled (automation paused)"
        # what a person starts still runs while paused
        models.script = [tool("bash", command="docker ps"), finish("ok", "All containers up.")]
        manual = client.portal.call(ctx.jobs.run, "docker", "manual")  # type: ignore[union-attr]
        assert manual["status"] == "ok"
