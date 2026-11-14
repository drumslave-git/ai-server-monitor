"""Shared types and formatting helpers for the digester."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

SEVERITIES = ("info", "warn", "crit")
SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class Flag:
    """One deterministic finding produced by the anomaly rules."""

    severity: str  # info | warn | crit
    title: str
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "title": self.title, "detail": self.detail}


def worst_severity(flags: list[Flag] | list[dict[str, Any]]) -> str:
    """'ok' when there are no flags above info, else warn/crit. (info never degrades health.)"""
    worst = "ok"
    for flag in flags:
        sev = flag.severity if isinstance(flag, Flag) else str(flag.get("severity"))
        if sev == "crit":
            return "crit"
        if sev == "warn":
            worst = "warn"
    return worst


def score_from_flags(flags: list[Flag] | list[dict[str, Any]]) -> int:
    """Deterministic 0-100 health score: crit < 50, warn < 90, info barely counts."""
    counts = {"crit": 0, "warn": 0, "info": 0}
    for flag in flags:
        sev = flag.severity if isinstance(flag, Flag) else str(flag.get("severity"))
        if sev in counts:
            counts[sev] += 1
    score = 100 - 35 * counts["crit"] - min(40, 8 * counts["warn"]) - min(5, counts["info"])
    if counts["crit"]:
        score = min(score, 49)
    elif counts["warn"]:
        score = min(score, 89)
    return max(0, min(100, score))


# --------------------------------------------------------------------------- formatting


def fmt_bytes(value: float | int | None) -> str:
    if value is None:
        return "?"
    size = float(value)
    sign = "-" if size < 0 else ""
    size = abs(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            if unit == "B":
                return f"{sign}{size:.0f}B"
            return f"{sign}{size:.1f}{unit}" if size < 100 else f"{sign}{size:.0f}{unit}"
        size /= 1000
    return f"{sign}{size:.0f}TB"  # pragma: no cover


def fmt_rate(value: float | int | None) -> str:
    return "?" if value is None else fmt_bytes(value) + "/s"


def fmt_pct(value: float | int | None) -> str:
    return "?" if value is None else f"{value:.0f}%" if value >= 10 else f"{value:.1f}%"


def fmt_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def num(value: Any, default: float = 0.0) -> float:
    """Defensive float(): collectors may hand us None or odd types."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
