"""Run one attempt of one scenario through the product's own code and measure it.

`AttemptRunner.run` builds the throw-away world (`world.build_app`), runs the report engine,
the chat engine or the Prometheus planner against it with the real LLM, scores the result with
the checks, and reads the timings from the LLM trace of exactly this attempt.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import statistics
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from ..config import ChatSettings
from ..llm import LLMClient
from ..promauto.catalog import JobSlice
from ..promauto.planner import (
    Planner,
    extract_json_array,
    make_proposal,
    selects_job,
    unwrapped_counter,
)
from ..promauto.store import KV_PLAN, QueryStore
from ..tracing import Tracing, trace_scope
from ..tracing.parse import THOUGHT_ONLY
from . import checks as checks_mod
from .checks import Outcome, ToolCallRecord
from .hardware import FIELDS
from .schema import (
    PRIVATE_PREFIX,
    ChatInput,
    PromPlanInput,
    Scenario,
    ScenarioError,
    Snapshot,
    build_snapshot,
    load_snapshot_file,
)
from .world import BenchApp, build_app, prom_transport

log = logging.getLogger(__name__)

ANSWER_LIMIT = 20_000
"""Characters of the answer kept per attempt."""

LEVELS = ("auto", "none", "low", "medium", "high")
"""The thinking levels: the API's reasoning_effort values. auto = not sent, the model decides."""

EMPTY_MARKS = ("used the whole output limit", THOUGHT_ONLY[:40])
"""Pieces of the failures that mean "the model wrote no answer" (see tracing.parse)."""


def is_empty_answer(text: str | None) -> bool:
    return bool(text) and any(mark in str(text) for mark in EMPTY_MARKS)


class OverlapWatch:
    """Tells whether the app's own report job ran while an attempt did."""

    def __init__(self, hit: Callable[[], bool]):
        self._hit = hit

    def stop(self) -> bool:
        return self._hit()


class RecordingPlanner(Planner):
    """The real planner; remembers what the model answered first for each job (before the
    repair round) so the first-reply checks cannot be hidden by the repair loop."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.first_replies: dict[str, str] = {}
        self.slices: dict[str, JobSlice] = {}
        self._job = ""
        self._asked = 0

    async def _plan_job(self, client: Any, sl: JobSlice, reserved: set[str]) -> Any:
        self._job, self._asked = sl.job, 0
        self.slices[sl.job] = sl
        return await super()._plan_job(client, sl, reserved)

    async def _ask(self, messages: list[dict[str, Any]]) -> str:
        reply = await super()._ask(messages)
        self._asked += 1
        if self._asked == 1:
            self.first_replies[self._job] = reply
        return reply


class AttemptRunner:
    def __init__(
        self,
        llm: LLMClient,
        tracing: Tracing,
        *,
        auto_tool_mode: str,
        private_snapshot: Callable[[str], Any] | None = None,
        overlap: Callable[[], AbstractContextManager[OverlapWatch]] | None = None,
        chat: ChatSettings | None = None,
    ):
        self.llm = llm
        self.tracing = tracing
        self.chat = chat
        self.auto_tool_mode = auto_tool_mode
        self._private = private_snapshot
        self._overlap = overlap
        self._base_docs: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ snapshots

    async def snapshot_for(self, scenario: Scenario) -> Snapshot:
        name = scenario.snapshot
        key = ("private:" if name.startswith(PRIVATE_PREFIX) else "file:") + name
        if key not in self._base_docs:
            if name.startswith(PRIVATE_PREFIX):
                doc = await self._private(name[len(PRIVATE_PREFIX) :]) if self._private else None
                if doc is None:
                    raise ScenarioError(f"unknown private snapshot {name!r}")
                self._base_docs[key] = doc
            else:
                self._base_docs[key] = load_snapshot_file(scenario)
        return build_snapshot(self._base_docs[key], scenario.patch)

    # ------------------------------------------------------------------ one attempt

    def tool_mode_for(self, scenario: Scenario) -> str:
        if scenario.input.kind != "chat":
            return "none"
        return self.auto_tool_mode if scenario.tool_mode == "auto" else scenario.tool_mode

    def llm_at(self, effort: str | None) -> LLMClient:
        """The connection for one attempt at a thinking level: the same HTTP client, statistics
        and trace, but `reasoning_effort` set for the calls that do not choose their own (the
        chat answer; helper calls such as the routing classifier keep asking for "none")."""
        if effort is None:
            return self.llm
        client = copy.copy(self.llm)
        client.cfg = self.llm.cfg.model_copy(update={"reasoning_effort": effort})
        return client

    async def run(
        self, scenario: Scenario, attempt: int, budget_s: float, effort: str | None = None
    ) -> dict[str, Any]:
        """→ the attempt row (see migrations 0008 and 0012) plus `outcome` for the judge.
        `effort`: the thinking level to run the answer calls at; None = the Settings value."""
        started = time.time()
        outcome = Outcome(kind=scenario.input.kind)
        app: BenchApp | None = None
        status = "ok"
        t0 = time.perf_counter()
        watch_ctx = self._overlap() if self._overlap else nullcontext(OverlapWatch(lambda: False))
        overlap = False
        with watch_ctx as watch, trace_scope("bench") as scope:
            try:
                snapshot = await self.snapshot_for(scenario)
                mode = self.tool_mode_for(scenario)
                app = await build_app(
                    scenario,
                    snapshot,
                    self.llm_at(effort),
                    self.tracing,
                    tool_mode=mode,
                    chat=self.chat,
                    effort=effort,
                )
                outcome.tool_mode = mode if scenario.input.kind == "chat" else None
                t0 = time.perf_counter()
                async with asyncio.timeout(budget_s):
                    await self._execute(scenario, app, outcome)
            except TimeoutError:
                status = "timeout"
                outcome.error = f"timed out after {budget_s:.0f}s"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("bench %s attempt %d crashed", scenario.id, attempt, exc_info=True)
                status = "error"
                outcome.error = f"{type(exc).__name__}: {exc}"[:300]
            wall = time.perf_counter() - t0
            overlap = watch.stop()
        if outcome.error and status == "ok":
            status = "error"
        if app is not None:
            try:
                await app.tracing.flush()
                await app.close()
            except Exception:
                log.debug("closing the bench world failed", exc_info=True)
        results = checks_mod.run_checks(list(scenario.checks), outcome)
        score = checks_mod.score(results)
        metrics = await self._metrics(scope.call_ids)
        issue = metrics.pop("issue")
        if is_empty_answer(outcome.error):
            issue = "empty"
        return {
            "scenario_id": scenario.id,
            "grp": scenario.group,
            "attempt": attempt,
            "started": started,
            "status": status,
            "score": score,
            "checks": results,
            "answer": outcome.answer[:ANSWER_LIMIT],
            "error": outcome.error,
            "total_ms": round(wall * 1000),
            "tool_calls": len(outcome.tool_calls),
            "tool_mode": outcome.tool_mode,
            "report_overlap": int(overlap),
            "trace_ids": sorted(set(scope.call_ids)),
            "detail": self._detail(outcome),
            "effort": effort,
            "answer_issue": issue,
            **metrics,
            "outcome": outcome,
        }

    @staticmethod
    def _detail(outcome: Outcome) -> dict[str, Any]:
        return {
            "tool_calls": [
                {"name": c.name, "args": c.args, "ok": c.ok, "kind": c.kind}
                for c in outcome.tool_calls
            ],
            "actions": outcome.actions,
            "repairs": outcome.repairs,
            "report": outcome.report,
            "prom": outcome.prom,
        }

    # ------------------------------------------------------------------ the three kinds

    async def _execute(self, scenario: Scenario, app: BenchApp, outcome: Outcome) -> None:
        kind = scenario.input.kind
        if kind == "report":
            await self._report(app, outcome)
        elif kind == "chat":
            assert isinstance(scenario.input, ChatInput)
            await self._chat(scenario.input, app, outcome)
        else:
            assert isinstance(scenario.input, PromPlanInput)
            await self._prom(scenario.input, app, outcome)

    async def _report(self, app: BenchApp, outcome: Outcome) -> None:
        result = await app.reports.run()
        if result.status != "ok" or result.report_id is None:
            outcome.error = result.message or f"report {result.status}"
            return
        row = await app.reports.store.get(result.report_id) or {}
        outcome.answer = row.get("markdown") or ""
        outcome.report = {
            "parsed": bool(row.get("parsed")),
            "health": row.get("health"),
            "score": row.get("score"),
            "summary": row.get("summary"),
            "findings": row.get("findings") or [],
        }

    async def _chat(self, inp: ChatInput, app: BenchApp, outcome: Outcome) -> None:
        assert app.chat is not None
        store = app.chat.store
        session = await store.create_session(inp.messages[0].content, "web")
        for message in inp.messages[:-1]:
            await store.add_message(session, message.role, message.content)
        pending: dict[str, ToolCallRecord] = {}
        async for event in app.chat.run(inp.messages[-1].content, session):
            kind = event.get("type")
            if kind == "tool_call":
                record = ToolCallRecord(
                    event["name"], dict(event.get("args") or {}), None, event.get("kind")
                )
                pending[event["id"]] = record
                outcome.tool_calls.append(record)
            elif kind == "tool_result":
                record = pending.get(event["id"])
                if record is not None:
                    record.ok = bool(event.get("ok"))
            elif kind == "done":
                outcome.answer = event.get("text") or ""
                outcome.repairs = int(event.get("repairs") or 0)
                outcome.tool_mode = event.get("tool_mode") or outcome.tool_mode
            elif kind == "error":
                outcome.error = str(event.get("message") or "chat failed")
        rows = await app.db.fetchall("SELECT action, status, target FROM actions ORDER BY id")
        outcome.actions = [dict(r) for r in rows]

    async def _prom(self, inp: PromPlanInput, app: BenchApp, outcome: Outcome) -> None:
        planner = RecordingPlanner(
            app.settings, app.db, app.llm, transport=prom_transport(inp.catalog)
        )
        result = await planner.run(force=True)
        if result.status == "failed":
            outcome.error = result.message
            return
        accepted = [q for q in await QueryStore(app.db).fetch() if q["source"] == "auto"]
        first: list[dict[str, Any]] = []
        for job, reply in planner.first_replies.items():
            items = extract_json_array(reply) or []
            sl = planner.slices.get(job)
            for raw in items:
                try:
                    proposal = make_proposal(raw, job)
                except ValueError:
                    continue
                bare = unwrapped_counter(proposal.query, sl.counters) if sl else None
                first.append(
                    {
                        "job": job,
                        "query": proposal.query,
                        "bare_counters": [bare] if bare else [],
                        "job_scoped": selects_job(proposal.query, job),
                    }
                )
        plan = await app.db.kv_get(KV_PLAN) or {}
        outcome.prom = {
            "status": result.status,
            "message": result.message,
            "accepted": [
                {"name": q["name"], "query": q["query"], "kind": q.get("kind")} for q in accepted
            ],
            "first_replies": first,
            "rejected": plan.get("rejected") or [],
        }
        outcome.answer = "\n".join(f"{q['name']}: {q['query']}" for q in accepted)

    # ------------------------------------------------------------------ measurements

    async def _metrics(self, call_ids: list[int]) -> dict[str, Any]:
        """Timings and token counts of this attempt, from its rows in the LLM trace."""
        ids = sorted(set(call_ids))
        empty: dict[str, Any] = {
            "ttft_ms": None,
            "tokens_in": None,
            "tokens_out": None,
            "tokens_per_s": None,
            "reasoning_chars": None,
            "issue": None,
            "llm_calls": len(ids),
        }
        if not ids:
            return empty
        marks = ",".join("?" * len(ids))
        rows = await self.tracing.store.db.fetchall(
            "SELECT id, ts, status, error, finish_reason, prompt_tokens, completion_tokens, "
            "ttft_ms, total_ms, reasoning_chars "
            f"FROM llm_calls WHERE id IN ({marks}) ORDER BY ts, id",
            ids,
        )
        if not rows:
            return empty
        ok_rows = [r for r in rows if r["status"] == "ok"] or list(rows)
        first = next((r["ttft_ms"] for r in rows if r["ttft_ms"] is not None), None)
        # Totals count every call, also one that failed after thinking (an empty answer spent
        # its tokens); the speed is read from the successful calls only.
        tin = sum(r["prompt_tokens"] or 0 for r in rows)
        tout = sum(r["completion_tokens"] or 0 for r in rows)
        tout_ok = sum(r["completion_tokens"] or 0 for r in ok_rows)
        gen_ms = sum(
            max((r["total_ms"] or 0) - (r["ttft_ms"] or 0), 0)
            for r in ok_rows
            if r["total_ms"] is not None
        )
        issue = None
        if any(is_empty_answer(r["error"]) for r in rows):
            issue = "empty"
        elif any(r["finish_reason"] == "length" for r in rows):
            issue = "limit"
        return {
            "ttft_ms": first,
            "tokens_in": tin or None,
            "tokens_out": tout or None,
            "tokens_per_s": round(tout_ok / (gen_ms / 1000), 1) if gen_ms > 0 and tout_ok else None,
            "reasoning_chars": sum(r["reasoning_chars"] or 0 for r in rows),
            "issue": issue,
            "llm_calls": len(rows),
        }

    async def relink_traces(
        self, run_id: int, scenario_id: str, attempt: int, ids: list[int]
    ) -> None:
        """Mark the attempt's LLM calls as benchmark calls (purpose `bench`, linked to the
        run/scenario/attempt) so the Logs page can find them."""
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        link = f"{run_id}/{scenario_id}/{attempt}"
        db = self.tracing.store.db
        turns = [
            r["turn"]
            for r in await db.fetchall(
                f"SELECT DISTINCT turn FROM llm_calls WHERE id IN ({marks}) AND turn IS NOT NULL",
                ids,
            )
        ]
        await db.execute(
            "UPDATE llm_calls SET purpose = 'bench', link_kind = 'bench', link_id = ? "
            f"WHERE id IN ({marks})",
            (link, *ids),
        )
        if turns:
            tmarks = ",".join("?" * len(turns))
            await db.execute(
                "UPDATE llm_tool_calls SET link_kind = 'bench', link_id = ? "
                f"WHERE turn IN ({tmarks})",
                (link, *turns),
            )


# --------------------------------------------------------------------------- summaries


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def _scores(attempts: list[dict[str, Any]]) -> tuple[dict[str, float], float | None]:
    """Mean score per group (of the scenarios' mean scores) and their overall mean."""
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for a in attempts:
        by_scenario.setdefault(a["scenario_id"], []).append(a)
    groups: dict[str, list[float]] = {}
    for rows in by_scenario.values():
        mean = sum(float(r["score"] or 0) for r in rows) / len(rows)
        groups.setdefault(rows[0]["grp"], []).append(mean)
    group_scores = {g: round(sum(v) / len(v), 4) for g, v in groups.items()}
    overall = round(sum(group_scores.values()) / len(group_scores), 4) if group_scores else None
    return group_scores, overall


def merge_hw(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The hardware numbers of several attempts as one: the highest peak, the average weighted
    by samples."""
    hws = [r["hw"] for r in rows if r.get("hw")]
    if not hws:
        return None
    out: dict[str, Any] = {"n": sum(h["n"] for h in hws)}
    for field in FIELDS:
        parts = [(h["n"], h[field]) for h in hws if h.get(field)]
        total = sum(n for n, _ in parts)
        out[field] = (
            {
                "avg": round(sum(n * st["avg"] for n, st in parts) / total, 1),
                "peak": max(st["peak"] for _, st in parts),
            }
            if parts and total
            else None
        )
    for field in ("vram_total", "ram_total"):
        vals = [h[field] for h in hws if h.get(field) is not None]
        out[field] = max(vals) if vals else None
    return out


def summarize_levels(attempts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The numbers of each thinking level of a run (attempts without a level, which no level
    changes, are left out): accuracy, time and tokens per answer, empty and cut-off answers."""
    out: dict[str, dict[str, Any]] = {}
    for level in LEVELS:
        rows = [a for a in attempts if a.get("effort") == level]
        if not rows:
            continue
        groups, overall = _scores(rows)
        times = [r["total_ms"] for r in rows if r.get("total_ms") is not None]
        tokens = [r["tokens_out"] for r in rows if r.get("tokens_out") is not None]
        reasoning = [r["reasoning_chars"] for r in rows if r.get("reasoning_chars") is not None]
        out[level] = {
            "attempts": len(rows),
            "overall": overall,
            "group_scores": groups,
            "pass_rate": round(
                sum(1 for r in rows if float(r["score"] or 0) >= 0.999) / len(rows), 4
            ),
            "failed": sum(1 for r in rows if r["status"] != "ok"),
            "empty": sum(1 for r in rows if r.get("answer_issue") == "empty"),
            "limit": sum(1 for r in rows if r.get("answer_issue") == "limit"),
            "median_total_ms": _median(times),
            "median_tokens_out": _median(tokens),
            "median_reasoning_chars": _median(reasoning),
            "tokens_out": sum(tokens),
            "reasoning_chars": sum(reasoning),
            "hw": merge_hw(rows),
        }
    return out


def summarize(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Run-level numbers from its attempts: scores per scenario and group, overall, medians,
    pass rate, failures."""
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for a in attempts:
        by_scenario.setdefault(a["scenario_id"], []).append(a)
    scenarios: dict[str, dict[str, Any]] = {}
    for sid, rows in by_scenario.items():
        scores = [float(r["score"] or 0) for r in rows]
        times = [r["total_ms"] for r in rows if r.get("total_ms") is not None]
        ttfts = [r["ttft_ms"] for r in rows if r.get("ttft_ms") is not None]
        per_level: dict[str, dict[str, Any]] = {}
        for level in LEVELS:
            lrows = [r for r in rows if r.get("effort") == level]
            if lrows:
                ltimes = [r["total_ms"] for r in lrows if r.get("total_ms") is not None]
                per_level[level] = {
                    "score": round(sum(float(r["score"] or 0) for r in lrows) / len(lrows), 4),
                    "attempts": len(lrows),
                    "failed": sum(1 for r in lrows if r["status"] != "ok"),
                    "empty": sum(1 for r in lrows if r.get("answer_issue") == "empty"),
                    "median_total_ms": _median(ltimes),
                }
        scenarios[sid] = {
            "group": rows[0]["grp"],
            "score": round(sum(scores) / len(scores), 4),
            "attempts": len(rows),
            "passed": sum(1 for s in scores if s >= 0.999),
            "failed": sum(1 for r in rows if r["status"] not in ("ok",)),
            "median_total_ms": _median(times),
            "median_ttft_ms": _median(ttfts),
            **({"levels": per_level} if per_level else {}),
        }
    groups: dict[str, list[float]] = {}
    for info in scenarios.values():
        groups.setdefault(info["group"], []).append(info["score"])
    group_scores = {g: round(sum(v) / len(v), 4) for g, v in groups.items()}
    overall = round(sum(group_scores.values()) / len(group_scores), 4) if group_scores else None
    ok = [a for a in attempts if a["status"] == "ok"]
    return {
        "overall": overall,
        "group_scores": group_scores,
        "summary": {
            "scenarios": scenarios,
            "attempts": len(attempts),
            "pass_rate": round(
                sum(1 for a in attempts if float(a["score"] or 0) >= 0.999) / len(attempts), 4
            )
            if attempts
            else None,
            "failures": len(attempts) - len(ok),
            "median_ttft_ms": _median([a["ttft_ms"] for a in ok if a.get("ttft_ms") is not None]),
            "median_total_ms": _median(
                [a["total_ms"] for a in ok if a.get("total_ms") is not None]
            ),
            "median_tokens_per_s": _median(
                [a["tokens_per_s"] for a in ok if a.get("tokens_per_s") is not None]
            ),
            "tokens_in": sum(a.get("tokens_in") or 0 for a in attempts),
            "tokens_out": sum(a.get("tokens_out") or 0 for a in attempts),
            "empty_answers": sum(1 for a in attempts if a.get("answer_issue") == "empty"),
            "limit_hits": sum(1 for a in attempts if a.get("answer_issue") == "limit"),
            "overlapped": sum(1 for a in attempts if a.get("report_overlap")),
            "reload_suspected": sum(1 for a in attempts if a.get("reload_suspected")),
        },
    }
