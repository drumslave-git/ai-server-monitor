"""The benchmark service: starts a run in the background, one at a time, and reports progress.

A run takes the LLM configured in Settings → LLM at the moment it starts, runs every selected
scenario N times through `AttemptRunner`, stores every attempt as it finishes, and publishes
`bench.progress` on the event bus (the Benchmarks page follows it). By default a run pauses the
app's own automation (the global switch) for its duration and puts it back as it found it; with
the switch left alone the jobs keep running and an attempt records whether a report overlapped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import httpx

from .. import __version__
from ..chat.safety import Scrubber
from ..config import ChatSettings
from ..digest import collectors_snapshot
from ..llm import LLMClient, LLMError
from ..tracing import Tracing
from ..tracing.config import TraceConfig
from . import judge as judge_mod
from .hardware import HardwareSampler, in_windows, stats, summarize_hardware
from .probe import ollama_loaded, probe_backend
from .runner import LEVELS, AttemptRunner, OverlapWatch, summarize, summarize_levels
from .schema import (
    GROUP_TITLES,
    GROUPS,
    SMOKE_GROUP,
    Scenario,
    load_scenarios,
    scenario_set_hash,
)
from .store import BenchStore
from .warmup import reload_suspects, warm_up

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger(__name__)

PRIVATE_HISTORY_COLLECTORS = ("system", "docker", "systemd")
PRIVATE_HISTORY_POINTS = 24
MIN_INTERVAL_S = 0.5


class BenchBusy(Exception):
    """A run is already in progress."""


class BenchNotReady(Exception):
    """The LLM is not configured, or the selection is empty."""


def loaded_model(probed: dict[str, Any], ps: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the backend has in memory for the model: Ollama's `/api/ps` after the warm-up (size,
    the part in VRAM, context) or, for llama.cpp, what its probe said. None = nothing known."""
    if ps and ps.get("size"):
        size, vram = ps["size"], ps.get("size_vram")
        return {
            "source": "ollama /api/ps",
            "size": size,
            "size_vram": vram,
            "gpu_percent": round(vram / size * 100) if vram is not None else None,
            "context_length": ps.get("context_length"),
        }
    if probed.get("kind") == "llama.cpp" and probed.get("size_bytes"):
        return {
            "source": "llama.cpp /props",
            "size": probed["size_bytes"],
            "size_vram": None,
            "gpu_percent": None,
            "context_length": probed.get("context_length"),
        }
    return None


Plan = list[tuple[str | None, Scenario]]
"""What a run does, in order: (thinking level or None, scenario), each repeated N times."""


def plan_attempts(scenarios: list[Scenario], efforts: list[str] | None) -> Plan:
    """Every scenario at every thinking level, one level after another (the model stays loaded
    throughout, and the attempts of a level are contiguous). The Prometheus planner always asks
    the model for no thinking, so a level changes nothing there: those scenarios run once, in the
    first pass, without a level. `efforts` None = no levels, the LLM setting is used as it is."""
    if not efforts:
        return [(None, s) for s in scenarios]
    plan: Plan = []
    for i, level in enumerate(efforts):
        for s in scenarios:
            if s.input.kind != "prom_plan":
                plan.append((level, s))
            elif i == 0:
                plan.append((None, s))
    return plan


def clean_efforts(efforts: list[str] | None) -> list[str] | None:
    if not efforts:
        return None
    unknown = set(efforts) - set(LEVELS)
    if unknown:
        raise BenchNotReady(f"unknown thinking level: {', '.join(sorted(unknown))}")
    return [e for e in LEVELS if e in efforts]


def thinking_known_absent(probed: dict[str, Any]) -> bool:
    """The backend listed the model's capabilities and "thinking" is not among them."""
    caps = probed.get("capabilities")
    return isinstance(caps, list) and bool(caps) and "thinking" not in caps


class BenchService:
    warmup_enabled = True
    """The cold-start request before the first attempt (tests switch it off)."""

    def __init__(self, ctx: AppContext, *, llm_transport: httpx.AsyncBaseTransport | None = None):
        self.ctx = ctx
        self.store = BenchStore(ctx.db)
        self._llm_transport = llm_transport
        self._judge_transport: httpx.AsyncBaseTransport | None = None
        self._probe_transport: httpx.AsyncBaseTransport | None = None
        self._ps_transport: httpx.AsyncBaseTransport | None = None
        self._watch_ps = False
        self._task: asyncio.Task[None] | None = None
        self.progress: dict[str, Any] | None = None
        self._cancelled = False
        self._restore_to: str | None = None
        """Set while this run holds the automation switch it turned on."""

    # ------------------------------------------------------------------ catalog

    def catalog(self, *, include_smoke: bool = False) -> dict[str, Any]:
        scenarios, problems = load_scenarios(include_smoke=include_smoke)
        return {
            "groups": [{"key": g, "title": GROUP_TITLES[g]} for g in GROUPS],
            "scenarios": [s.public() for s in scenarios],
            "hash": scenario_set_hash([s for s in scenarios if not s.smoke]),
            "problems": problems,
            "repeats": self.ctx.settings.bench.repeats,
        }

    def select(self, *, group: str | None = None, ids: list[str] | None = None) -> list[Scenario]:
        if group == SMOKE_GROUP:
            return [s for s in load_scenarios(include_smoke=True)[0] if s.smoke]
        scenarios, _ = load_scenarios(include_smoke=bool(ids))
        if ids:
            wanted = set(ids)
            chosen = [s for s in scenarios if s.id in wanted]
            missing = wanted - {s.id for s in chosen}
            if missing:
                raise BenchNotReady(f"unknown scenario: {', '.join(sorted(missing))}")
            return chosen
        chosen = [s for s in scenarios if not s.smoke]
        if group:
            chosen = [s for s in chosen if s.group == group]
        return chosen

    # ------------------------------------------------------------------ running

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(
        self,
        *,
        group: str | None = None,
        ids: list[str] | None = None,
        repeats: int | None = None,
        efforts: list[str] | None = None,
        pause_automation: bool = True,
    ) -> int:
        """Start a run in the background → its id. Raises BenchBusy / BenchNotReady.
        `pause_automation`: turn the global automation switch on for the run, and back to what
        it was when the run ends (also on failure or cancel)."""
        if self.running:
            raise BenchBusy("a benchmark run is already in progress")
        settings = self.ctx.settings
        if not settings.llm.configured:
            raise BenchNotReady("The LLM is not configured: set it in Settings → LLM first.")
        scenarios = self.select(group=group, ids=ids)
        if not scenarios:
            raise BenchNotReady("no scenarios to run")
        n = repeats or settings.bench.repeats
        levels = clean_efforts(efforts)
        scope = "ids" if ids else (f"group:{group}" if group else "all")
        llm_cfg = settings.llm.model_copy(deep=True)
        chat_cfg = settings.chat.model_copy(deep=True)
        run_id = await self.store.create_run(
            scope=scope,
            repeats=n,
            scenario_hash=scenario_set_hash(scenarios),
            app_version=__version__,
            model=llm_cfg.model,
            base_url=llm_cfg.base_url,
        )
        self._cancelled = False
        was_paused = now_paused = self.ctx.automation.paused
        self._restore_to = None
        if pause_automation and not was_paused:
            by = f"bench #{run_id}"
            try:
                await self.ctx.pause_automation(by)
            except BaseException:
                await self.store.update_run(
                    run_id, status="failed", error="could not pause automation"
                )
                raise
            self._restore_to = by
            now_paused = True
        automation = {
            "paused": now_paused,
            "by_run": self._restore_to is not None,
            "was_paused": was_paused,
        }
        await self.store.update_run(run_id, meta={"automation": automation})
        total = len(plan_attempts(scenarios, levels)) * n
        self.progress = {
            "run_id": run_id,
            "status": "running",
            "stage": "preparing",
            "total": total,
            "done": 0,
            "scenario": None,
            "attempt": None,
            "scenarios": len(scenarios),
            "efforts": levels,
            "effort": None,
            "started": time.time(),
        }
        self._publish()
        self._task = asyncio.create_task(
            self._run(
                run_id,
                scenarios,
                n,
                llm_cfg,
                settings.bench.attempt_timeout.total_seconds(),
                chat_cfg,
                levels,
                automation,
            ),
            name=f"bench-{run_id}",
        )
        self._task.add_done_callback(self._on_task_done)
        return run_id

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        # A task cancelled before its first step never reaches its own `finally`.
        if self._restore_to is not None:
            asyncio.get_running_loop().create_task(self._restore_automation())

    async def _restore_automation(self) -> None:
        """Put the automation switch back as the run found it (idempotent). Only if it is still
        the state this run set: someone who resumed or re-paused meanwhile has the say."""
        by, self._restore_to = self._restore_to, None
        if by is None:
            return
        ctx = self.ctx
        if ctx.automation.paused and ctx.automation.by == by:
            try:
                await ctx.resume_automation(by)
            except Exception:
                log.exception("could not resume automation after benchmark run")

    async def cancel(self) -> bool:
        if not self.running or self._task is None:
            return False
        self._cancelled = True
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        return True

    async def shutdown(self) -> None:
        await self.cancel()

    def _publish(self, event: str = "bench.progress") -> None:
        if self.progress is not None:
            self.ctx.events.publish(event, dict(self.progress))

    # ------------------------------------------------------------------ the run

    @contextmanager
    def _overlap(self) -> Iterator[OverlapWatch]:
        reports = self.ctx.reports
        job = self.ctx.scheduler.jobs.get("report")
        before = reports.last_outcome
        busy = bool(job and job.running)

        def hit() -> bool:
            now_busy = bool(job and job.running)
            return busy or now_busy or reports.last_outcome is not before

        yield OverlapWatch(hit)

    async def _run(
        self,
        run_id: int,
        scenarios: list[Scenario],
        repeats: int,
        llm_cfg: Any,
        budget_s: float,
        chat_cfg: ChatSettings | None = None,
        efforts: list[str] | None = None,
        automation: dict[str, Any] | None = None,
    ) -> None:
        ctx = self.ctx
        attempts: list[dict[str, Any]] = []
        tracing = Tracing(
            ctx.db,
            TraceConfig(
                enabled=True,
                store_bodies=ctx.settings.logs.store_bodies,
                retention_days=ctx.settings.logs.retention.total_seconds() / 86400,
                max_total_mb=ctx.settings.logs.max_total_mb,
            ),
            scrubber=lambda: Scrubber(ctx.settings),
            events=ctx.events,
        )
        llm = LLMClient(llm_cfg, transport=self._llm_transport, tracing=tracing)
        status, error = "done", None
        sampler = self._start_sampler(run_id)
        windows: dict[str, list[tuple[float, float]]] = {}
        try:
            runner, probed = await self._prepare(
                run_id, llm, llm_cfg, tracing, scenarios, sampler, chat_cfg, efforts, automation
            )
            if efforts and len(efforts) > 1 and thinking_known_absent(probed):
                # A model that does not think: every level is the same run.
                efforts = [efforts[0]]
                await self._levels_meta(run_id, efforts)
            plan = plan_attempts(scenarios, efforts)
            assert self.progress is not None
            self.progress["total"] = len(plan) * repeats
            self._publish()
            for effort, scenario in plan:
                for attempt in range(1, repeats + 1):
                    self._progress(scenario, attempt, effort)
                    gone = await self._model_gone(llm_cfg)
                    row = await runner.run(
                        scenario, attempt, float(scenario.timeout_s or budget_s), effort
                    )
                    outcome = row.pop("outcome")
                    if sampler is not None:
                        span = (float(row["started"]), time.time())
                        windows.setdefault(scenario.id, []).append(span)
                        row["hw"] = stats(in_windows(sampler.samples, [span]))
                    attempt_id = await self.store.add_attempt(run_id, row)
                    row["id"] = attempt_id
                    if gone:
                        await self.store.flag_reload(attempt_id, gone)
                        row["reload_suspected"] = gone
                    await runner.relink_traces(
                        run_id, scenario.id, attempt, list(row.get("trace_ids") or [])
                    )
                    row["scenario_id"] = scenario.id
                    attempts.append(row)
                    await self._judge(attempt_id, scenario, outcome)
                    assert self.progress is not None
                    self.progress["done"] += 1
                    self.progress["last_score"] = row["score"]
                    self._publish()
        except asyncio.CancelledError:
            status = "cancelled"
        except LLMError as exc:
            status, error = "failed", f"The language model is not reachable: {exc}"
        except Exception as exc:
            log.exception("benchmark run %d failed", run_id)
            status, error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if sampler is not None:
                await sampler.stop()
            try:
                await self._finish(run_id, status, error, attempts, sampler, windows, llm_cfg)
                await llm.aclose()
                await tracing.flush()
            finally:
                await self._restore_automation()

    def _start_sampler(self, run_id: int) -> HardwareSampler | None:
        bench = self.ctx.settings.bench
        if not bench.record_hardware:
            return None
        interval = min(max(bench.hardware_interval.total_seconds(), MIN_INTERVAL_S), 10.0)
        sampler = HardwareSampler(
            self.ctx.db, run_id, self.ctx.collectors.collectors.get("gpu"), interval
        )
        sampler.start()
        return sampler

    async def _model_gone(self, llm_cfg: Any) -> str | None:
        """Before an attempt: did the backend unload the model since the last one? (Ollama only)"""
        if not self._watch_ps:
            return None
        try:
            state = await ollama_loaded(llm_cfg, transport=self._ps_transport)
        except Exception:
            return None
        if state is not None and not state["loaded"]:
            return "the model was not loaded when this attempt started (Ollama /api/ps)"
        return None

    async def _prepare(
        self,
        run_id: int,
        llm: LLMClient,
        llm_cfg: Any,
        tracing: Tracing,
        scenarios: list[Scenario],
        sampler: HardwareSampler | None = None,
        chat_cfg: ChatSettings | None = None,
        efforts: list[str] | None = None,
        automation: dict[str, Any] | None = None,
    ) -> tuple[AttemptRunner, dict[str, Any]]:
        ctx = self.ctx
        # The chat settings the run was started with, like the LLM settings.
        chat_cfg = chat_cfg or ctx.settings.chat.model_copy(deep=True)
        probed = await probe_backend(llm_cfg, transport=self._probe_transport)
        # The tool mode the product itself uses for this model (probed once, then cached).
        mode = await ctx.tool_mode.resolve() if llm_cfg.tool_mode == "auto" else llm_cfg.tool_mode
        gpu = None
        result = ctx.collectors.latest.get("gpu")
        if result is not None and result.ok:
            gpu = {
                "driver": result.data.get("driver"),
                "gpus": [
                    {
                        k: g.get(k)
                        for k in (
                            "name",
                            "mem_total",
                            "mem_used",
                            "util_percent",
                            "temperature_c",
                            "power_w",
                        )
                    }
                    for g in result.data.get("gpus", [])
                ],
            }
        judge_cfg = ctx.settings.bench.judge
        meta = {
            "llm": {
                "temperature": llm_cfg.temperature,
                "max_output_tokens": llm_cfg.max_output_tokens,
                "reasoning_effort": llm_cfg.reasoning_effort,
                "max_context_tokens": llm_cfg.max_context_tokens,
                "tool_mode": llm_cfg.tool_mode,
                "timeout_s": llm_cfg.timeout.total_seconds(),
                "max_retries": llm_cfg.max_retries,
                "extra_body": llm_cfg.extra_body,
            },
            "tool_mode": mode,
            "chat": {
                "tool_routing": chat_cfg.tool_routing,
                "tool_routing_tokens": chat_cfg.tool_routing_tokens,
                "max_steps": chat_cfg.max_steps,
                "history_messages": chat_cfg.history_messages,
                "max_tool_result_chars": chat_cfg.max_tool_result_chars,
                "tool_timeout_s": chat_cfg.tool_timeout.total_seconds(),
            },
            "levels": (
                {
                    "tested": efforts,
                    "applies_to": "the chat answers and the report",
                    "fixed": (
                        "Tool routing, chat titles, the warm-up and the Prometheus planner "
                        "always ask for no thinking, so those scenarios run once, without a level."
                    ),
                }
                if efforts
                else None
            ),
            "backend": probed,
            "gpu": gpu,
            "hardware": {"recorded": sampler is not None},
            "automation": automation,
            "judge": (
                {"model": judge_cfg.model, "base_url": judge_cfg.base_url}
                if judge_cfg.configured
                else None
            ),
        }
        await self.store.update_run(
            run_id,
            backend_kind=probed.get("kind"),
            backend_version=probed.get("version"),
            params=probed.get("params"),
            quantization=probed.get("quantization"),
            context_length=probed.get("context_length"),
            meta=meta,
        )
        runner = AttemptRunner(
            llm,
            tracing,
            auto_tool_mode=mode,
            private_snapshot=self.store.snapshot_doc,
            overlap=self._overlap,
            chat=chat_cfg,
        )
        if self.warmup_enabled:
            assert self.progress is not None
            self.progress.update(stage="warming up")
            self._publish()
            cold, scope = await warm_up(llm, llm_cfg, ps_transport=self._ps_transport)
            await tracing.flush()
            await runner.relink_traces(run_id, "warmup", 0, sorted(set(scope.call_ids)))
            self._watch_ps = bool(cold.get("supported"))
            meta["cold_start"] = cold
        meta["loaded_model"] = loaded_model(probed, (meta.get("cold_start") or {}).get("model"))
        await self.store.update_run(run_id, meta=meta)
        return runner, probed

    async def _levels_meta(self, run_id: int, efforts: list[str]) -> None:
        """The model does not think: record that the run was cut to one level."""
        run = await self.store.get_run(run_id)
        meta = (run or {}).get("meta") or {}
        if meta.get("levels"):
            meta["levels"]["tested"] = efforts
            meta["levels"]["collapsed"] = "the model does not report thinking: one level is enough"
            await self.store.update_run(run_id, meta=meta)

    def _progress(self, scenario: Scenario, attempt: int, effort: str | None = None) -> None:
        assert self.progress is not None
        self.progress.update(
            stage="running",
            scenario=scenario.id,
            title=scenario.title,
            attempt=attempt,
            effort=effort,
        )
        self._publish()

    async def _judge(self, attempt_id: int, scenario: Scenario, outcome: Any) -> None:
        cfg = self.ctx.settings.bench.judge
        if scenario.judge is None or not cfg.configured or outcome.error:
            return
        inp = scenario.input
        task = (
            inp.messages[-1].content
            if inp.kind == "chat"
            else "Write the server status report for the server data you were given."
            if inp.kind == "report"
            else "Choose Prometheus queries for the metrics catalog you were given."
        )
        score, reason = await judge_mod.grade(
            cfg,
            task=f"{scenario.description}\n\n{task}",
            rubric=scenario.judge.rubric,
            answer=outcome.answer,
            tools=[c.name for c in outcome.tool_calls],
            transport=self._judge_transport,
        )
        await self.store.set_judge(attempt_id, score, reason)

    async def _finish(
        self,
        run_id: int,
        status: str,
        error: str | None,
        attempts: list[dict[str, Any]],
        sampler: HardwareSampler | None = None,
        windows: dict[str, list[tuple[float, float]]] | None = None,
        llm_cfg: Any = None,
    ) -> None:
        try:
            for attempt_id, why in reload_suspects(attempts).items():
                await self.store.flag_reload(attempt_id, why)
                for a in attempts:
                    if a.get("id") == attempt_id and not a.get("reload_suspected"):
                        a["reload_suspected"] = why
            summary = summarize(attempts) if attempts else {}
            body = summary.get("summary")
            levels = summarize_levels(attempts) if attempts else {}
            if levels and body is not None:
                body["levels"] = levels
            extra: dict[str, Any] = {}
            if sampler is not None:
                hw = summarize_hardware(sampler.samples, windows or {})
                body = body or {}
                body["hardware"] = hw["run"]
                for sid, stat in hw["scenarios"].items():
                    if sid in body.get("scenarios", {}):
                        body["scenarios"][sid]["hw"] = stat
                run = await self.store.get_run(run_id)
                meta = (run or {}).get("meta") or {}
                meta["hardware"] = sampler.describe(getattr(llm_cfg, "base_url", None))
                extra["meta"] = meta
            await self.store.update_run(
                run_id,
                status=status,
                error=error,
                finished=time.time(),
                overall=summary.get("overall"),
                group_scores=summary.get("group_scores"),
                summary=body,
                **extra,
            )
        except Exception:
            log.exception("could not finish benchmark run %d", run_id)
        if self.progress is not None:
            self.progress.update(status=status, stage="finished", error=error)
            self._publish("bench.done")
        self.progress = None

    # ------------------------------------------------------------------ private snapshots

    async def capture_snapshot(self, name: str, description: str = "") -> dict[str, Any]:
        """Freeze the current collector data (and a little history) of this server as a snapshot
        named `name` in the database. Never written to disk or the repo."""
        ctx = self.ctx
        now = time.time()
        raw = collectors_snapshot(ctx.collectors)
        collectors: dict[str, Any] = {}
        for cname, status in raw.items():
            result = status.get("result")
            if result is None:
                continue
            collectors[cname] = {
                "ok": bool(result["ok"]),
                "state": "ok" if result["ok"] else "error",
                "reason": status.get("reason"),
                "error": result.get("error"),
                "age_s": round(max(now - float(result["ts"]), 0), 1),
                "data": result.get("data") or {},
            }
        if not collectors:
            raise BenchNotReady("no collector data yet: wait for the first collection")
        history: dict[str, list[dict[str, Any]]] = {}
        window = ctx.settings.report.trend_window.total_seconds()
        for cname in PRIVATE_HISTORY_COLLECTORS:
            rows = await ctx.db.fetchall(
                "SELECT ts, data FROM collector_samples WHERE collector = ? AND ok = 1 "
                "AND ts > ? ORDER BY ts",
                (cname, now - window),
            )
            if not rows:
                continue
            step = max(len(rows) // PRIVATE_HISTORY_POINTS, 1)
            history[cname] = [
                {"hours_ago": round((now - r["ts"]) / 3600, 3), "data": json.loads(r["data"])}
                for r in rows[::step]
            ]
        doc = {
            "name": name,
            "description": description or f"captured from this server at {time.ctime(now)}",
            "now": now,
            "collectors": collectors,
            "history": history,
        }
        await self.store.save_snapshot(name, doc, doc["description"])
        return {"name": name, "collectors": sorted(collectors), "history": sorted(history)}
