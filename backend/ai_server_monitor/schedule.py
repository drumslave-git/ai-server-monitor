"""Everything that runs by itself, on APScheduler (3.x, in memory): the LLM jobs from the `jobs`
table (the source of truth: `sync_jobs` after every change) and the housekeeping jobs
(pruning, approval sweeps).

Each job runs at most once at a time; runs missed while paused or busy are coalesced into one.
The pause switch pauses the whole scheduler (`automation.py`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

log = logging.getLogger(__name__)

JOB_PREFIX = "job:"
HOUSEKEEPING_TIMEOUT_S = 600.0
HOUSEKEEPING_JITTER_S = 30.0
HOUSEKEEPING_GRACE_S = 5
MIN_DELAY = timedelta(seconds=0.2)


@dataclass
class _Stats:
    name: str
    kind: str
    """housekeeping | job"""
    runs: int = 0
    running: bool = False
    last_run: float | None = None
    last_status: str | None = None
    """ok | error | timeout | cancelled"""
    last_error: str | None = None


def job_trigger(job: dict[str, Any], tz: tzinfo | None = None) -> BaseTrigger:
    """Interval or cron trigger; cron times are in `tz` (`general.timezone`, default the
    host's)."""
    if job.get("interval_s"):
        return IntervalTrigger(seconds=int(job["interval_s"]))
    return CronTrigger.from_crontab(job["cron"], timezone=tz or UTC)


class Schedule:
    def __init__(self, *, paused: bool = False, timezone: tzinfo | None = None):
        self.timezone = timezone or UTC
        self._sched = AsyncIOScheduler(
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
            timezone=self.timezone,
        )
        self._start_paused = paused
        self._stats: dict[str, _Stats] = {}

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._sched.start(paused=self._start_paused)

    def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    @property
    def paused(self) -> bool:
        from apscheduler.schedulers.base import STATE_PAUSED

        return self._sched.state == STATE_PAUSED or (not self._sched.running and self._start_paused)

    def pause(self) -> None:
        self._start_paused = True
        if self._sched.running:
            self._sched.pause()

    def resume(self) -> None:
        self._start_paused = False
        if self._sched.running:
            self._sched.resume()

    # ------------------------------------------------------------------ jobs

    def add_housekeeping(
        self,
        name: str,
        func: Callable[[], Awaitable[Any]],
        every: timedelta,
        *,
        initial_delay: timedelta = timedelta(seconds=1),
        timeout_s: float = HOUSEKEEPING_TIMEOUT_S,
        jitter_s: float | None = None,
    ) -> None:
        """`jitter_s`: random delay added to every run time (default 10 % of the interval, at
        most HOUSEKEEPING_JITTER_S), so the housekeeping jobs do not all fire together."""
        stats = self._stats.setdefault(name, _Stats(name, "housekeeping"))

        async def run() -> None:
            await self._run(stats, func, timeout_s)

        self._sched.add_job(
            run,
            IntervalTrigger(
                seconds=max(every.total_seconds(), 1.0),
                # (a start time already past would wait a whole interval)
                start_date=datetime.now(UTC) + max(initial_delay, MIN_DELAY),
                # spread the housekeeping jobs over time instead of all at the same second
                jitter=(
                    min(every.total_seconds() * 0.1, HOUSEKEEPING_JITTER_S)
                    if jitter_s is None
                    else jitter_s
                )
                or None,
            ),
            id=name,
            name=name,
            replace_existing=True,
            # A run missed by more than this (the schedule was paused) is skipped, not
            # caught up: after a pause they come back on their own jittered times, not at once.
            misfire_grace_time=HOUSEKEEPING_GRACE_S,
        )

    def sync_jobs(
        self, jobs: list[dict[str, Any]], run_job: Callable[[str], Awaitable[Any]]
    ) -> None:
        """Make the scheduled LLM jobs match the table: enabled ones with their trigger,
        nothing else."""
        wanted = {JOB_PREFIX + j["slug"]: j for j in jobs if j["enabled"]}
        for existing in self._sched.get_jobs():
            if existing.id.startswith(JOB_PREFIX) and existing.id not in wanted:
                existing.remove()
                self._stats.pop(existing.id, None)
        for job_id, job in wanted.items():
            stats = self._stats.setdefault(job_id, _Stats(job_id, "job"))
            slug = job["slug"]

            async def run(slug: str = slug, stats: _Stats = stats) -> None:
                await self._run(stats, lambda: run_job(slug), None)

            trigger = job_trigger(job, self.timezone)
            current = self._sched.get_job(job_id)
            if current is not None and str(current.trigger) == str(trigger):
                continue  # unchanged: keep its next run time
            self._sched.add_job(run, trigger, id=job_id, name=job_id, replace_existing=True)

    def run_soon(self, slugs: list[str], first: timedelta, spacing: timedelta) -> list[str]:
        """Move the next run of these (scheduled) jobs forward to `first`, `first + spacing`, …
        in this order; their trigger takes over after that run. Disabled jobs are left out.
        A paused schedule runs them when it is resumed. Returns the slugs moved."""
        moved = []
        when = datetime.now(UTC) + first
        for slug in slugs:
            job = self._sched.get_job(JOB_PREFIX + slug)
            if job is None:
                continue
            job.modify(next_run_time=when)
            moved.append(slug)
            when += spacing
        return moved

    def next_run(self, slug: str) -> float | None:
        job = self._sched.get_job(JOB_PREFIX + slug)
        if job is None or self.paused:
            return None
        when = getattr(job, "next_run_time", None)
        return when.timestamp() if when else None

    def status(self) -> list[dict[str, Any]]:
        out = []
        for job in self._sched.get_jobs():
            stats = self._stats.get(job.id) or _Stats(job.id, "job")
            when = getattr(job, "next_run_time", None)
            out.append(
                {
                    "name": job.id,
                    "kind": stats.kind,
                    "paused": self.paused,
                    "next_run": when.timestamp() if when and not self.paused else None,
                    "runs": stats.runs,
                    "running": stats.running,
                    "last_run": stats.last_run,
                    "last_status": stats.last_status,
                    "last_error": stats.last_error,
                }
            )
        return sorted(out, key=lambda j: j["name"])

    async def _run(
        self, stats: _Stats, func: Callable[[], Awaitable[Any]], timeout_s: float | None
    ) -> None:
        stats.running = True
        stats.last_run = time.time()
        try:
            async with asyncio.timeout(timeout_s):
                await func()
            stats.last_status, stats.last_error = "ok", None
        except TimeoutError:
            stats.last_status, stats.last_error = "timeout", f"over {timeout_s:.0f}s"
            log.warning("scheduled %s timed out", stats.name)
        except asyncio.CancelledError:
            stats.last_status, stats.last_error = "cancelled", None
        except Exception as exc:
            stats.last_status, stats.last_error = "error", f"{type(exc).__name__}: {exc}"
            log.exception("scheduled %s failed", stats.name)
        finally:
            stats.runs += 1
            stats.running = False
