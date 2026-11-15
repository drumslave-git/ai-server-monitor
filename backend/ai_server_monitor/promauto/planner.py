"""The planner: asks the LLM which PromQL queries are worth running, and proves each one.

One small LLM call per Prometheus job (a 4B model copes with ~700 tokens of catalog, not
with 13k). Every proposed query is then run against the real Prometheus: it must parse,
answer in time, return data (alert-style queries may be empty) and stay small. A failure
goes back to the model once with Prometheus's own error; what still fails is dropped.
A model that produces nothing leaves the deterministic fallback set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..db import Database
from ..llm import LLMClient, LLMError
from ..llm.text import strip_reasoning
from ..promql import PromQL, PromQLError
from ..thresholds import GRAMMAR_HELP, breaches, normalize_threshold
from .catalog import _TOKEN, Catalog, JobSlice, build_catalog, mentions_known_metric
from .store import KV_PLAN, MAX_QUERY, Proposal, QueryStore, clean_name

log = logging.getLogger(__name__)

PER_JOB = 3
"""Queries asked of the model per Prometheus job."""
MAX_AUTO_SERIES = 20
"""An accepted query returns at most this many series (aggregate instead)."""
MAX_REJECTED_KEPT = 30
_SELECTORS = re.compile(r"\{[^}]*\}|\[[^\]]*\]")
_FILTERS = re.compile(r"==|!=|>=|<=|>|<")
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)

COVERED = (
    "host CPU, memory and swap, disks and filesystems, network traffic, temperatures, top "
    "processes, Docker containers (state, CPU, RAM, restarts, log errors), the NVIDIA GPU of "
    "this host, failed systemd units, the system journal, pending apt updates, container "
    "image updates and the *arr apps"
)

SYSTEM_PROMPT = """\
You choose PromQL queries for a server-monitoring digest. A Prometheus server scrapes the \
job described below.

The monitor ALREADY collects by itself: {covered}. Do not repeat those. Choose up to \
{limit} queries that add something the monitor cannot see: errors, drops, saturation or \
pressure, latency, queue lengths, health or state, or anything about another machine or \
service.

Rules:
- Use ONLY metric names from the list. Never invent metric names or labels.
- Every metric selector must contain the job label of this job, e.g. \
node_load5{{job="{job}"}}, because other jobs may export the same metric names.
- The query must return only a few values: aggregate with sum, max or topk(3, ...) and keep \
"by (instance)" or "by (name)" when the parts matter.
- Metrics marked c in the list are COUNTERS: they only ever grow, so a bare value is \
meaningless. Always wrap them: rate(x[5m]) for a speed or increase(x[1h]) for a count. \
Never put a limit on a bare counter.
- One instant query, no subqueries, no range vectors as the final result.
- kind "value" = the query always has an answer. kind "alert" = the query only returns \
something when there is a problem, e.g. "... > 0"; it may be empty.
- warn_if / crit_if are optional. Form: an operator and a number only, e.g. "> 80", \
"<= 1", "== 0".
- unit: one of "%", "bytes", "bytes/s", "seconds", "celsius", "count", "per_s", "ratio" \
or "".
- Reply with ONLY a JSON array, no other text.

Example reply:
[{{"name": "disk_queue_depth", "query": "max by (instance) (node_disk_io_now{{job=\\"{job}\\"}})", \
"description": "Requests waiting on the busiest disk", "unit": "count", "kind": "value", \
"warn_if": "> 8", "crit_if": "> 32"}}]
"""

REPAIR_JSON = (
    "That was not a valid JSON array. Reply with ONLY the JSON array, in the format shown, "
    "and nothing else."
)


def system_prompt(limit: int, job: str = "node") -> str:
    return SYSTEM_PROMPT.format(covered=COVERED, limit=limit, job=job)


def user_prompt(sl: JobSlice) -> str:
    where = ""
    if sl.other_host:
        where = (
            f"This job scrapes ANOTHER machine ({sl.other_host}), not the monitored server. "
            "Keep the instance label in the result.\n"
        )
    return f"{where}<catalog>\n{sl.text}\n</catalog>\n\nChoose the queries now."


def repair_prompt(failures: list[dict[str, str]]) -> str:
    lines = [f"- {f['name']}: {f['reason']}" for f in failures]
    return (
        "These queries were checked against the real Prometheus and failed:\n"
        + "\n".join(lines)
        + "\nReply with ONLY a JSON array of corrected versions of these queries (same "
        "format). Leave out any you cannot fix."
    )


# --------------------------------------------------------------------------- parsing


def extract_json_array(text: str) -> list[Any] | None:
    """The first JSON array of objects in model output (fenced, bare or embedded).

    A lone object, or an object wrapping the list under "queries", is accepted too.
    """
    text = strip_reasoning(text)
    candidates = [text, *(m.group(1).strip() for m in _FENCE.finditer(text))]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        found = _as_list(value)
        if found is not None:
            return found
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        found = _as_list(value)
        if found is not None:
            return found
    return None


def _as_list(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value if all(isinstance(v, dict) for v in value) else None
    if isinstance(value, dict):
        inner = value.get("queries")
        if isinstance(inner, list):
            return inner
        if "query" in value:
            return [value]
    return None


def make_proposal(raw: Any, job: str) -> Proposal:
    """Check and normalize one item of the model's list. Raises ValueError with a reason
    that can be shown to the model."""
    if not isinstance(raw, dict):
        raise ValueError("not a JSON object")
    name = clean_name(raw.get("name"))
    if not name:
        raise ValueError("missing name")
    query = " ".join(str(raw.get("query") or "").split())
    if not query:
        raise ValueError("missing query")
    if len(query) > MAX_QUERY:
        raise ValueError(f"query longer than {MAX_QUERY} characters")
    thresholds: dict[str, str | None] = {}
    for key in ("warn_if", "crit_if"):
        value = raw.get(key)
        if value in (None, ""):
            thresholds[key] = None
            continue
        try:
            thresholds[key] = normalize_threshold(value if isinstance(value, str) else None)
        except ValueError as exc:
            raise ValueError(f"{key}: {exc}") from exc
        if thresholds[key] is None:
            raise ValueError(f"{key}: not a threshold. {GRAMMAR_HELP}")
    kind = "alert" if str(raw.get("kind") or "").strip().lower() == "alert" else "value"
    if kind == "alert" and not _FILTERS.search(_SELECTORS.sub("", query)):
        kind = "value"  # nothing filters the result, so it is never "empty when healthy"
    return Proposal(
        name=name,
        query=query,
        description=" ".join(str(raw.get("description") or "").split())[:160],
        unit=str(raw.get("unit") or "").strip()[:12],
        kind=kind,
        warn_if=thresholds["warn_if"],
        crit_if=thresholds["crit_if"],
        job=job,
    )


def selects_job(query: str, job: str) -> bool:
    """Does the query restrict itself to `job` (job="x" or job=~"x|y")?"""
    return re.search(r"job\s*=~?\s*[\"'][^\"']*" + re.escape(job), query) is not None


_RATE_FUNCTIONS = r"(?:rate|irate|increase|resets|changes|idelta|deriv)"


def unwrapped_counter(query: str, counters: set[str]) -> str | None:
    """A counter used as a plain value, not through rate()/increase()/...: it only grows, so
    a threshold on it stays crossed forever. Returns the first such metric name."""
    for match in _TOKEN.finditer(query):
        name = match.group(0)
        if name not in counters:
            continue
        before = query[: match.start()]
        if before.count('"') % 2:
            continue  # inside a label value
        after = query[match.end() :]
        wrapped = re.search(_RATE_FUNCTIONS + r"\(\s*$", before) is not None
        ranged = re.match(r"\s*(\{[^}]*\})?\s*\[", after) is not None
        if not (wrapped and ranged):
            return name
    return None


async def validate(
    client: PromQL,
    p: Proposal,
    metrics: dict[str, int] | None,
    job: str | None = None,
    counters: set[str] | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Run the query. Returns (result dict, None) when it is good, else (None, reason)."""
    if metrics is not None and not mentions_known_metric(p.query, metrics):
        return None, "uses no metric from the list (invented metric names?)"
    if job and not selects_job(p.query, job):
        return None, f'has no {{job="{job}"}} selector (other jobs export the same names)'
    bare = unwrapped_counter(p.query, counters) if counters else None
    if bare:
        return None, (
            f"{bare} is a counter (it only ever grows); use rate({bare}[5m]) or "
            f"increase({bare}[1h]) instead"
        )
    try:
        result = await client.instant(p.query)
    except PromQLError as exc:
        return None, str(exc)
    data = result.to_dict()
    if result.result_type == "matrix":
        return None, "returns a range vector; wrap it in rate(), increase() or max_over_time()"
    if result.result_type == "string":
        return None, "returns a string"
    if result.total == 0:
        if p.kind == "alert":
            return data, None
        return None, "returned no data (metric or label does not exist?)"
    if result.total > MAX_AUTO_SERIES:
        return None, f"returns {result.total} series; aggregate with sum, max or topk(3, ...)"
    if not any(isinstance(s.get("value"), int | float) for s in result.series):
        return None, "returns no finite number (NaN or Inf)"
    return data, None


def _drop_stale_thresholds(p: Proposal, result: dict[str, Any]) -> None:
    """A threshold the current value already crosses is a model's guess, not knowledge: the
    server is presumably fine right now, so it would only raise a false alarm."""
    if p.kind == "alert":
        return
    values = [s.get("value") for s in result.get("series") or []]
    if p.warn_if and any(breaches(p.warn_if, v) for v in values):
        p.warn_if = None
    if p.crit_if and any(breaches(p.crit_if, v) for v in values):
        p.crit_if = None


# --------------------------------------------------------------------------- fallback

FALLBACK = (
    Proposal(
        name="scrape_slowest",
        query="max(scrape_duration_seconds)",
        description="Slowest scrape of any Prometheus target",
        unit="seconds",
        warn_if="> 10",
    ),
    Proposal(
        name="rule_eval_failures",
        query="sum(increase(prometheus_rule_evaluation_failures_total[1h]))",
        description="Prometheus rule evaluations that failed in the last hour",
        unit="count",
        warn_if="> 0",
    ),
    Proposal(
        name="samples_scraped",
        query="sum(scrape_samples_scraped)",
        description="Samples Prometheus collected per scrape round",
        unit="count",
    ),
)
"""Used when no model is available. Down targets and firing alerts need no query: the
collector reads them from the targets and alerts APIs. Candidates that Prometheus cannot
answer (no self-scrape, say) are dropped by the same validation as the model's."""


# --------------------------------------------------------------------------- planner


@dataclass
class PlanOutcome:
    status: str
    """ok | fallback | skipped | failed"""
    message: str = ""
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "message": self.message, "counts": self.counts}


class Planner:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        llm: LLMClient,
        *,
        transport: Any = None,
    ):
        self.settings = settings
        self.db = db
        self.llm = llm
        self.store = QueryStore(db)
        self._transport = transport
        self.force_next = False
        self.running = False
        self.progress = ""
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ status

    async def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "progress": self.progress,
            "last": await self.db.kv_get(KV_PLAN),
        }

    def enabled(self) -> bool:
        cfg = self.settings.collectors.prometheus
        return bool(cfg.url) and cfg.enabled is not False and cfg.auto_queries

    # ------------------------------------------------------------------ scheduler entry

    async def run_job(self) -> None:
        """Hourly tick: plans only when the catalog changed or the interval has passed."""
        force, self.force_next = self.force_next, False
        await self.run(force=force)

    def time_limit(self, jobs: int) -> float:
        cfg = self.settings.llm
        per_call = cfg.timeout.total_seconds() * (cfg.max_retries + 1)
        return per_call * 2 * max(jobs, 1) + 120

    async def run(self, *, force: bool = False) -> PlanOutcome:
        if self._lock.locked():
            return PlanOutcome("skipped", "a plan is already running")
        async with self._lock:
            self.running = True
            self.progress = "reading Prometheus"
            try:
                return await self._run(force)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("prometheus planning failed")
                return await self._finish(
                    PlanOutcome("failed", f"{type(exc).__name__}: {exc}"), "", None
                )
            finally:
                self.running = False
                self.progress = ""

    def _client(self, *, big: bool = False) -> PromQL:
        cfg = self.settings.collectors.prometheus
        return PromQL(
            cfg.url,
            token=cfg.token.get_secret_value(),
            username=cfg.username,
            password=cfg.password.get_secret_value(),
            timeout=cfg.timeout.total_seconds(),
            max_series=5000 if big else MAX_AUTO_SERIES + 1,
            transport=self._transport,
        )

    async def _run(self, force: bool) -> PlanOutcome:
        cfg = self.settings.collectors.prometheus
        if not self.enabled():
            return PlanOutcome("skipped", "automatic queries are off or Prometheus is not set")
        started = time.time()
        client, big = self._client(), self._client(big=True)
        try:
            try:
                catalog = await build_catalog(client, big)
            except PromQLError as exc:
                return await self._finish(
                    PlanOutcome("failed", f"cannot read Prometheus: {exc}"), "", None
                )
            last = await self.db.kv_get(KV_PLAN) or {}
            llm_ok = self.llm.cfg.configured
            interval = cfg.replan_interval.total_seconds()
            due = (
                force
                or last.get("hash") != catalog.hash
                or started - float(last.get("ts") or 0) >= interval
                or (llm_ok and last.get("source") == "fallback")
            )
            if not due:
                return PlanOutcome("skipped", "catalog unchanged and not due yet")
            usable = [j for j in catalog.jobs if j.usable]
            existing = await self.store.fetch()
            have_auto = any(q["source"] == "auto" for q in existing)
            if not llm_ok:
                if have_auto:
                    return PlanOutcome("skipped", "LLM is not configured; kept the current queries")
                return await self._apply_fallback(client, catalog, started, "LLM is not configured")
            if not usable:
                return await self._apply_fallback(
                    client, catalog, started, "Prometheus has no usable metrics yet"
                )

            reserved = {q.name for q in cfg.queries} | set(await self.store.dismissed())
            per_job: list[list[Proposal]] = []
            rejected: list[dict[str, str]] = []
            errors: list[str] = []
            calls = 0
            async with asyncio.timeout(self.time_limit(len(usable))):
                for i, sl in enumerate(usable, start=1):
                    self.progress = f"asking the model about {sl.job} ({i}/{len(usable)})"
                    try:
                        accepted, bad, n = await self._plan_job(client, sl, reserved)
                    except LLMError as exc:
                        errors.append(f"{sl.job}: {exc}")
                        per_job.append([])
                        continue
                    calls += n
                    per_job.append(accepted)
                    rejected.extend(bad)
            kept = sum(1 for q in existing if q["pinned"] or q["edited"])
            merged = self._merge(usable, per_job, reserved, max(1, cfg.max_auto_queries - kept))
            if not merged:
                reason = "; ".join(errors[:2]) or "the model proposed no usable queries"
                if have_auto:
                    return await self._finish(
                        PlanOutcome("failed", f"{reason}; kept the current queries"),
                        catalog.hash,
                        None,
                        rejected=rejected,
                    )
                return await self._apply_fallback(
                    client, catalog, started, reason, rejected=rejected
                )
            counts = await self.store.apply_plan(merged, source="auto")
            outcome = PlanOutcome(
                "ok",
                f"{len(merged)} queries chosen from {len(usable)} jobs"
                + (f" ({len(errors)} job(s) failed)" if errors else ""),
                counts,
            )
            return await self._finish(
                outcome, catalog.hash, "llm", rejected=rejected, started=started, calls=calls
            )
        finally:
            await client.close()
            await big.close()

    async def _apply_fallback(
        self,
        client: PromQL,
        catalog: Catalog,
        started: float,
        why: str,
        *,
        rejected: list[dict[str, str]] | None = None,
    ) -> PlanOutcome:
        reserved = {q.name for q in self.settings.collectors.prometheus.queries}
        reserved |= set(await self.store.dismissed())
        good: list[Proposal] = []
        for p in FALLBACK:
            if p.name in reserved:
                continue
            result, error = await validate(client, p, None)
            if result is not None:
                p.last_value = result
                good.append(p)
            else:
                log.debug("fallback query %s dropped: %s", p.name, error)
        counts = await self.store.apply_plan(good, source="fallback")
        outcome = PlanOutcome("fallback", f"{why}; using the built-in queries", counts)
        return await self._finish(
            outcome, catalog.hash, "fallback", rejected=rejected, started=started
        )

    async def _finish(
        self,
        outcome: PlanOutcome,
        catalog_hash: str,
        source: str | None,
        *,
        rejected: list[dict[str, str]] | None = None,
        started: float | None = None,
        calls: int = 0,
    ) -> PlanOutcome:
        now = time.time()
        previous = await self.db.kv_get(KV_PLAN) or {}
        record: dict[str, Any] = {
            "ts": now,
            "status": outcome.status,
            "message": outcome.message,
            "counts": outcome.counts,
            "model": self.llm.model or None,
            "llm_calls": calls,
            "duration_s": round(now - started, 1) if started else None,
            "rejected": (rejected or [])[:MAX_REJECTED_KEPT],
        }
        if outcome.status in ("ok", "fallback"):
            record.update(hash=catalog_hash, source=source)
            record["ok_ts"] = now
        else:
            # A failed attempt must not hide the last good plan (or re-trigger on its hash).
            record.update(
                hash=previous.get("hash"),
                source=previous.get("source"),
                ok_ts=previous.get("ok_ts"),
            )
        await self.db.kv_set(KV_PLAN, record)
        level = logging.INFO if outcome.status in ("ok", "fallback") else logging.WARNING
        log.log(level, "prometheus plan %s: %s", outcome.status, outcome.message)
        return outcome

    # ------------------------------------------------------------------ one job

    async def _ask(self, messages: list[dict[str, Any]]) -> str:
        completion = await self.llm.complete(
            messages, max_tokens=min(self.llm.cfg.max_output_tokens, 2000)
        )
        return completion.content

    async def _plan_job(
        self, client: PromQL, sl: JobSlice, reserved: set[str]
    ) -> tuple[list[Proposal], list[dict[str, str]], int]:
        """Ask, validate, repair once. Returns (accepted, rejected-with-reasons, LLM calls)."""
        limit = min(PER_JOB, self.settings.collectors.prometheus.max_auto_queries)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(limit, sl.job)},
            {"role": "user", "content": user_prompt(sl)},
        ]
        calls = 1
        reply = await self._ask(messages)
        items = extract_json_array(reply)
        if items is None:
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": REPAIR_JSON},
            ]
            calls += 1
            reply = await self._ask(messages)
            items = extract_json_array(reply)
        if items is None:
            return (
                [],
                [{"job": sl.job, "name": "(reply)", "query": "", "reason": "not JSON"}],
                calls,
            )

        accepted, failures = await self._check(client, sl, items, reserved, [])
        if failures:
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": repair_prompt(failures)},
            ]
            calls += 1
            try:
                fixed = extract_json_array(await self._ask(messages)) or []
            except LLMError:
                fixed = []
            more, failures = await self._check(client, sl, fixed, reserved, accepted)
            accepted += more
        bad = [{**f, "job": sl.job} for f in failures]
        return accepted[:limit], bad, calls

    async def _check(
        self,
        client: PromQL,
        sl: JobSlice,
        items: list[Any],
        reserved: set[str],
        already: list[Proposal],
    ) -> tuple[list[Proposal], list[dict[str, str]]]:
        taken = {p.name for p in already}
        failures: list[dict[str, str]] = []
        candidates: list[Proposal] = []
        for raw in items[: PER_JOB * 2]:
            try:
                p = make_proposal(raw, sl.job)
            except ValueError as exc:
                name = clean_name(raw.get("name")) if isinstance(raw, dict) else "?"
                failures.append({"name": name or "?", "query": str(raw)[:120], "reason": str(exc)})
                continue
            if p.name in reserved or p.name in taken:
                continue
            taken.add(p.name)
            candidates.append(p)
        sem = asyncio.Semaphore(4)

        async def check(p: Proposal) -> tuple[Proposal, dict[str, Any] | None, str | None]:
            async with sem:
                result, error = await validate(client, p, sl.metrics, sl.job, sl.counters)
            return p, result, error

        accepted: list[Proposal] = []
        for p, result, error in await asyncio.gather(*(check(p) for p in candidates)):
            if result is None:
                failures.append({"name": p.name, "query": p.query, "reason": error or "failed"})
            else:
                p.last_value = result
                _drop_stale_thresholds(p, result)
                accepted.append(p)
        return accepted, failures

    # ------------------------------------------------------------------ merging

    @staticmethod
    def _merge(
        jobs: list[JobSlice], per_job: list[list[Proposal]], reserved: set[str], cap: int
    ) -> list[Proposal]:
        """Round-robin over the jobs (so one chatty exporter cannot take every slot)."""
        out: list[Proposal] = []
        names = set(reserved)
        queries: set[str] = set()
        depth = max((len(p) for p in per_job), default=0)
        for level in range(depth):
            for sl, proposals in zip(jobs, per_job, strict=False):
                if level >= len(proposals) or len(out) >= cap:
                    continue
                p = proposals[level]
                if p.name in names:
                    p.name = clean_name(f"{p.name}_{sl.job}")
                if p.name in names or not p.name or p.query in queries:
                    continue
                names.add(p.name)
                queries.add(p.query)
                out.append(p)
        return out
