"""Prometheus data in the digest: result lines for the LLM and deterministic flags.

Down targets, firing alerts and threshold breaches become flags without any model. Results
of scrapes of another machine (see promauto.hosts) are labelled "other host <address>" so
the report never mixes them up with the monitored server.
"""

from __future__ import annotations

from typing import Any

from ..promauto.hosts import instance_host, is_other_host
from ..thresholds import breaches
from .models import Flag, fmt_bytes, fmt_duration, fmt_rate, num

MAX_SERIES_SHOWN = 3
MAX_QUERY_LINES = 14
MAX_ALERT_LINES = 5
MAX_DOWN_LINES = 5
FLAG_PREFIX = "Metric "
"""Value lines start with this; the report fingerprint ignores them (values always wiggle)."""


def format_value(value: Any, unit: str = "") -> str:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return str(value)
    u = unit.strip().lower()
    if u in ("%", "percent"):
        return f"{value:.1f}%" if abs(value) < 10 else f"{value:.0f}%"
    if u == "bytes":
        return fmt_bytes(value)
    if u in ("bytes/s", "bytes_per_s", "bps"):
        return fmt_rate(value)
    if u in ("seconds", "s"):
        return f"{value:.3g}s" if abs(value) < 60 else fmt_duration(value)
    if u == "celsius":
        return f"{value:.0f}°C"
    text = f"{value:.4g}"
    return f"{text} {unit}".strip()


def series_tag(labels: dict[str, Any], job: str = "") -> str:
    """Short description of which series this is, e.g. 'other host 192.168.88.30 C:'."""
    instance = str(labels.get("instance") or "")
    series_job = str(labels.get("job") or job)
    rest = [
        str(v)[:40]
        for k, v in labels.items()
        if k not in ("instance", "job", "__name__") and str(v)
    ][:2]
    parts: list[str] = []
    if is_other_host(instance, series_job):
        parts.append(f"other host {instance_host(instance) or series_job}")
    elif instance and not rest:
        parts.append(instance_host(instance))
    parts.extend(rest)
    return " ".join(p for p in parts if p)


def _numeric(series: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in series if isinstance(s.get("value"), int | float)]


def _breach_level(q: dict[str, Any], value: Any) -> str | None:
    if breaches(q.get("crit_if"), value):
        return "crit"
    if breaches(q.get("warn_if"), value):
        return "warn"
    return None


def _ok_queries(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in data.get("queries") or [] if isinstance(q, dict) and q.get("ok")]


def _query_line(q: dict[str, Any]) -> str | None:
    series = [s for s in q.get("series") or [] if isinstance(s, dict)]
    if not series:
        return None
    unit = str(q.get("unit") or "")
    job = str(q.get("job") or "")
    ranked = sorted(
        series,
        key=lambda s: (
            _breach_level(q, s.get("value")) is None,
            -num(s.get("value")) if isinstance(s.get("value"), int | float) else 0,
        ),
    )
    shown = []
    for s in ranked[:MAX_SERIES_SHOWN]:
        tag = series_tag(s.get("labels") or {}, job)
        value = format_value(s.get("value"), unit)
        shown.append(f"{tag} {value}".strip())
    more = int(num(q.get("total"), len(series))) - len(shown)
    text = (
        f"{FLAG_PREFIX}{q.get('name')}: " + ", ".join(shown) + (f" (+{more} more)" if more else "")
    )
    description = str(q.get("description") or "").strip()
    return f"{text} — {_short(description)}" if description else text


def _short(text: str, limit: int = 100) -> str:
    """Cut at a word boundary."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:.") + "…"


def digest_lines(data: dict[str, Any]) -> list[str]:
    """The Prometheus section body; empty when there is nothing worth showing."""
    lines: list[str] = []
    targets = data.get("targets")
    if isinstance(targets, dict) and targets.get("total"):
        line = f"Scrape targets: {targets.get('up', 0)} of {targets['total']} up"
        other = targets.get("other_hosts") or {}
        if other:
            line += "; other hosts scraped: " + ", ".join(f"{j} ({a})" for j, a in other.items())
        lines.append(line)
        for t in (targets.get("down") or [])[:MAX_DOWN_LINES]:
            err = f" — {t['error']}" if t.get("error") else ""
            lines.append(f"Target DOWN: {t.get('job')} {t.get('instance')}{err}")
    alerts = data.get("alerts")
    if isinstance(alerts, dict):
        for a in (alerts.get("firing") or [])[:MAX_ALERT_LINES]:
            sev = f" [{a['severity']}]" if a.get("severity") else ""
            where = f" on {a['instance']}" if a.get("instance") else ""
            why = f" — {a['summary']}" if a.get("summary") else ""
            lines.append(f"ALERT firing: {a.get('name')}{sev}{where}{why}")
        rest = int(num(alerts.get("firing_count"))) - min(
            len(alerts.get("firing") or []), MAX_ALERT_LINES
        )
        if rest > 0:
            lines.append(f"(+{rest} more alerts firing)")
        if num(alerts.get("pending_count")):
            lines.append(f"{int(num(alerts['pending_count']))} alert(s) pending (not firing yet)")
    quiet: list[str] = []
    shown = 0
    for q in _ok_queries(data):
        line = _query_line(q)
        if line is None:
            if q.get("kind") == "alert":
                quiet.append(str(q.get("name")))
            else:
                lines.append(f"{FLAG_PREFIX}{q.get('name')}: no data")
            continue
        if shown < MAX_QUERY_LINES:
            lines.append(line)
            shown += 1
    if quiet:
        lines.append("Checks with nothing to report: " + ", ".join(quiet[:8]))
    failing = [
        f"{q.get('name')} ({str(q.get('error'))[:60]})"
        for q in data.get("queries") or []
        if isinstance(q, dict) and not q.get("ok")
    ]
    if failing:
        lines.append(f"{len(failing)} query(ies) failing: " + "; ".join(failing[:4]))
    return lines


# --------------------------------------------------------------------------- flags

_ALERT_SEVERITY = {"critical": "crit", "page": "crit", "error": "warn", "warning": "warn"}


def flags(data: dict[str, Any]) -> list[Flag]:
    out: list[Flag] = []
    targets = data.get("targets")
    if isinstance(targets, dict) and targets.get("down"):
        everything = targets.get("up", 0) == 0 and targets.get("total", 0) > 0
        for t in targets["down"]:
            tag = "other host" if is_other_host(str(t.get("instance")), str(t.get("job"))) else ""
            out.append(
                Flag(
                    "crit" if everything else "warn",
                    f"Prometheus target down: {t.get('job')} ({t.get('instance')})",
                    " ".join(x for x in (tag, str(t.get("error") or "")) if x),
                )
            )
    alerts = data.get("alerts")
    if isinstance(alerts, dict):
        for a in alerts.get("firing") or []:
            sev = _ALERT_SEVERITY.get(str(a.get("severity")).lower(), "warn")
            if str(a.get("severity")).lower() == "info":
                sev = "info"
            where = f" on {a['instance']}" if a.get("instance") else ""
            out.append(
                Flag(sev, f"Prometheus alert firing: {a.get('name')}{where}", str(a.get("summary")))
            )
    for q in _ok_queries(data):
        out.extend(_query_flags(q))
    return out


def _query_flags(q: dict[str, Any]) -> list[Flag]:
    series = _numeric([s for s in q.get("series") or [] if isinstance(s, dict)])
    unit, job = str(q.get("unit") or ""), str(q.get("job") or "")
    title_base = str(q.get("description") or q.get("name"))
    levels = [(s, _breach_level(q, s["value"])) for s in series]
    hits = [(s, lvl) for s, lvl in levels if lvl]
    if hits:
        worst = "crit" if any(lvl == "crit" for _, lvl in hits) else "warn"
        top = max(hits, key=lambda h: (h[1] == "crit", abs(h[0]["value"])))[0]
        tag = series_tag(top.get("labels") or {}, job)
        threshold = q.get("crit_if") if worst == "crit" else q.get("warn_if")
        detail = f"{q.get('name')} = {format_value(top['value'], unit)} (limit {threshold})"
        if tag:
            detail += f", {tag}"
        if len(hits) > 1:
            detail += f"; {len(hits)} series affected"
        return [Flag(worst, title_base[:100], detail)]
    if q.get("kind") == "alert" and series and not (q.get("warn_if") or q.get("crit_if")):
        top = series[0]
        tag = series_tag(top.get("labels") or {}, job)
        extra = f" ({tag})" if tag else ""
        return [
            Flag(
                "warn",
                title_base[:100],
                f"{q.get('name')}: {len(series)} series match{extra}, e.g. "
                f"{format_value(top['value'], unit)}",
            )
        ]
    return []
