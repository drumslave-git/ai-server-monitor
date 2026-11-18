"""Running one job: the shared agent loop (`agent/runtime.py`) with a structured final result.

Topic jobs get only `bash`; the summary job also `delegate` and the latest topic results. Every
run is a `job_runs` row from the moment it is requested to its end; a run that fails, times
out or is cancelled is stored as `error` with the reason, never dropped. One run per job at a
time; Settings → Jobs → concurrent runs limits how many use the models at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ..agent.prompts import host_line
from ..agent.runtime import AgentConfig, AgentTool, run_agent
from ..agent.safety import Scrubber
from ..agent.tools import app_bash, app_delegate, app_shell
from ..config import Settings
from ..llm.text import strip_reasoning
from .prompts import summary_input, summary_instructions, topic_instructions
from .store import JobStore

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger(__name__)

Trigger = Literal["schedule", "manual", "llm"]


class Finding(BaseModel):
    severity: Literal["info", "warn", "crit"]
    title: str = Field(description="Short title of the problem.")
    detail: str = Field(default="", description="Numbers, names, paths.")


class JobResult(BaseModel):
    """The result of the check."""

    status: Literal["ok", "warn", "crit"]
    summary: str = Field(description="1 to 3 short sentences with the key numbers (markdown).")
    findings: list[Finding] = Field(default_factory=list, description="Only what is not ok.")


class JobRunner:
    def __init__(self, app: AppContext):
        self.app = app
        self.store = JobStore(app.db)
        self._locks: dict[str, asyncio.Lock] = {}
        self._limit = app.settings.jobs.max_concurrent_runs
        self._slots = asyncio.Semaphore(self._limit)
        self._running: set[asyncio.Task[Any]] = set()
        self._background: set[asyncio.Task[Any]] = set()

    def apply_settings(self, settings: Settings) -> None:
        """New settings: a new limit of concurrent runs (runs holding the old one finish)."""
        if settings.jobs.max_concurrent_runs != self._limit:
            self._limit = settings.jobs.max_concurrent_runs
            self._slots = asyncio.Semaphore(self._limit)

    # ------------------------------------------------------------------ entry points

    async def run(self, slug: str, trigger: Trigger) -> dict[str, Any]:
        """Run `slug` now and wait for it; returns the finished run."""
        job = await self.store.require(slug)
        run_id = await self._open(job, trigger)
        task = asyncio.create_task(self._execute(job, run_id, trigger), name=f"job:{slug}")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel("the requester went away")
            raise
        return await self.store.get_run(run_id) or {"id": run_id}

    async def start(self, slug: str, trigger: Trigger) -> int:
        """Run `slug` in the background; returns the run id at once."""
        job = await self.store.require(slug)
        run_id = await self._open(job, trigger)
        task = asyncio.create_task(self._execute(job, run_id, trigger), name=f"job:{slug}")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return run_id

    async def cancel_all(self, reason: str) -> list[str]:
        """Cancel the scheduled runs in flight (the pause switch); they are stored as error.
        Runs a person or the chat started keep going."""
        tasks = [t for t in self._running if not t.done()]
        names = [t.get_name() for t in tasks]
        for task in tasks:
            task.cancel(reason)
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
        return names

    async def close(self) -> None:
        await self.cancel_all("stopping")
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)

    # ------------------------------------------------------------------ one run

    async def _open(self, job: dict[str, Any], trigger: Trigger) -> int:
        run_id = await self.store.start_run(
            job["slug"], trigger, self.app.models.name(job["model"])
        )
        self.app.events.publish(
            "job.started", {"run_id": run_id, "slug": job["slug"], "trigger": trigger}
        )
        return run_id

    async def _execute(self, job: dict[str, Any], run_id: int, trigger: Trigger) -> None:
        slug = job["slug"]
        settings = self.app.settings
        timeout = float(job.get("timeout_s") or settings.jobs.timeout.total_seconds())
        turn = uuid.uuid4().hex[:12]
        fields: dict[str, Any] = {"trace_turn": turn}
        task = asyncio.current_task()
        lock = self._locks.setdefault(slug, asyncio.Lock())
        try:
            if task is not None and trigger == "schedule":
                self._running.add(task)
            async with lock, self._slots:
                started = time.monotonic()
                try:
                    async with asyncio.timeout(timeout):
                        fields.update(await self._agent(job, turn))
                except TimeoutError:
                    fields.update(
                        status="error",
                        error=f"timed out after {time.monotonic() - started:.0f}s "
                        f"(limit {timeout:.0f}s)",
                    )
        except asyncio.CancelledError as exc:
            reason = exc.args[0] if exc.args else "cancelled"
            fields.update(status="error", error=f"cancelled ({reason})")
            await asyncio.shield(self._close(slug, run_id, fields))
            raise
        except Exception as exc:
            log.warning("job %s failed: %s", slug, exc, exc_info=True)
            fields.update(status="error", error=f"{type(exc).__name__}: {exc}"[:2000])
        finally:
            if task is not None:
                self._running.discard(task)
        await self._close(slug, run_id, fields)

    async def _close(self, slug: str, run_id: int, fields: dict[str, Any]) -> None:
        try:
            await self.store.finish_run(run_id, **fields)
        except Exception:
            log.exception("could not store the result of job run %s", run_id)
        self.app.events.publish(
            "job.finished",
            {
                "run_id": run_id,
                "slug": slug,
                "status": fields.get("status"),
                "error": fields.get("error"),
            },
        )

    async def _agent(self, job: dict[str, Any], turn: str) -> dict[str, Any]:
        app = self.app
        settings = app.settings
        slug = job["slug"]
        if not app.models.configured(job["model"]):
            return {"status": "error", "error": "the model is not configured (Settings)"}
        host = host_line(app.host_env.root)
        language = settings.general.language
        scrub = Scrubber(settings)
        shell = app_shell(app)
        session = f"job:{slug}"
        tools: list[AgentTool] = [app_bash(app, session, shell)]
        if job["kind"] == "summary":
            tools.append(app_delegate(app, session, scrub, host, shell))
            instructions = summary_instructions(host, language, job["prompt"])
            prompt = await self._summary_input(slug)
        else:
            instructions = topic_instructions(host, language, job["prompt"])
            prompt = "Run the check now, then call final_result."
        cfg = AgentConfig(
            model=app.models.model(job["model"]),
            instructions=instructions,
            tools=tools,
            max_steps=job.get("max_steps") or settings.jobs.max_steps,
            tool_timeout_s=settings.chat.tool_timeout.total_seconds(),
            result_chars=settings.chat.max_tool_result_chars,
            scrub=scrub,
            tracing=app.tracing,
            trace={"purpose": "job", "link_kind": "job", "link_id": slug, "turn": turn},
            output_type=JobResult,
        )
        end: dict[str, Any] = {}
        async for event in run_agent(cfg, prompt):
            if event["type"] == "end":
                end = event
        usage = end.get("usage", {})
        fields: dict[str, Any] = {
            "steps": end.get("steps"),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        }
        output = end.get("output")
        if not isinstance(output, JobResult):
            text = strip_reasoning(str(output or ""))[:500]
            return {**fields, "status": "error", "error": f"no result: {text or 'empty answer'}"}
        return {
            **fields,
            "status": output.status,
            "summary": output.summary,
            "findings": [f.model_dump() for f in output.findings],
        }

    async def _summary_input(self, slug: str) -> str:
        jobs = await self.store.list()
        topics = [j for j in jobs if j["kind"] == "topic" and j["enabled"]]
        latest = await self.store.latest_runs()
        return summary_input(topics, latest, latest.get(slug))
