"""A short plain-language reason for every scenario score, built from the recorded checks.

No model is involved: for each scenario the failed checks of its attempts are counted (a check
is named by its plain-English label from `check_labels`, else by its type), plus the
attempts that errored or gave no answer. The Compare page and its PDF show the text under the
numbers: "missed: names the restart count (2 of 3 attempts)".
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from .check_labels import CHECK_LABELS, label_for_type

MAX_MISSED = 2
"""Missed checks named in a cell; the rest is "+N more"."""
MAX_ERROR = 48


def _short(text: str, limit: int) -> str:
    """One line of at most `limit` characters; a list ("a, b, c") is cut between its items."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if ", " in cut:
        cut = cut[: cut.rindex(", ")]
    return cut.rstrip(" ,;:") + "…"


def check_labels(scenarios: Iterable[Any]) -> dict[str, dict[str, str]]:
    """scenario id -> {check description -> plain-English label}, for the installed scenarios.

    The label is taken from `CHECK_LABELS` by the check's position, so a run recorded before a
    label was written reads the same as a new one; a scenario whose checks no longer line up with
    its labels (a check added or changed) gets none, and its checks read by type."""
    out: dict[str, dict[str, str]] = {}
    for s in scenarios:
        entries = CHECK_LABELS.get(s.id)
        checks = s.public()["checks"]
        if entries is None or len(entries) != len(checks):
            continue
        out[s.id] = {
            c["describe"]: label
            for c, (kind, label) in zip(checks, entries, strict=True)
            if kind == c["type"] and c["describe"]
        }
    return out


def _label(check: dict[str, Any], labels: dict[str, str]) -> str:
    """The reader's name of a check: its label, else one by type (never the raw description)."""
    return labels.get(check.get("describe") or "") or label_for_type(check.get("type") or "")


def _plural(n: int, one: str, many: str) -> str:
    return one if n == 1 else many


def explain_scenario(
    attempts: list[dict[str, Any]], labels: dict[str, str] | None = None
) -> dict[str, Any]:
    """The explanation of one scenario for one run: `lines` (at most two short sentences),
    `ok` (every attempt passed every check) and the raw counts."""
    labels = labels or {}
    n = len(attempts)
    missed: Counter[str] = Counter()
    weight: dict[str, float] = {}
    errored = empty = limit = judged = 0
    reasons: Counter[str] = Counter()
    partial = 0
    for a in attempts:
        if a.get("status") != "ok":
            errored += 1
            if a.get("error"):
                reasons[_short(a["error"], MAX_ERROR)] += 1
        issue = a.get("answer_issue")
        if issue == "empty":
            empty += 1
        elif issue == "limit":
            limit += 1
        failed = [c for c in (a.get("checks") or []) if not c.get("passed")]
        seen: set[str] = set()
        for c in failed:
            label = _label(c, labels)
            if label in seen:
                continue
            seen.add(label)
            missed[label] += 1
            weight[label] = max(weight.get(label, 0.0), float(c.get("weight") or 0))
        if not failed and a.get("status") == "ok" and (a.get("score") or 0) < 0.999:
            partial += 1
            if a.get("judge_score") is not None:
                judged += 1

    lines: list[str] = []
    if missed:
        ranked = sorted(missed, key=lambda k: (-weight[k], -missed[k], k))
        shown = ranked[:MAX_MISSED]
        words = " attempts" if len(shown) == 1 and len(ranked) == 1 else ""
        parts = [f"{k} ({missed[k]} of {n}{words})" for k in shown]
        more = len(ranked) - len(shown)
        lines.append("missed: " + "; ".join(parts) + (f"; +{more} more" if more else ""))
    if partial:
        lines.append(
            f"{partial} of {n} attempts lost points on the judge's rating"
            if judged == partial
            else f"{partial} of {n} attempts scored under 100 %"
        )
    problems: list[str] = []
    if errored:
        why = [r for r, _ in reasons.most_common(1)]
        what = f"{errored} {_plural(errored, 'attempt', 'attempts')} errored"
        problems.append(what + (f": {why[0]}" if why else ""))
    if empty:
        problems.append(f"{empty} empty {_plural(empty, 'answer', 'answers')}")
    if limit:
        problems.append(f"{limit} cut off by the output limit")
    if problems:
        lines.append("; ".join(problems))
    ok = not lines
    return {
        "ok": ok,
        "lines": ["all checks passed"] if ok else lines,
        "attempts": n,
        "errored": errored,
        "empty": empty,
        "limit": limit,
    }


def explain_run(
    attempts: Iterable[dict[str, Any]], labels: dict[str, dict[str, str]] | None = None
) -> dict[str, dict[str, Any]]:
    """scenario id -> explanation, over every attempt of a run (all thinking levels)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for a in attempts:
        by.setdefault(a["scenario_id"], []).append(a)
    labels = labels or {}
    return {sid: explain_scenario(rows, labels.get(sid)) for sid, rows in by.items()}
