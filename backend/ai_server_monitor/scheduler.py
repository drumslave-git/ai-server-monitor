"""Minimal asyncio interval scheduler.

Each job runs in its own task: run, then sleep `interval`, repeat. A job never overlaps
with itself. `trigger(name)` wakes a sleeping job immediately (a trigger during a run
causes one more run right after). Exceptions are logged and recorded, never fatal.

The scheduler can be paused as a whole (the global "automation paused" switch). `pause()` cancels
the runs in flight (recorded as `cancelled`, not as failures) and makes every job wait at a gate
when it comes due; `resume()` releases them one after another, a couple of seconds apart, so the
whole app does not fire at once. A manual `trigger(name)` still runs while paused: someone asked.
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

RESUME_STAGGER_S = 2.0
"""Gap between two jobs released by `resume()`, in the order they were registered."""


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
    cancelled: int = 0
    running: bool = False
    last_start: float | None = None
    last_end: float | None = None
    last_duration: float | None = None
    last_error: str | None = None
    next_run: float | None = None
    last_status: str | None = None
    """ok | failed | cancelled (by the pause switch); None before the first run."""
    paused: bool = False
    """The pause switch is on: the job does not run by itself."""
    waiting: bool = False
    """Due, but held back by the pause switch."""
    _wake: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _current: asyncio.Future[Any] | None = field(default=None, repr=False)
    _idle: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    """Set while no run is in flight (`pause()` waits on it)."""
    _gate: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _manual: bool = field(default=False, repr=False)
    _pause_cancel: bool = field(default=False, repr=False)
    _resume_delay: float = field(default=0.0, repr=False)

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interval_s": self.interval.total_seconds(),
            "runs": self.runs,
            "failures": self.failures,
            "cancelled": self.cancelled,
            "running": self.running,
            "paused": self.paused,
            "waiting": self.waiting,
            "last_status": self.last_status,
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
        self._paused = False
        self.paused_since: float | None = None

    @property
    def jobs(self) -> dict[str, Job]:
        return self._jobs

    @property
    def paused(self) -> bool:
        return self._paused

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
        job._idle.set()
        job.paused = self._paused
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
        """Run a job now. While paused this is a manual run: it goes through the gate once."""
        job = self._jobs[name]
        if self._paused:
            job._manual = True
            job._gate.set()
        job._wake.set()

    def status(self) -> list[dict[str, Any]]:
        return [job.status() for job in self._jobs.values()]

    async def run_once(self, name: str) -> None:
        """Run a job inline (outside its loop). Mainly for tests and manual triggers."""
        await self._run(self._jobs[name])

    # ------------------------------------------------------------------ pause switch

    def hold(self, since: float | None = None) -> None:
        """Start out paused (before `start()`): nothing runs and nothing is in flight."""
        self._paused = True
        self.paused_since = since if since is not None else time.time()
        for job in self._jobs.values():
            job.paused = True

    async def pause(self, *, grace_s: float = 10.0) -> list[str]:
        """Stop all automatic work: the runs in flight are cancelled (recorded as `cancelled`)
        and every job waits at its gate from now on. Returns the names of the cancelled runs.
        Returns once they have finished unwinding."""
        if not self._paused:
            self._paused = True
            self.paused_since = time.time()
        for job in self._jobs.values():
            job.paused = True
            job._gate.clear()
        cancelled: list[str] = []
        futures: list[asyncio.Future[Any]] = []
        for job in self._jobs.values():
            fut = job._current
            if fut is not None and not fut.done() and not job._manual:
                job._pause_cancel = True
                fut.cancel()
                cancelled.append(job.name)
                futures.append(fut)
        if futures:
            await asyncio.wait(futures, timeout=grace_s)
            idle = [self._jobs[name]._idle.wait() for name in cancelled]
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.gather(*idle), timeout=grace_s)
        return cancelled

    def resume(self, *, stagger_s: float = RESUME_STAGGER_S) -> None:
        """Let the jobs run again. The ones that came due while paused start one after another,
        `stagger_s` apart (in registration order); the others keep their normal schedule."""
        if not self._paused:
            return
        self._paused = False
        self.paused_since = None
        slot = 0
        for job in self._jobs.values():
            job.paused = False
            job._manual = False
            if job.waiting:
                slot += 1
                job._resume_delay = slot * stagger_s
            job._gate.set()

    # ------------------------------------------------------------------ internals

    async def _loop(self, job: Job) -> None:
        await self._sleep(job, job.initial_delay)
        while True:
            await self._wait_gate(job)
            await self._run(job)
            if job.last_status == "cancelled":
                continue  # that run did not happen: still due, it starts after the resume
            await self._sleep(job, job.interval)

    async def _wait_gate(self, job: Job) -> None:
        """Hold a due job back while the scheduler is paused (a manual trigger passes once)."""
        if not self._paused:
            return
        job.waiting = True
        job.next_run = None
        try:
            while self._paused and not job._manual:
                await job._gate.wait()
                if self._paused and not job._manual:
                    job._gate.clear()  # a stale release: paused again before we woke up
        finally:
            job.waiting = False
        if self._paused:
            job._gate.clear()  # a manual pass, once
            return
        delay, job._resume_delay = job._resume_delay, 0.0
        if delay > 0:
            job.next_run = time.time() + delay
            await asyncio.sleep(delay)

    async def _sleep(self, job: Job, delay: timedelta) -> None:
        seconds = delay.total_seconds()
        job.next_run = time.time() + seconds
        if seconds <= 0:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(job._wake.wait(), timeout=seconds)

    async def _run(self, job: Job) -> None:
        job._wake.clear()
        job._idle.clear()
        job.running = True
        job.next_run = None
        job.last_start = time.time()
        started = time.perf_counter()
        counted = True
        # The body runs in its own task so that the pause switch can cancel it alone.
        fut = asyncio.ensure_future(job.func())
        job._current = fut
        try:
            if job.timeout is not None:
                async with asyncio.timeout(job.timeout.total_seconds()):
                    await fut
            else:
                await fut
            job.last_error = None
            job.last_status = "ok"
        except asyncio.CancelledError:
            if not job._pause_cancel:
                raise  # the scheduler itself is stopping
            # Cancelled by the pause switch: a clean stop, not a failure.
            counted = False
            job.cancelled += 1
            job.last_status = "cancelled"
            job.last_error = None
            log.info("job %s cancelled: automation paused", job.name)
        except Exception as exc:
            job.failures += 1
            job.last_error = f"{type(exc).__name__}: {exc}"
            job.last_status = "failed"
            log.exception("job %s failed", job.name)
        finally:
            job._current = None
            job._pause_cancel = False
            job._manual = False
            if counted:
                job.runs += 1
            job.running = False
            job._idle.set()
            job.last_end = time.time()
            job.last_duration = time.perf_counter() - started
