"""Parse the model's report: a JSON header followed by Markdown.

Lenient on purpose (weak models): fenced or bare object, trailing commas, wrong-case keys
and values, scores as "85/100". When no usable header is found the rule-based flags
provide health, score and findings, and the whole text becomes the Markdown.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..digest.models import Flag, score_from_flags, worst_severity
from ..llm.text import strip_reasoning

_FENCE = re.compile(r"```[ \t]*(?:json|JSON|jsonc)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")
_HEALTH_WORDS = {
    "ok": "ok",
    "okay": "ok",
    "good": "ok",
    "healthy": "ok",
    "green": "ok",
    "info": "ok",
    "warn": "warn",
    "warning": "warn",
    "degraded": "warn",
    "yellow": "warn",
    "crit": "crit",
    "critical": "crit",
    "bad": "crit",
    "red": "crit",
    "error": "crit",
    "fail": "crit",
}
_SEVERITY_WORDS = {
    **_HEALTH_WORDS,
    "info": "info",
    "information": "info",
    "notice": "info",
    "low": "info",
    "high": "crit",
    "medium": "warn",
}


@dataclass
class ParsedReport:
    health: str
    score: int
    summary: str
    findings: list[dict[str, str]]
    markdown: str
    parsed: bool
    """True when the model's JSON header was understood; False = rule-based fallback."""
    notes: list[str] = field(default_factory=list)


def _loads(text: str) -> dict[str, Any] | None:
    for candidate in (text, _TRAILING_COMMA.sub(r"\1", text)):
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _find_object(text: str) -> tuple[dict[str, Any], int, int] | None:
    """The header object and its (start, end) span in `text`: a fenced block first,
    otherwise the first '{...}' that decodes (with trailing commas tolerated)."""
    for match in _FENCE.finditer(text):
        obj = _loads(match.group(1).strip())
        if obj is None:
            # Fenced but not clean JSON: look for an object inside the fence.
            found = _scan_objects(match.group(1))
            obj = found[0] if found else None
        if obj is not None:
            return obj, match.start(), match.end()
    found = _scan_objects(text)
    if found:
        obj, start, end = found
        return obj, start, end
    return None


def _scan_objects(text: str) -> tuple[dict[str, Any], int, int] | None:
    for match in re.finditer(r"\{", text):
        end = _balanced_end(text, match.start())
        if end is None:
            continue
        obj = _loads(text[match.start() : end])
        if obj is not None and any(
            str(k).lower() in ("health", "score", "findings", "summary") for k in obj
        ):
            return obj, match.start(), end
    return None


def _balanced_end(text: str, start: int) -> int | None:
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None


def _norm_health(value: Any) -> str | None:
    if isinstance(value, str):
        return _HEALTH_WORDS.get(value.strip().lower())
    return None


def _norm_severity(value: Any) -> str:
    if isinstance(value, str):
        sev = _SEVERITY_WORDS.get(value.strip().lower(), "info")
        return "info" if sev == "ok" else sev
    return "info"


def _norm_score(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            return None
        number = float(match.group(0))
    else:
        return None
    if 0 < number <= 1 and not float(number).is_integer():
        number *= 100  # 0.85 -> 85
    return max(0, min(100, round(number)))


def _norm_findings(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append({"severity": "info", "title": item.strip()[:200], "detail": ""})
        elif isinstance(item, dict):
            low = {str(k).lower(): v for k, v in item.items()}
            title = str(low.get("title") or low.get("name") or low.get("issue") or "").strip()
            detail = str(
                low.get("detail") or low.get("details") or low.get("description") or ""
            ).strip()
            if not title and not detail:
                continue
            out.append(
                {
                    "severity": _norm_severity(low.get("severity") or low.get("level")),
                    "title": (title or detail)[:200],
                    "detail": detail if title else "",
                }
            )
    return out[:20]


def _first_sentence(markdown: str) -> str:
    """First real prose line of the text; never a code fence or a line of JSON."""
    in_fence = False
    for raw in markdown.splitlines():
        if raw.strip().startswith("```"):
            in_fence = not in_fence
            continue
        line = raw.strip().lstrip("#*-> ").strip()
        if in_fence or len(line) <= 3 or line.lower().startswith(("overview", "findings")):
            continue
        if line[0] in "{}[]" or re.match(r'^"[\w ]+"\s*:', line):
            continue  # a JSON line (truncated header), not a sentence
        return line[:200]
    return ""


def _generated_summary(health: str, flags: list[Flag] | list[dict[str, Any]]) -> str:
    """A one-line summary built from the rule flags, for when the text has none."""
    titles = [
        str(f.title if isinstance(f, Flag) else f.get("title") or "")
        for f in flags
        if (f.severity if isinstance(f, Flag) else f.get("severity")) in ("crit", "warn")
    ]
    titles = [t for t in titles if t]
    if not titles:
        return {
            "ok": "No problems found.",
            "warn": "Warnings found.",
            "crit": "Critical problems.",
        }[health]
    extra = f" (+{len(titles) - 1} more)" if len(titles) > 1 else ""
    return f"{health.upper()}: {titles[0]}{extra}"[:300]


_KEY = r'"{}"\s*:\s*'
_TRUNCATED_NOTE = (
    "_The model's answer was cut off (output token limit). "
    "Raise Settings → LLM → maximum output tokens._"
)


def _salvage_truncated(text: str) -> tuple[dict[str, Any], int] | None:
    """Pull health, score, summary and every complete finding out of a header object that
    was cut off mid-way (the model hit its output-token limit). Returns (object, start)."""
    start = text.find("{")
    if start < 0:
        return None
    frag = text[start:]
    obj: dict[str, Any] = {}
    if m := re.search(_KEY.format("health") + r'"([^"\\]*)"', frag, re.IGNORECASE):
        obj["health"] = m.group(1)
    score_pat = _KEY.format("score") + r'("[^"]*"|-?\d+(?:\.\d+)?)(?=\s*[,}\]\n])'
    if m := re.search(score_pat, frag, re.IGNORECASE):
        with contextlib.suppress(ValueError):
            obj["score"] = json.loads(m.group(1))
    if m := re.search(_KEY.format("summary") + r'("(?:[^"\\]|\\.)*")', frag, re.IGNORECASE):
        with contextlib.suppress(ValueError):
            obj["summary"] = json.loads(m.group(1))
    if m := re.search(_KEY.format("findings") + r"\[", frag, re.IGNORECASE):
        items: list[Any] = []
        pos = m.end()
        while True:
            brace = frag.find("{", pos)
            if brace < 0:
                break
            end = _balanced_end(frag, brace)
            if end is None:
                break  # the item that was cut off
            item = _loads(frag[brace:end])
            if item is not None:
                items.append(item)
            pos = end
        obj["findings"] = items
    return (
        (obj, start) if any(k in obj for k in ("health", "score", "summary", "findings")) else None
    )


OK_SCORE_FLOOR = 90


def _agree_with_flags(
    health: str, score: int, findings: list[dict[str, str]], flags: list[Flag], notes: list[str]
) -> tuple[str, int]:
    """The digest's rules are the authority on what needs attention: a model that says `warn`
    while the digest has no [WARN]/[CRIT] flag (only [INFO] notes such as kernel audit lines)
    has over-read the notes, so the health stays `ok`. `crit` is never lowered."""
    only_info = bool(flags) and worst_severity(flags) == "ok"
    if health != "warn" or not only_info:
        return health, score
    for finding in findings:
        if finding.get("severity") == "warn":
            finding["severity"] = "info"
    notes.append("model said warn, but the digest has only info flags: health kept ok")
    return "ok", max(score, OK_SCORE_FLOOR)


def parse_report(text: str, flags: list[Flag]) -> ParsedReport:
    text = strip_reasoning(text).strip()
    found = _find_object(text)
    if found is None:
        salvaged = _salvage_truncated(text)
        if salvaged is not None:
            return _from_truncated(salvaged[0], text[: salvaged[1]], flags)
        health = worst_severity(flags)
        return ParsedReport(
            health=health,
            score=score_from_flags(flags),
            summary=_first_sentence(text) or _generated_summary(health, flags),
            findings=[f.to_dict() for f in flags if f.severity != "info" or len(flags) <= 5],
            markdown=text,
            parsed=False,
            notes=["no JSON header found"],
        )
    obj, start, end = found
    markdown = (text[:start] + text[end:]).strip()
    markdown = re.sub(r"^\s*```(?:json)?\s*```\s*", "", markdown).strip()
    low = {str(k).lower(): v for k, v in obj.items()}
    findings = _norm_findings(low.get("findings"))
    notes: list[str] = []

    health = _norm_health(low.get("health") or low.get("status"))
    if health is None:
        health = worst_severity(findings) if findings else worst_severity(flags)
        notes.append("health missing or invalid; derived")
    score = _norm_score(low.get("score"))
    if score is None:
        score = score_from_flags(findings or flags)
        notes.append("score missing or invalid; derived")
    health, score = _agree_with_flags(health, score, findings, flags, notes)
    summary = (
        str(low.get("summary") or "").strip()
        or _first_sentence(markdown)
        or _generated_summary(health, findings or flags)
    )
    return ParsedReport(
        health=health,
        score=score,
        summary=summary[:300],
        findings=findings,
        markdown=markdown,
        parsed=True,
        notes=notes,
    )


def _from_truncated(obj: dict[str, Any], before: str, flags: list[Flag]) -> ParsedReport:
    """A report whose JSON header was cut off: keep what survived, fill the rest from the
    rule flags, and say so in the Markdown."""
    findings = _norm_findings(obj.get("findings"))
    notes = ["header truncated (output token limit); salvaged"]
    health = _norm_health(obj.get("health"))
    if health is None:
        health = worst_severity(findings) if findings else worst_severity(flags)
        notes.append("health missing; derived")
    score = _norm_score(obj.get("score"))
    if score is None:
        score = score_from_flags(findings or flags)
        notes.append("score missing; derived")
    health, score = _agree_with_flags(health, score, findings, flags, notes)
    summary = str(obj.get("summary") or "").strip() or _generated_summary(health, findings or flags)
    lines = ["## Overview", summary, ""]
    if findings:
        lines += ["## Findings", *(f"- **{f['title']}** {f['detail']}".rstrip() for f in findings)]
        lines.append("")
    lines.append(_TRUNCATED_NOTE)
    prose = re.sub(r"```[a-zA-Z]*\s*$", "", before.rstrip()).strip()
    markdown = (prose + "\n\n" if prose else "") + "\n".join(lines)
    return ParsedReport(
        health=health,
        score=score,
        summary=summary[:300],
        findings=findings,
        markdown=markdown,
        parsed=True,
        notes=notes,
    )
