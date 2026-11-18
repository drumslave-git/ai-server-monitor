"""Application context: owns long-lived services and their lifecycle.

Settings come from the database and can change at runtime (web UI). `apply_settings`
saves them and rebuilds the settings-dependent services (scheduler, models, MCP,
approvals, Telegram) in place; the database connection stays open.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

import httpx

from .actions import ActionService, Notifier
from .agent.models import ModelHub
from .agent.safety import Scrubber
from .automation import KV_KEY as AUTOMATION_KEY
from .automation import AutomationState
from .chat import ChatEngine
from .chat.audit import audit_hook
from .config import Bootstrap, Settings
from .db import Database
from .events import EventBus
from .hostenv import HostEnv
from .jobs import JobRunner
from .mcp_client import McpManager
from .outputs.action_events import ActionEventBridge
from .outputs.metrics import Family, MetricsHub, Sample
from .outputs.telegram import TelegramBot
from .scheduler import Scheduler
from .settings_store import SettingsStore
from .tracing import Tracing

log = logging.getLogger(__name__)

APPROVAL_SWEEP = timedelta(minutes=1)


class AppContext:
    def __init__(
        self,
        bootstrap: Bootstrap,
        *,
        llm_transport: httpx.AsyncBaseTransport | None = None,
        host_env: HostEnv | None = None,
        seed_settings: Settings | None = None,
        telegram_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.bootstrap = bootstrap
        self.db = Database(bootstrap.db_path)
        self.store = SettingsStore(self.db)
        self.host_env = host_env or HostEnv.detect(bootstrap.host_root)
        self.started_at = time.time()
        self._llm_transport = llm_transport
        self._seed = seed_settings
        self._telegram_transport = telegram_transport
        self.telegram: TelegramBot | None = None
        self._lock = asyncio.Lock()
        # Survive settings changes (subscribers and journal history stay valid):
        self.events = EventBus()
        self.metrics = MetricsHub()
        self.metrics.add_provider(self._approval_metrics)
        self._metrics_task: asyncio.Task[None] | None = None
        self.action_notifiers: list[Notifier] = [ActionEventBridge(self.events, self.metrics)]
        """Callables `(event, record)` told about suggested/pending/finished actions.
        Survives settings changes; the event bus subscribes here."""
        # Built in start() / apply_settings():
        self.settings = Settings()
        self.tracing = Tracing(
            self.db, scrubber=lambda: Scrubber(self.settings), events=self.events
        )
        """The LLM trace and the app log; survives settings changes like the event bus."""
        self.automation = AutomationState()
        """The global pause switch (persisted); a new scheduler is built to match it."""
        self.scheduler = Scheduler()
        self.models = ModelHub(self.settings, tracing=self.tracing, transport=llm_transport)
        """The agents' models (main, worker), rebuilt with every settings change."""
        self.mcp = McpManager(self.settings.mcp, audit=audit_hook(self.db, "mcp"))
        self.actions = ActionService(
            self.db,
            self.settings,
            notifiers=self.action_notifiers,
            mcp=self.mcp,
        )
        self.chat = ChatEngine(self)
        self.jobs = JobRunner(self)
        """Runs scheduled LLM jobs; lives as long as the app (runs survive settings changes)."""

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.db.connect()
        self.tracing.applog.install()
        self.tracing.applog.start()
        self._start_metrics_consumer()
        if self._seed is not None:
            await self.store.save(self._seed)
        self.settings = await self.store.load()
        self.automation = AutomationState.from_stored(await self.db.kv_get(AUTOMATION_KEY))
        if self.automation.paused:
            log.warning("automation is paused: nothing runs by itself until it is resumed")
        await self.jobs.store.seed_builtins()
        if interrupted := await self.jobs.store.mark_interrupted():
            log.warning("%d job run(s) were interrupted by the last stop", interrupted)
        await self._start_services()
        log.info("started (data=%s, host root=%s)", self.bootstrap.data_dir, self.host_env.root)

    async def stop(self) -> None:
        await self._stop_services()
        await self.jobs.close()
        self.events.close()
        if self._metrics_task is not None:
            self._metrics_task.cancel()
            await asyncio.gather(self._metrics_task, return_exceptions=True)
            self._metrics_task = None
        await self.tracing.stop()
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
        if self.automation.paused:
            self.scheduler.hold(self.automation.since)
        self.tracing.apply_settings(s.logs)
        self.models = ModelHub(s, tracing=self.tracing, transport=self._llm_transport)
        self.jobs.apply_settings(s)
        self.scheduler.add(
            "prune",
            lambda: self.db.prune(s.storage.retention),
            s.storage.prune_interval,
            initial_delay=timedelta(seconds=30),
        )
        self.scheduler.add(
            "trace_prune",
            self.tracing.prune,
            s.storage.prune_interval,
            initial_delay=timedelta(seconds=60),
        )
        self.mcp = McpManager(s.mcp, audit=audit_hook(self.db, "mcp"))
        self.actions = ActionService(
            self.db,
            s,
            notifiers=self.action_notifiers,
            mcp=self.mcp,
        )
        self.scheduler.add("approvals:sweep", self.actions.sweep_expired, APPROVAL_SWEEP)
        self.scheduler.add("approvals:drain", self.actions.drain_queue, APPROVAL_SWEEP)
        await self.scheduler.start()
        tg = s.outputs.telegram
        if tg.enabled and tg.token.get_secret_value():
            self.telegram = TelegramBot(self, tg, transport=self._telegram_transport)
            await self.telegram.start()

    async def jobs_changed(self) -> None:
        """A job was created, changed or deleted: bring the schedule in line."""

    def job_next_run(self, slug: str) -> float | None:
        """When the scheduler runs `slug` next (None: not scheduled)."""
        return None

    async def pending_approvals_count(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM actions WHERE status = 'pending'")
        return int(row["n"]) if row else 0

    async def pending_approval_batches_count(self) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(DISTINCT batch_id) AS n FROM actions WHERE status = 'pending'"
        )
        return int(row["n"]) if row else 0

    async def _approval_metrics(self) -> list[Family]:
        """Evaluated at scrape time: one indexed COUNT each."""
        return [
            Family(
                "asm_pending_approvals",
                "gauge",
                "Actions (items) waiting for a human decision.",
                [Sample(await self.pending_approvals_count())],
            ),
            Family(
                "asm_pending_approval_batches",
                "gauge",
                "Approvals (batches of items) waiting for a human decision.",
                [Sample(await self.pending_approval_batches_count())],
            ),
        ]

    def _start_metrics_consumer(self) -> None:
        """Count LLM requests from the trace's `llm.call` events (subscribed before anything can
        publish)."""
        sub = self.events.subscribe("llm.call")

        async def consume() -> None:
            async for event in sub:
                self.metrics.on_llm_call(event.data)

        self._metrics_task = asyncio.create_task(consume(), name="metrics-consumer")

    async def _stop_services(self) -> None:
        if self.telegram is not None:
            await self.telegram.stop()
            self.telegram = None
        await self.scheduler.stop()
        await self.mcp.close()
        await self.models.aclose()

    # ------------------------------------------------------------------ automation switch

    async def automation_status(self) -> dict[str, Any]:
        """The switch for the UI and /api/info, with what is waiting because of it."""
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM actions WHERE status = 'queued'")
        return {
            **self.automation.to_dict(),
            "queued_actions": int(row["n"]) if row else 0,
            "jobs": len(self.scheduler.jobs),
            "jobs_cancelled": [
                j.name for j in self.scheduler.jobs.values() if j.last_status == "cancelled"
            ],
        }

    async def pause_automation(self, by: str = "ui") -> list[str]:
        """Turn the switch on: persist it, cancel what is running, hold every job. Idempotent.
        Returns the jobs that were cancelled mid-run."""
        async with self._lock:
            if self.automation.paused and self.scheduler.paused:
                return []
            self.automation = AutomationState(paused=True, since=time.time(), by=by)
            await self.db.kv_set(AUTOMATION_KEY, self.automation.to_dict())
            self.scheduler.paused_since = self.automation.since
            cancelled = await self.scheduler.pause()
            log.warning("automation paused by %s (cancelled: %s)", by, ", ".join(cancelled) or "-")
        self.events.publish("automation.changed", await self.automation_status())
        return cancelled

    async def resume_automation(self, by: str = "ui") -> None:
        """Turn the switch off: jobs come due on their normal schedule, those that were due
        meanwhile start a couple of seconds apart. Idempotent."""
        async with self._lock:
            if not self.automation.paused and not self.scheduler.paused:
                return
            self.automation = AutomationState()
            await self.db.kv_set(AUTOMATION_KEY, self.automation.to_dict())
            self.scheduler.resume()
            log.info("automation resumed by %s", by)
        self.events.publish("automation.changed", await self.automation_status())
