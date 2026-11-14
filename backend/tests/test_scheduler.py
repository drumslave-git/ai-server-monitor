from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from ai_server_monitor.scheduler import Scheduler


async def _until(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_runs_immediately_then_waits_and_trigger_wakes() -> None:
    sched = Scheduler()
    calls: list[int] = []

    async def job() -> None:
        calls.append(1)

    sched.add("j", job, timedelta(hours=1))
    await sched.start()
    try:
        await _until(lambda: len(calls) == 1)
        await asyncio.sleep(0.05)
        assert len(calls) == 1  # sleeping for an hour
        sched.trigger("j")
        await _until(lambda: len(calls) == 2)
    finally:
        await sched.stop()


async def test_failures_are_recorded_and_job_keeps_running() -> None:
    sched = Scheduler()
    attempts = 0

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("first run fails")

    job = sched.add("flaky", flaky, timedelta(milliseconds=20))
    await sched.start()
    try:
        await _until(lambda: attempts >= 2)
        await _until(lambda: not job.running)
    finally:
        await sched.stop()
    assert job.failures == 1
    assert job.last_error is None  # cleared by the later successful run
    assert job.runs >= 2


async def test_timeout() -> None:
    sched = Scheduler()

    async def slow() -> None:
        await asyncio.sleep(10)

    job = sched.add("slow", slow, timedelta(hours=1), timeout=timedelta(milliseconds=50))
    await sched.run_once("slow")
    assert job.failures == 1
    assert job.last_error and job.last_error.startswith("TimeoutError")


async def test_initial_delay_and_status() -> None:
    sched = Scheduler()
    calls = 0

    async def job() -> None:
        nonlocal calls
        calls += 1

    sched.add("late", job, timedelta(hours=1), initial_delay=timedelta(hours=1))
    await sched.start()
    try:
        await asyncio.sleep(0.05)
        assert calls == 0
        (status,) = sched.status()
        assert status["name"] == "late" and status["next_run"] is not None
        sched.trigger("late")
        await _until(lambda: calls == 1)
    finally:
        await sched.stop()


def test_rejects_duplicate_and_zero_interval() -> None:
    sched = Scheduler()

    async def job() -> None: ...

    sched.add("a", job, timedelta(seconds=1))
    with pytest.raises(ValueError):
        sched.add("a", job, timedelta(seconds=1))
    with pytest.raises(ValueError):
        sched.add("b", job, timedelta(0))
