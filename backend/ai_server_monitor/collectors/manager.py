"""Builds, schedules and runs collectors; keeps latest results and persists history."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from ..config import Settings
from ..db import Database
from ..scheduler import Scheduler
from .base import Collector, CollectorResult, CollectorUnavailable, HostEnv

log = logging.getLogger(__name__)

CollectorState = Literal["active", "disabled", "unavailable"]


class CollectorManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        scheduler: Scheduler,
        registry: dict[str, type[Collector]] | None = None,
        env: HostEnv | None = None,
    ):
        from . import REGISTRY

        self.settings = settings
        self.db = db
        self.scheduler = scheduler
        self.registry = registry if registry is not None else REGISTRY
        self.env = env or HostEnv.detect(Path("/host"))
        self.collectors: dict[str, Collector] = {}
        self.states: dict[str, CollectorState] = {}
        self.reasons: dict[str, str] = {}
        self.latest: dict[str, CollectorResult] = {}
        self._last_persist: dict[str, float] = {}
        self.on_result: Callable[[CollectorResult], None] | None = None
        """Called (synchronously, errors ignored) after every collector run."""

    async def setup(self) -> None:
        """Instantiate enabled collectors, check availability, register scheduler jobs."""
        log.info("host env: root=%s (container=%s)", self.env.root, self.env.in_container)
        for name, cls in self.registry.items():
            cfg = getattr(self.settings.collectors, name)
            if cfg.enabled is False:
                self.states[name] = "disabled"
                continue
            # Nothing a single collector does may stop the service from starting.
            try:
                collector = cls(cfg, self.env)
                collector.app_settings = self.settings
            except Exception as exc:
                self.states[name] = "unavailable"
                self.reasons[name] = f"cannot start: {exc}"
                log.warning("collector %s cannot start: %s", name, exc)
                continue
            try:
                async with asyncio.timeout(collector.timeout.total_seconds()):
                    await collector.check()
            except Exception as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, CollectorUnavailable)
                    else f"check failed: {type(exc).__name__}: {exc}".rstrip(": ")
                )
                self.reasons[name] = reason
                if cfg.enabled == "auto":
                    self.states[name] = "unavailable"
                    log.info("collector %s unavailable: %s", name, reason)
                    await self._close_quietly(collector)
                    continue
                # Explicitly enabled: keep trying, it will report the error each run.
                log.warning("collector %s enabled but unavailable: %s", name, reason)
            self.collectors[name] = collector
            self.states[name] = "active"
            self.scheduler.add(f"collect:{name}", self._job(name), cfg.interval)

    def _job(self, name: str):  # type: ignore[no-untyped-def]
        async def run() -> None:
            await self.run(name)

        return run

    async def run(self, name: str) -> CollectorResult:
        collector = self.collectors[name]
        started = time.perf_counter()
        ts = time.time()
        try:
            async with asyncio.timeout(collector.timeout.total_seconds()):
                data = await collector.collect()
            result = CollectorResult(name, ts, ok=True, data=data)
            self.reasons.pop(name, None)
        except CollectorUnavailable as exc:
            result = CollectorResult(name, ts, ok=False, error=str(exc), unavailable=True)
            self.reasons[name] = str(exc)
        except TimeoutError:
            result = CollectorResult(
                name, ts, ok=False, error=f"timed out after {collector.timeout}"
            )
            log.warning("collector %s timed out", name)
        except Exception as exc:
            result = CollectorResult(
                name, ts, ok=False, error=f"{type(exc).__name__}: {exc}".rstrip(": ")
            )
            log.warning("collector %s failed: %s", name, exc, exc_info=log.isEnabledFor(10))
        result.duration_s = round(time.perf_counter() - started, 3)
        self.latest[name] = result
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception:
                log.exception("on_result hook failed")
        await self._maybe_persist(collector, result)
        return result

    async def _maybe_persist(self, collector: Collector, result: CollectorResult) -> None:
        spacing = self.settings.storage.sample_interval.total_seconds()
        last = self._last_persist.get(result.name)
        if last is not None and result.ts - last < spacing:
            return
        data = collector.persist_view(result.data) if result.ok else {}
        await self.db.execute(
            "INSERT INTO collector_samples (ts, collector, ok, data, error) VALUES (?, ?, ?, ?, ?)",
            (result.ts, result.name, int(result.ok), json.dumps(data), result.error),
        )
        self._last_persist[result.name] = result.ts

    def status(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for name in self.registry:
            latest = self.latest.get(name)
            out[name] = {
                "state": self.states.get(name, "disabled"),
                "reason": self.reasons.get(name),
                "last_ts": latest.ts if latest else None,
                "ok": latest.ok if latest else None,
                "error": latest.error if latest else None,
                "duration_s": latest.duration_s if latest else None,
            }
        return out

    @staticmethod
    async def _close_quietly(collector: Collector) -> None:
        try:
            await collector.close()
        except Exception:
            log.exception("closing collector %s", collector.name)

    async def close(self) -> None:
        for collector in self.collectors.values():
            await self._close_quietly(collector)
