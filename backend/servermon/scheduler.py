"""Minimal asyncio interval scheduler.

Each job runs in its own task: run, then sleep `interval`, repeat. A job never overlaps
with itself. `trigger(name)` wakes a sleeping job immediately (a trigger during a run
causes one more run right after). Exceptions are logged and recorded, never fatal.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

log = logging.getLogger(__name__)

JobFunc = Callable[[], Awaitable[Any]]


@dataclass
class Job:
    name: str
    func: JobFunc
    interval: timedelta
    initial_delay: timedelta = timedelta(0)
    timeout: timedelta | None = None
    # runtime state
    runs: int = 0
    failures: int = 0
    running: bool = False
    last_start: float | None = None
    last_end: float | None = None
    last_duration: float | None = None
    last_error: str | None = None
    next_run: float | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_s": self.interval.total_seconds(),
            "runs": self.runs,
            "failures": self.failures,
            "running": self.running,
            "last_start": self.last_start,
            "last_end": self.last_end,
            "last_duration_s": self.last_duration,
            "last_error": self.last_error,
            "next_run": self.next_run,
        }


class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._started = False

    @property
    def jobs(self) -> dict[str, Job]:
        return self._jobs

    def add(
        self,
        name: str,
        func: JobFunc,
        interval: timedelta,
        *,
        initial_delay: timedelta = timedelta(0),
        timeout: timedelta | None = None,
    ) -> Job:
        if name in self._jobs:
            raise ValueError(f"job {name!r} already registered")
        if interval.total_seconds() <= 0:
            raise ValueError(f"job {name!r}: interval must be positive")
        job = Job(name, func, interval, initial_delay, timeout)
        self._jobs[name] = job
        if self._started:
            job._task = asyncio.create_task(self._loop(job), name=f"job:{name}")
        return job

    async def start(self) -> None:
        self._started = True
        for job in self._jobs.values():
            if job._task is None:
                job._task = asyncio.create_task(self._loop(job), name=f"job:{job.name}")

    async def stop(self) -> None:
        self._started = False
        tasks = [j._task for j in self._jobs.values() if j._task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in self._jobs.values():
            job._task = None

    def trigger(self, name: str) -> None:
        self._jobs[name]._wake.set()

    def status(self) -> list[dict[str, Any]]:
        return [job.status() for job in self._jobs.values()]

    async def run_once(self, name: str) -> None:
        """Run a job inline (outside its loop). Mainly for tests and manual triggers."""
        await self._run(self._jobs[name])

    # ------------------------------------------------------------------ internals

    async def _loop(self, job: Job) -> None:
        await self._sleep(job, job.initial_delay)
        while True:
            await self._run(job)
            await self._sleep(job, job.interval)

    async def _sleep(self, job: Job, delay: timedelta) -> None:
        seconds = delay.total_seconds()
        job.next_run = time.time() + seconds
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(job._wake.wait(), timeout=seconds)

    async def _run(self, job: Job) -> None:
        job._wake.clear()
        job.running = True
        job.next_run = None
        job.last_start = time.time()
        started = time.perf_counter()
        try:
            if job.timeout is not None:
                async with asyncio.timeout(job.timeout.total_seconds()):
                    await job.func()
            else:
                await job.func()
            job.last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job.failures += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("job %s failed", job.name)
        finally:
            job.runs += 1
            job.running = False
            job.last_end = time.time()
            job.last_duration = time.perf_counter() - started
