"""Report engine: digest -> one streamed LLM call -> parsed, stored report.

Runs as the scheduler job "report" (every `report.interval`) or on demand
(`POST /api/reports/run`). Failures never raise out of `run`: they become a stored
failed/skipped row, an event, and (when new) one log line.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..collectors import CollectorManager
from ..config import Settings
from ..db import Database
from ..digest import Digest, WindowObserver, build_digest, collectors_snapshot
from ..events import EventBus
from ..llm import LLMClient, LLMError
from ..llm.text import strip_reasoning
from ..tracing import trace_scope
from .parsing import ParsedReport, parse_report
from .prompts import previous_report_note, system_prompt, user_prompt
from .store import ReportStore

log = logging.getLogger(__name__)

TRUNCATED_NOTE = (
    "\n\n> ⚠ The report was cut off: the model reached its output limit. Raise "
    "Settings → LLM → max output tokens (models that “think” spend part of it on reasoning)."
)
NOT_CONFIGURED = "LLM is not configured: set the base URL and model in Settings → LLM."


@dataclass
class RunOutcome:
    status: str  # ok | failed | skipped
    report_id: int | None = None
    message: str | None = None
    stored: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "report_id": self.report_id,
            "message": self.message,
            "stored": self.stored,
        }


class ReportEngine:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: LLMClient,
        collectors: CollectorManager,
        events: EventBus,
        observer: WindowObserver,
        tracing: Any = None,
    ):
        self.settings = settings
        self.db = db
        self.llm = llm
        self.collectors = collectors
        self.events = events
        self.observer = observer
        self.tracing = tracing
        self.store = ReportStore(db)
        self.force_next = False
        """Set by a manual run: ignore skip_if_unchanged once."""
        self.last_outcome: RunOutcome | None = None
        self._last_problem: str | None = None

    # ------------------------------------------------------------------ digest

    async def digest(self) -> Digest:
        snapshot = collectors_snapshot(self.collectors)
        self.observer.observe(snapshot)
        return await build_digest(snapshot, self.db, self.settings.report, observer=self.observer)

    async def observe(self) -> None:
        """Cheap job: remember journal windows between reports."""
        self.observer.observe(collectors_snapshot(self.collectors))

    # ------------------------------------------------------------------ job

    def time_limit(self) -> float:
        """Overall cap for one report: every request attempt may take the full timeout."""
        cfg = self.settings.llm
        return cfg.timeout.total_seconds() * (cfg.max_retries + 1) + 60

    async def run_job(self) -> None:
        """Scheduler entry point."""
        await self.run()

    async def run(self) -> RunOutcome:
        force, self.force_next = self.force_next, False
        started_ts = time.time()
        limit = self.time_limit()
        with trace_scope("report") as scope:
            try:
                async with asyncio.timeout(limit):
                    outcome = await self._run(force, started_ts)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                outcome = await self._problem(
                    "failed",
                    f"Report generation timed out after {limit:.0f}s",
                    started_ts,
                    None,
                    None,
                )
            except Exception as exc:  # a bug must not kill the job loop or lose the reason
                log.exception("report run failed")
                outcome = await self._problem(
                    "failed", f"{type(exc).__name__}: {exc}", started_ts, None, None
                )
        if self.tracing is not None and outcome.report_id is not None:
            # The row only exists now; link the LLM calls made for it (Logs ↔ report page).
            try:
                await self.tracing.attach(scope, "report", outcome.report_id)
            except Exception:
                log.warning("could not link the report to its LLM calls", exc_info=True)
        self.last_outcome = outcome
        return outcome

    async def _run(self, force: bool, ts: float) -> RunOutcome:
        llm_cfg = self.settings.llm
        digest = await self.digest()
        if not llm_cfg.configured:
            return await self._problem("skipped", NOT_CONFIGURED, ts, digest, None)

        previous = await self.store.latest(status="ok")
        if (
            self.settings.report.skip_if_unchanged
            and not force
            and previous is not None
            and previous.get("digest_hash") == digest.hash
        ):
            log.debug("report skipped: digest unchanged")
            return RunOutcome("skipped", message="digest unchanged", stored=False)

        messages = [
            {"role": "system", "content": system_prompt(self.settings.report.language)},
            {
                "role": "user",
                "content": user_prompt(digest.text, previous_report_note(previous, ts)),
            },
        ]
        self.events.publish("report.started", {"ts": ts, "digest_tokens": digest.tokens})
        text_parts: list[str] = []
        usage = None
        latency_s = 0.0
        finish_reason: str | None = None
        try:
            async for chunk in self.llm.stream(
                messages, reasoning_effort=self.settings.report.reasoning_effort
            ):
                if chunk.done:
                    usage = chunk.usage
                    finish_reason = chunk.finish_reason
                    latency_s = chunk.latency_s or 0.0
                    continue
                text_parts.append(chunk.text)
                self.events.publish("report.token", {"text": chunk.text})
        except LLMError as exc:
            return await self._problem("failed", f"LLM request failed: {exc}", ts, digest, exc)

        raw = "".join(text_parts)
        text = strip_reasoning(raw)
        if not text.strip():
            return await self._problem(
                "failed", "The model returned an empty answer.", ts, digest, None
            )
        parsed = parse_report(text, digest.flags)
        truncated = finish_reason == "length"
        if truncated:
            parsed.markdown += TRUNCATED_NOTE
        report_id = await self._save(parsed, digest, ts, usage, latency_s)
        self._last_problem = None
        self.events.publish(
            "report.done",
            {
                "id": report_id,
                "ts": ts,
                "health": parsed.health,
                "score": parsed.score,
                "summary": parsed.summary,
                "findings": parsed.findings,
                "parsed": parsed.parsed,
                "truncated": truncated,
            },
        )
        return RunOutcome("ok", report_id=report_id)

    async def _save(
        self, parsed: ParsedReport, digest: Digest, ts: float, usage: Any, latency_s: float
    ) -> int:
        return await self.store.add(
            ts=ts,
            status="ok",
            markdown=parsed.markdown,
            health=parsed.health,
            score=parsed.score,
            summary=parsed.summary,
            findings=parsed.findings,
            digest=digest.text,
            digest_hash=digest.hash,
            model=self.llm.model,
            tokens_in=usage.prompt_tokens if usage else None,
            tokens_out=usage.completion_tokens if usage else None,
            latency_ms=round(latency_s * 1000),
            parsed=parsed.parsed,
        )

    async def _problem(
        self,
        status: str,
        message: str,
        ts: float,
        digest: Digest | None,
        exc: Exception | None,
    ) -> RunOutcome:
        report_id = await self.store.record_problem(
            ts=ts,
            status=status,
            error=message,
            model=self.llm.model or None,
            digest_hash=digest.hash if digest else None,
        )
        # One log line per distinct problem, not one per interval.
        if message != self._last_problem:
            level = logging.INFO if status == "skipped" else logging.WARNING
            log.log(level, "report %s: %s", status, message)
            self._last_problem = message
        else:
            log.debug("report %s (repeat): %s", status, message)
        self.events.publish("report.failed", {"ts": ts, "status": status, "error": message})
        return RunOutcome(status, report_id=report_id, message=message)
