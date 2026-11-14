"""Parse the model's report: a JSON header followed by Markdown.

Lenient on purpose (weak models): fenced or bare object, trailing commas, wrong-case keys
and values, scores as "85/100". When no usable header is found the rule-based flags
provide health, score and findings, and the whole text becomes the Markdown.
"""

from __future__ import annotations

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
    for line in markdown.splitlines():
        line = line.strip().lstrip("#*-> ").strip()
        if len(line) > 3 and not line.lower().startswith(("overview", "findings")):
            return line[:200]
    return ""


def parse_report(text: str, flags: list[Flag]) -> ParsedReport:
    text = strip_reasoning(text).strip()
    found = _find_object(text)
    if found is None:
        return ParsedReport(
            health=worst_severity(flags),
            score=score_from_flags(flags),
            summary=_first_sentence(text),
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
    summary = str(low.get("summary") or "").strip() or _first_sentence(markdown)
    return ParsedReport(
        health=health,
        score=score,
        summary=summary[:300],
        findings=findings,
        markdown=markdown,
        parsed=True,
        notes=notes,
    )
