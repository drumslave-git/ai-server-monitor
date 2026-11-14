"""Rule-driven automations on top of the action layer (stalled cleanup, searches, triage)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from ...scheduler import Scheduler
from . import search, stalled, triage
from .base import AutomationEnv

log = logging.getLogger(__name__)

INITIAL_DELAY = timedelta(minutes=2)
JOB_TIMEOUT = timedelta(minutes=5)


def register(scheduler: Scheduler, env: AutomationEnv) -> list[str]:
    """Schedule the automations whose policy is not `off` and whose targets are configured.
    Returns the job names."""
    acts = env.settings.arr.actions
    arr = env.settings.arr
    has_apps = bool(arr.radarr or arr.sonarr or arr.whisparr)
    plan: list[tuple[str, Any, bool, Callable[[AutomationEnv], Awaitable[dict[str, Any]]]]] = [
        ("stalled_cleanup", acts.stalled_cleanup, has_apps or bool(arr.qbittorrent), stalled.run),
        (
            "missing_search",
            acts.missing_search,
            has_apps,
            lambda e: search.run(e, "missing"),
        ),
        (
            "cutoff_search",
            acts.cutoff_search,
            has_apps,
            lambda e: search.run(e, "cutoff"),
        ),
        ("indexer_triage", acts.indexer_triage, bool(arr.prowlarr), triage.run),
    ]
    jobs = []
    for name, cfg, configured, runner in plan:
        if cfg.policy == "off" or not configured:
            continue

        async def job(
            runner: Callable[..., Awaitable[dict[str, Any]]] = runner, name: str = name
        ) -> None:
            summary = await runner(env)
            log.info("automation %s: %s", name, summary)

        scheduler.add(
            f"auto:{name}", job, cfg.interval, initial_delay=INITIAL_DELAY, timeout=JOB_TIMEOUT
        )
        jobs.append(f"auto:{name}")
    return jobs


__all__ = ["AutomationEnv", "register", "search", "stalled", "triage"]
