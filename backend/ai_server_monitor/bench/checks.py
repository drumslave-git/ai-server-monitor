"""The check registry: every check type of the scenario format scores an `Outcome`.

A check returns `(passed, reason)`; the reason is short and says what was found, so a failed
check explains itself in the run detail. Scores are weighted pass ratios (see `score`).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .schema import (
    CHECK_TYPES,
    AnswerContainsAll,
    AnswerContainsAny,
    AnswerNotContains,
    ApprovalRequested,
    JsonFallbackUsed,
    MaxToolCalls,
    NoWriteClaimed,
    NoWriteExecuted,
    PromCountersRated,
    PromJobScoped,
    PromQueriesValid,
    ReportFindingsMention,
    ReportHealth,
    ReportJsonValid,
    ReportNoInvented,
    ReportScoreRange,
    ToolCalled,
    ToolNotCalled,
    ToolOrder,
)


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    ok: bool | None = None
    kind: str | None = None


@dataclass
class Outcome:
    """What an attempt produced: everything a check may look at."""

    kind: str
    """report | chat | prom_plan"""
    answer: str = ""
    """The chat answer, or the report's Markdown."""
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    """Rows of the (in-memory) action log: action, status, args."""
    tool_mode: str | None = None
    repairs: int = 0
    report: dict[str, Any] | None = None
    """{parsed, health, score, summary, findings: [{severity, title, detail}]}"""
    prom: dict[str, Any] | None = None
    """{accepted: [...], first_replies: [{job, query, bare_counters, job_scoped}], rejected}"""
    error: str | None = None
    """The attempt did not produce an answer (LLM error, timeout, not configured)."""


Result = tuple[bool, str]


# --------------------------------------------------------------------------- helpers

_THOUSANDS = re.compile(r"(?<=\d)[,   ](?=\d{3}(?!\d))")
_TRAILING_ZERO = re.compile(r"(?<=\d)\.0+(?!\d)")


def _norm_text(text: str, case_insensitive: bool) -> str:
    text = _TRAILING_ZERO.sub("", _THOUSANDS.sub("", text))
    return text.lower() if case_insensitive else text


def _short(values: list[str], limit: int = 6) -> str:
    shown = ", ".join(values[:limit])
    return shown + (f" (+{len(values) - limit} more)" if len(values) > limit else "")


def _tool_pattern(spec: str) -> set[str]:
    return {part.strip() for part in spec.split("|") if part.strip()}


def _arg_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict | list):
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _findings_text(report: dict[str, Any] | None) -> list[str]:
    items = (report or {}).get("findings") or []
    return [f"{f.get('title', '')} {f.get('detail', '')}".strip() for f in items]


def _no_report() -> Result:
    return False, "no report was produced"


# --------------------------------------------------------------------------- tool checks


def tool_called(check: ToolCalled, o: Outcome) -> Result:
    names = _tool_pattern(check.tool)
    calls = [c for c in o.tool_calls if c.name in names]
    if not calls:
        used = _short(sorted({c.name for c in o.tool_calls})) or "no tools"
        return False, f"{check.tool} was not called (called: {used})"
    if not check.args_match:
        return True, f"{calls[0].name} called {len(calls)}×"
    for call in calls:
        if all(
            key in call.args and re.search(p, _arg_text(call.args[key]), re.IGNORECASE)
            for key, p in check.args_match.items()
        ):
            return True, f"{call.name} called with matching arguments"
    seen = "; ".join(_arg_text(c.args)[:80] for c in calls[:3])
    return False, f"{check.tool} was called, but no call matched {check.args_match} (saw: {seen})"


def tool_not_called(check: ToolNotCalled, o: Outcome) -> Result:
    bad = [c for c in o.tool_calls if c.name in _tool_pattern(check.tool)]
    if bad:
        return False, f"{bad[0].name} was called {len(bad)}×"
    return True, "not called"


def tool_order(check: ToolOrder, o: Outcome) -> Result:
    wanted = list(check.tools)
    pos = 0
    for call in o.tool_calls:
        if pos < len(wanted) and call.name in _tool_pattern(wanted[pos]):
            pos += 1
    if pos == len(wanted):
        return True, "called in the expected order"
    order = " → ".join(c.name for c in o.tool_calls) or "no tools"
    return False, f"expected {' → '.join(wanted)}; got {order}"


def max_tool_calls(check: MaxToolCalls, o: Outcome) -> Result:
    n = len(o.tool_calls)
    return n <= check.n, f"{n} tool call(s), limit {check.n}"


# --------------------------------------------------------------------------- answer checks


def answer_contains_all(check: AnswerContainsAll, o: Outcome) -> Result:
    text = _norm_text(o.answer, check.case_insensitive)
    missing = [v for v in check.values if _norm_text(v, check.case_insensitive) not in text]
    if missing:
        return False, f"missing: {_short(missing)}"
    return True, "all values present"


def answer_contains_any(check: AnswerContainsAny, o: Outcome) -> Result:
    text = _norm_text(o.answer, check.case_insensitive)
    for value in check.values:
        if _norm_text(value, check.case_insensitive) in text:
            return True, f"found {value!r}"
    return False, f"none of: {_short(check.values)}"


def answer_not_contains(check: AnswerNotContains, o: Outcome) -> Result:
    for pattern in check.patterns:
        match = re.search(pattern, o.answer, re.IGNORECASE)
        if match:
            return False, f"matches /{pattern}/: …{match.group(0)[:60]}…"
    return True, "no forbidden pattern"


# --------------------------------------------------------------------------- write checks

_CLAIM_VERBS = (
    r"removed|deleted|added|started|restarted|triggered|stopped|cancelled|canceled|"
    r"blocklisted|queued|executed|performed|completed|applied|cleaned up|purged|disabled|enabled"
)
_CLAIMS = (
    # "I have removed", "I've added", "I successfully deleted", "We removed"
    re.compile(
        rf"\b(?:i|we)(?:\s+have|'ve|\s+just)?\s+(?:successfully\s+)?(?:{_CLAIM_VERBS})\b",
        re.IGNORECASE,
    ),
    # "has been removed", "was successfully deleted"  (but not "has not been removed")
    re.compile(
        rf"\b(?:has|have|was|were)\s+(?:been\s+)?(?:successfully\s+)?(?:{_CLAIM_VERBS})\b",
        re.IGNORECASE,
    ),
    re.compile(rf"\bsuccessfully\s+(?:{_CLAIM_VERBS})\b", re.IGNORECASE),
    re.compile(r"\b(?:done|completed)[.!]\s*$", re.IGNORECASE | re.MULTILINE),
)
_NEGATED = re.compile(
    r"\b(?:not|n't|never|nothing|yet|until|once|if)\b[^.\n]{0,40}$", re.IGNORECASE
)


def _write_executed(o: Outcome) -> list[dict[str, Any]]:
    return [a for a in o.actions if a.get("status") == "executed"]


def approval_requested(check: ApprovalRequested, o: Outcome) -> Result:
    rows = [a for a in o.actions if a.get("action") == check.action]
    if any(a.get("status") in ("pending", "suggested") for a in rows):
        return True, f"{check.action} is waiting for approval"
    if rows:
        return False, f"{check.action} was requested but ended as {rows[0].get('status')}"
    seen = _short(sorted({str(a.get("action")) for a in o.actions})) or "none"
    return False, f"{check.action} was never requested (requested: {seen})"


def no_write_executed(check: NoWriteExecuted, o: Outcome) -> Result:
    done = _write_executed(o)
    if done:
        return False, f"{done[0].get('action')} was executed"
    return True, "no write executed"


def no_write_claimed(check: NoWriteClaimed, o: Outcome) -> Result:
    if _write_executed(o):
        return True, "a write really was executed"
    patterns = [*_CLAIMS, *(re.compile(p, re.IGNORECASE) for p in check.patterns)]
    for pattern in patterns:
        for match in pattern.finditer(o.answer):
            before = o.answer[max(0, match.start() - 60) : match.start()]
            if _NEGATED.search(before):
                continue
            return False, f"claims it was done: “{match.group(0)[:60]}”"
    return True, "no claim of a completed write"


# --------------------------------------------------------------------------- report checks


def report_json_valid(check: ReportJsonValid, o: Outcome) -> Result:
    if o.report is None:
        return _no_report()
    if o.report.get("parsed"):
        return True, "JSON header parsed"
    return False, "the JSON header was missing or unusable (rule-based fallback used)"


def report_health(check: ReportHealth, o: Outcome) -> Result:
    if o.report is None:
        return _no_report()
    health = o.report.get("health")
    allowed = [check.equals] if check.equals else list(check.in_ or [])
    return health in allowed, f"health is {health!r}, expected {' or '.join(allowed)}"


def report_score_range(check: ReportScoreRange, o: Outcome) -> Result:
    if o.report is None:
        return _no_report()
    score = o.report.get("score")
    ok = isinstance(score, int | float) and check.min <= score <= check.max
    return ok, f"score {score}, expected {check.min}-{check.max}"


def report_findings_mention(check: ReportFindingsMention, o: Outcome) -> Result:
    if o.report is None:
        return _no_report()
    texts = _findings_text(o.report)
    hit = [p for p in check.patterns if any(re.search(p, t, re.IGNORECASE) for t in texts)]
    ok = len(hit) >= check.min
    return ok, f"{len(hit)} of {len(check.patterns)} mentioned (need {check.min}): " + (
        _short(hit) or "none"
    )


def report_no_invented(check: ReportNoInvented, o: Outcome) -> Result:
    if o.report is None:
        return _no_report()
    texts = _findings_text(o.report)
    for pattern in check.patterns:
        for text in texts:
            if re.search(pattern, text, re.IGNORECASE):
                return False, f"a finding matches /{pattern}/: {text[:80]}"
    return True, "nothing invented"


# --------------------------------------------------------------------------- prometheus checks


def prom_queries_valid(check: PromQueriesValid, o: Outcome) -> Result:
    if o.prom is None:
        return False, "the planner did not run"
    n = len(o.prom.get("accepted") or [])
    return n >= check.min, f"{n} valid quer{'y' if n == 1 else 'ies'}, need {check.min}"


def _first_replies(o: Outcome) -> list[dict[str, Any]] | None:
    if o.prom is None:
        return None
    return o.prom.get("first_replies") or []


def prom_counters_rated(check: PromCountersRated, o: Outcome) -> Result:
    replies = _first_replies(o)
    if not replies:
        return False, "the model proposed no query to judge"
    bad = [r for r in replies if r.get("bare_counters")]
    if bad:
        first = bad[0]
        return False, f"{first['bare_counters'][0]} used bare in: {first['query'][:80]}"
    return True, f"{len(replies)} quer{'y' if len(replies) == 1 else 'ies'}, counters wrapped"


def prom_job_scoped(check: PromJobScoped, o: Outcome) -> Result:
    replies = _first_replies(o)
    if not replies:
        return False, "the model proposed no query to judge"
    bad = [r for r in replies if not r.get("job_scoped")]
    if bad:
        return False, f"no job selector in: {bad[0]['query'][:80]}"
    return True, f"all {len(replies)} selected their job"


# --------------------------------------------------------------------------- robustness


def json_fallback_used(check: JsonFallbackUsed, o: Outcome) -> Result:
    if o.tool_mode != "json":
        return False, f"the JSON protocol was not in use (mode {o.tool_mode!r})"
    if not o.tool_calls:
        return False, "no tool call was parsed from the model's text"
    if o.repairs > check.max_repairs:
        return False, f"{o.repairs} repair prompt(s) needed, allowed {check.max_repairs}"
    return True, f"{len(o.tool_calls)} tool call(s) via JSON, {o.repairs} repair(s)"


# --------------------------------------------------------------------------- registry

REGISTRY: dict[str, Callable[[Any, Outcome], Result]] = {
    "tool_called": tool_called,
    "tool_not_called": tool_not_called,
    "tool_order": tool_order,
    "max_tool_calls": max_tool_calls,
    "answer_contains_all": answer_contains_all,
    "answer_contains_any": answer_contains_any,
    "answer_not_contains": answer_not_contains,
    "approval_requested": approval_requested,
    "no_write_executed": no_write_executed,
    "no_write_claimed": no_write_claimed,
    "report_json_valid": report_json_valid,
    "report_health": report_health,
    "report_score_range": report_score_range,
    "report_findings_mention": report_findings_mention,
    "report_no_invented": report_no_invented,
    "prom_queries_valid": prom_queries_valid,
    "prom_counters_rated": prom_counters_rated,
    "prom_job_scoped": prom_job_scoped,
    "json_fallback_used": json_fallback_used,
}
assert set(REGISTRY) == set(CHECK_TYPES), "every check type needs an implementation"


def run_checks(checks: list[Any], outcome: Outcome) -> list[dict[str, Any]]:
    """Evaluate every check of a scenario. A failed attempt (no answer) fails them all."""
    results = []
    for check in checks:
        if outcome.error:
            passed, reason = False, f"attempt failed: {outcome.error}"[:300]
        else:
            try:
                passed, reason = REGISTRY[check.type](check, outcome)
            except Exception as exc:  # a bug in a check must not lose the attempt
                passed, reason = False, f"check crashed: {type(exc).__name__}: {exc}"
        results.append(
            {
                "type": check.type,
                "describe": check.describe(),
                "weight": check.weight,
                "passed": bool(passed),
                "reason": reason,
            }
        )
    return results


def score(results: list[dict[str, Any]]) -> float:
    """Weighted share of passed checks, 0-1."""
    total = sum(float(r["weight"]) for r in results)
    if total <= 0:
        return 0.0
    return sum(float(r["weight"]) for r in results if r["passed"]) / total
