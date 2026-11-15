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
from typing import Any

import httpx

from .actions import ActionService, Notifier
from .arr.automations import AutomationEnv
from .arr.automations import register as register_automations
from .chat import ChatEngine
from .chat.audit import audit_hook
from .collectors import Collector, CollectorManager, HostEnv
from .collectors.base import CollectorResult
from .config import Bootstrap, Settings
from .db import Database
from .digest import WindowObserver
from .events import EventBus
from .llm import LLMClient, ToolModeResolver
from .mcp_client import McpManager
from .outputs.action_events import ActionEventBridge
from .outputs.arr_metrics import arr_families
from .outputs.live import LiveFeed
from .outputs.metrics import Family, MetricsHub
from .outputs.telegram import TelegramBot
from .promauto import Planner, QueryStore
from .report.engine import ReportEngine
from .scheduler import Scheduler
from .settings_store import SettingsStore

log = logging.getLogger(__name__)

LLM_PROBE_RETRY = timedelta(minutes=5)
REPORT_INITIAL_DELAY = timedelta(seconds=45)
"""Let the collectors produce their first results before the first report."""
OBSERVE_INTERVAL = timedelta(seconds=30)
APPROVAL_SWEEP = timedelta(minutes=1)
PROM_PLAN_CHECK = timedelta(hours=1)
PROM_PLAN_INITIAL_DELAY = timedelta(minutes=2)


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
        telegram_transport: httpx.AsyncBaseTransport | None = None,
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
        self._telegram_transport = telegram_transport
        self.telegram: TelegramBot | None = None
        self._lock = asyncio.Lock()
        # Survive settings changes (subscribers and journal history stay valid):
        self.events = EventBus()
        self.observer = WindowObserver()
        self.live = LiveFeed(self.events)
        self.metrics = MetricsHub()
        self.metrics.add_provider(self._arr_metrics)
        self._metrics_task: asyncio.Task[None] | None = None
        self.action_notifiers: list[Notifier] = [ActionEventBridge(self.events, self.metrics)]
        """Callables `(event, record)` told about suggested/pending/finished actions.
        Survives settings changes; the event bus subscribes here."""
        # Built in start() / apply_settings():
        self.settings = Settings()
        self.scheduler = Scheduler()
        self.llm = LLMClient(self.settings.llm, transport=llm_transport)
        self.tool_mode = ToolModeResolver(self.llm, self.db, self.settings.llm.tool_mode)
        self.collectors = CollectorManager(
            self.settings,
            self.db,
            self.scheduler,
            registry=collector_registry,
            env=self.host_env,
            options=self._collector_options(),
        )
        self.reports = self._build_reports()
        self.promauto = Planner(self.settings, self.db, self.llm)
        self.mcp = McpManager(self.settings.mcp, audit=audit_hook(self.db, "mcp"))
        self.actions = ActionService(
            self.db,
            self.settings,
            notifiers=self.action_notifiers,
            transport=arr_transport,
            mcp=self.mcp,
        )
        self.chat = ChatEngine(self)

    def _collector_options(self) -> dict[str, dict[str, Any]]:
        options: dict[str, dict[str, Any]] = {"prometheus": {"store": QueryStore(self.db)}}
        if self._arr_transport:
            options["arr"] = {"transport": self._arr_transport}
        return options

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        await self.db.connect()
        self._start_metrics_consumer()
        if self._seed is not None:
            await self.store.save(self._seed)
        self.settings = await self.store.load()
        await self._start_services()
        log.info("started (data=%s, host root=%s)", self.bootstrap.data_dir, self.host_env.root)

    async def stop(self) -> None:
        await self._stop_services()
        self.events.close()
        if self._metrics_task is not None:
            self._metrics_task.cancel()
            await asyncio.gather(self._metrics_task, return_exceptions=True)
            self._metrics_task = None
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
            s,
            self.db,
            self.scheduler,
            registry=self._registry,
            env=self.host_env,
            options=self._collector_options(),
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
        self.mcp = McpManager(s.mcp, audit=audit_hook(self.db, "mcp"))
        self.actions = ActionService(
            self.db, s, notifiers=self.action_notifiers, transport=self._arr_transport, mcp=self.mcp
        )
        self.scheduler.add("approvals:sweep", self.actions.sweep_expired, APPROVAL_SWEEP)
        register_automations(
            self.scheduler,
            AutomationEnv(self.actions, s, collector_results=lambda: self.collectors.latest),
        )
        await self.collectors.setup()
        self.metrics.reset_collectors()
        self.llm.stats.on_success = self.metrics.on_llm_success
        self.llm.stats.on_error = self.metrics.on_llm_error
        self.collectors.on_result = self._on_collector_result
        self.reports = self._build_reports()
        self.promauto = Planner(s, self.db, self.llm)
        self.scheduler.add(
            "prom_plan",
            self.promauto.run_job,
            PROM_PLAN_CHECK,
            initial_delay=PROM_PLAN_INITIAL_DELAY,
            timeout=timedelta(hours=3),
        )
        self.scheduler.add("digest_observe", self.reports.observe, OBSERVE_INTERVAL)
        self.scheduler.add(
            "report",
            self.reports.run_job,
            s.report.interval,
            initial_delay=REPORT_INITIAL_DELAY,
        )
        await self.scheduler.start()
        tg = s.outputs.telegram
        if tg.enabled and tg.token.get_secret_value():
            self.telegram = TelegramBot(self, tg, transport=self._telegram_transport)
            await self.telegram.start()

    async def pending_approvals_count(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM actions WHERE status = 'pending'")
        return int(row["n"]) if row else 0

    async def _arr_metrics(self) -> list[Family]:
        result = self.collectors.latest.get("arr")
        data = result.data if result is not None and result.ok else None
        return arr_families(self.settings, data, await self.pending_approvals_count())

    def _on_collector_result(self, result: CollectorResult) -> None:
        self.metrics.on_collector_result(result)
        self.live.on_result(result)

    def _start_metrics_consumer(self) -> None:
        """Count report outcomes from the event bus (subscribed before anything can publish)."""
        sub = self.events.subscribe("report.done", "report.failed")

        async def consume() -> None:
            async for event in sub:
                self.metrics.on_report_event(event.type, event.data)

        self._metrics_task = asyncio.create_task(consume(), name="metrics-consumer")

    def _build_reports(self) -> ReportEngine:
        return ReportEngine(
            self.settings, self.db, self.llm, self.collectors, self.events, self.observer
        )

    async def _stop_services(self) -> None:
        if self.telegram is not None:
            await self.telegram.stop()
            self.telegram = None
        await self.scheduler.stop()
        await self.collectors.close()
        await self.actions.close()
        await self.mcp.close()
        await self.llm.aclose()

    # ------------------------------------------------------------------ reports

    def replan_prometheus_now(self) -> bool:
        """Have the planner choose the Prometheus queries again. False if it is running."""
        job = self.scheduler.jobs["prom_plan"]
        if job.running or self.promauto.running:
            return False
        self.promauto.force_next = True
        self.scheduler.trigger("prom_plan")
        return True

    def run_report_now(self) -> bool:
        """Trigger the report job. False if a report is already being written."""
        job = self.scheduler.jobs["report"]
        if job.running:
            return False
        self.reports.force_next = True
        self.scheduler.trigger("report")
        return True
