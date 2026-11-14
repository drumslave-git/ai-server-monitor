"""Application context: owns long-lived services and their lifecycle.

Settings come from the database and can change at runtime (web UI). `apply_settings`
saves them and rebuilds the settings-dependent services (scheduler, LLM client,
collectors) in place; the database connection stays open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

import httpx

from .actions import ActionService, Notifier
from .arr.automations import AutomationEnv
from .arr.automations import register as register_automations
from .collectors import Collector, CollectorManager, HostEnv
from .config import Bootstrap, Settings
from .db import Database
from .llm import LLMClient, ToolModeResolver
from .scheduler import Scheduler
from .settings_store import SettingsStore

log = logging.getLogger(__name__)

LLM_PROBE_RETRY = timedelta(minutes=5)
APPROVAL_SWEEP = timedelta(minutes=1)


class AppContext:
    def __init__(
        self,
        bootstrap: Bootstrap,
        *,
        llm_transport: httpx.AsyncBaseTransport | None = None,
        collector_registry: dict[str, type[Collector]] | None = None,
        host_env: HostEnv | None = None,
        seed_settings: Settings | None = None,
        arr_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.bootstrap = bootstrap
        self.db = Database(bootstrap.db_path)
        self.store = SettingsStore(self.db)
        self.host_env = host_env or HostEnv.detect(bootstrap.host_root)
        self.started_at = time.time()
        self._llm_transport = llm_transport
        self._registry = collector_registry
        self._seed = seed_settings
        self._arr_transport = arr_transport
        self._lock = asyncio.Lock()
        self.action_notifiers: list[Notifier] = []
        """Callables `(event, record)` told about suggested/pending/finished actions.
        Survives settings changes; the event bus subscribes here."""
        # Built in start() / apply_settings():
        self.settings = Settings()
        self.scheduler = Scheduler()
        self.llm = LLMClient(self.settings.llm, transport=llm_transport)
        self.tool_mode = ToolModeResolver(self.llm, self.db, self.settings.llm.tool_mode)
        self.collectors = CollectorManager(
            self.settings, self.db, self.scheduler, registry=collector_registry, env=self.host_env
        )
        self.actions = ActionService(
            self.db, self.settings, notifiers=self.action_notifiers, transport=arr_transport
        )

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.db.connect()
        if self._seed is not None:
            await self.store.save(self._seed)
        self.settings = await self.store.load()
        await self._start_services()
        log.info("started (data=%s, host root=%s)", self.bootstrap.data_dir, self.host_env.root)

    async def stop(self) -> None:
        await self._stop_services()
        await self.db.close()
        log.info("stopped")

    async def apply_settings(self, settings: Settings) -> None:
        """Persist new settings and restart the services that depend on them."""
        async with self._lock:
            await self.store.save(settings)
            await self._stop_services()
            self.settings = settings
            await self._start_services()
            log.info("settings applied")

    # ------------------------------------------------------------------ internals

    async def _start_services(self) -> None:
        s = self.settings
        logging.getLogger().setLevel(s.general.log_level.upper())
        self.scheduler = Scheduler()
        self.llm = LLMClient(s.llm, transport=self._llm_transport)
        self.tool_mode = ToolModeResolver(self.llm, self.db, s.llm.tool_mode)
        self.collectors = CollectorManager(
            s, self.db, self.scheduler, registry=self._registry, env=self.host_env
        )
        self.scheduler.add(
            "prune",
            lambda: self.db.prune(s.storage.retention),
            s.storage.prune_interval,
            initial_delay=timedelta(seconds=30),
        )
        # Retries until the LLM is reachable; a no-op once resolved or if not configured.
        self.scheduler.add(
            "llm_probe",
            self.tool_mode.resolve_job,
            LLM_PROBE_RETRY,
            timeout=timedelta(seconds=s.llm.timeout.total_seconds() * 2 + 30),
        )
        self.actions = ActionService(
            self.db, s, notifiers=self.action_notifiers, transport=self._arr_transport
        )
        self.scheduler.add("approvals:sweep", self.actions.sweep_expired, APPROVAL_SWEEP)
        register_automations(
            self.scheduler,
            AutomationEnv(self.actions, s, collector_results=lambda: self.collectors.latest),
        )
        await self.collectors.setup()
        await self.scheduler.start()

    async def _stop_services(self) -> None:
        await self.scheduler.stop()
        await self.collectors.close()
        await self.actions.close()
        await self.llm.aclose()
