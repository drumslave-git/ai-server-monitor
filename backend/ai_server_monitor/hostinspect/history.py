"""Trend of one metric from the stored `collector_samples` history."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from .output import human

MAX_POINTS = 24
MAX_ROWS = 400
BYTE_NAMES = ("bytes", "used", "avail", "free", "size", "total", "usage", "rss", "cached")


class MetricError(ValueError):
    pass


def parse_where(where: str | None) -> tuple[str, str] | None:
    if not where:
        return None
    key, sep, value = where.partition("=")
    if not sep or not key:
        raise MetricError("`where` must look like key=value, e.g. mount=/mnt/disks/disk14TB")
    return key.strip(), value.strip()


def _pick(items: list[Any], where: tuple[str, str] | None) -> Any:
    if where is None:
        raise MetricError("the metric is inside a list: pass `where` as key=value to pick one item")
    key, wanted = where
    return next((i for i in items if isinstance(i, dict) and str(i.get(key)) == wanted), None)


def extract(data: Any, metric: str, where: tuple[str, str] | None) -> float | None:
    """`metric` is a dotted path (e.g. `filesystems.used`); a list on the way is resolved
    with `where` (e.g. mount=/). Returns a number or None when it is not there."""
    node = data
    for part in (p for p in metric.split(".") if p):
        if isinstance(node, list):
            node = _pick(node, where)
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    if isinstance(node, list):
        node = _pick(node, where)
    if isinstance(node, bool) or not isinstance(node, int | float):
        return None
    return float(node)


def _fmt(metric: str, value: float) -> str:
    leaf = metric.rsplit(".", 1)[-1].lower()
    if any(name in leaf for name in BYTE_NAMES) and "percent" not in leaf:
        return human(value)
    return f"{value:.1f}".rstrip("0").rstrip(".") or "0"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M")


def build(points: list[tuple[float, float]], metric: str) -> dict[str, Any]:
    if not points:
        return {"samples": 0, "note": "no samples with that metric in the period"}
    first, last = points[0], points[-1]
    values = [v for _, v in points]
    step = max(len(points) // MAX_POINTS, 1)
    shown = points[::step]
    if shown[-1] != last:
        shown.append(last)
    span_h = (last[0] - first[0]) / 3600
    delta = last[1] - first[1]
    out: dict[str, Any] = {
        "samples": len(points),
        "first": f"{_fmt(metric, first[1])} at {_iso(first[0])} UTC",
        "last": f"{_fmt(metric, last[1])} at {_iso(last[0])} UTC",
        "min": _fmt(metric, min(values)),
        "max": _fmt(metric, max(values)),
        "series": [f"{_iso(ts)} {_fmt(metric, v)}" for ts, v in shown],
    }
    if span_h >= 0.5:
        per_day = delta / span_h * 24
        verb = "Grew" if delta > 0 else "Shrank" if delta < 0 else "Did not change"
        out["trend"] = (
            f"{verb}"
            + (f" by {_fmt(metric, abs(delta))}" if delta else "")
            + f" over {span_h / 24:.1f} days"
            + (f" (about {_fmt(metric, abs(per_day))} per day)" if delta else "")
            + "."
        )
    else:
        out["trend"] = "Not enough history to say."
    return out


async def metric_history(
    db: Any,
    collector: str,
    metric: str,
    where: str | None,
    since_hours: float,
    now: float | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    start = now - since_hours * 3600
    bucket = max(since_hours * 3600 / MAX_ROWS, 60.0)
    rows = await db.fetchall(
        "SELECT ts, data FROM collector_samples WHERE id IN (SELECT MIN(id) FROM "
        "collector_samples WHERE collector = ? AND ok = 1 AND ts >= ? "
        "GROUP BY CAST(ts / ? AS INTEGER)) ORDER BY ts",
        (collector, start, bucket),
    )
    selector = parse_where(where)
    points: list[tuple[float, float]] = []
    for row in rows:
        try:
            data = json.loads(row["data"])
        except ValueError:
            continue
        value = extract(data, metric, selector)
        if value is not None:
            points.append((row["ts"], value))
    head: dict[str, Any] = {"collector": collector, "metric": metric}
    if where:
        head["where"] = where
    return {**head, "period_hours": since_hours, **build(points, metric)}
