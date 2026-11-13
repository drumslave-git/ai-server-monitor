"""Application context: owns long-lived services and their lifecycle."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

import httpx

from .config import Settings
from .db import Database
from .llm import LLMClient, ToolModeResolver
from .scheduler import Scheduler

log = logging.getLogger(__name__)

LLM_PROBE_RETRY = timedelta(minutes=5)


@dataclass
class AppContext:
    settings: Settings
    db: Database
    scheduler: Scheduler
    llm: LLMClient
    tool_mode: ToolModeResolver
    started_at: float = field(default_factory=time.time)

    @classmethod
    def build(
        cls, settings: Settings, *, llm_transport: httpx.AsyncBaseTransport | None = None
    ) -> AppContext:
        db = Database(settings.storage.path)
        llm = LLMClient(settings.llm, transport=llm_transport)
        return cls(
            settings=settings,
            db=db,
            scheduler=Scheduler(),
            llm=llm,
            tool_mode=ToolModeResolver(llm, db, settings.llm.tool_mode),
        )

    def _register_jobs(self) -> None:
        storage = self.settings.storage
        self.scheduler.add(
            "prune",
            lambda: self.db.prune(storage.retention),
            storage.prune_interval,
            initial_delay=timedelta(seconds=30),
        )
        # Retries until the LLM is reachable; becomes a no-op once resolved.
        self.scheduler.add(
            "llm_probe",
            self.tool_mode.resolve,
            LLM_PROBE_RETRY,
            timeout=timedelta(seconds=self.settings.llm.timeout.total_seconds() * 2 + 30),
        )

    async def start(self) -> None:
        await self.db.connect()
        self._register_jobs()
        await self.scheduler.start()
        log.info(
            "started (db=%s, llm=%s @ %s)", self.db.path, self.llm.model, self.settings.llm.base_url
        )

    async def stop(self) -> None:
        await self.scheduler.stop()
        await self.llm.aclose()
        await self.db.close()
        log.info("stopped")
