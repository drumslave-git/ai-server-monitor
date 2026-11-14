"""Trends from stored `collector_samples` and a small in-memory journal window log.

Queries are downsampled in SQL (one sample per hour) so a 7-day window costs ~170 rows,
not ~2000, every time a report is built.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ..db import Database
from .models import num

log = logging.getLogger(__name__)

_DAY = 86400.0
MIN_TREND_SPAN_S = 6 * 3600
MIN_GROWTH_PER_DAY = 50e6
"""Growth below ~50 MB/day is noise and never produces an ETA."""


# --------------------------------------------------------------------------- disks


@dataclass
class DiskTrend:
    mount: str
    growth_per_day: float  # bytes/day, least-squares over the window (negative = shrinking)
    span_days: float
    change_24h: float | None = None
    eta_days: float | None = None  # days until full at the current rate, None = not filling

    def to_dict(self) -> dict[str, Any]:
        return {
            "mount": self.mount,
            "growth_per_day": self.growth_per_day,
            "span_days": self.span_days,
            "change_24h": self.change_24h,
            "eta_days": self.eta_days,
        }


def _regression_slope(points: list[tuple[float, float]]) -> float:
    """Least-squares slope (y per x unit); 0 when degenerate."""
    n = len(points)
    mean_x = sum(p[0] for p in points) / n
    mean_y = sum(p[1] for p in points) / n
    den = sum((x - mean_x) ** 2 for x, _ in points)
    if den == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / den


def disk_trend_from_points(
    mount: str, points: list[tuple[float, float]], avail: float | None, now: float
) -> DiskTrend | None:
    """points: (ts, used_bytes) oldest first."""
    if len(points) < 3 or points[-1][0] - points[0][0] < MIN_TREND_SPAN_S:
        return None
    slope_per_s = _regression_slope(points)
    per_day = slope_per_s * _DAY
    change_24h: float | None = None
    cutoff = points[-1][0] - _DAY
    older = [p for p in points if p[0] <= cutoff]
    if older:
        change_24h = points[-1][1] - older[-1][1]
    eta = None
    if per_day >= MIN_GROWTH_PER_DAY and avail is not None and avail > 0:
        eta = avail / per_day
    return DiskTrend(
        mount=mount,
        growth_per_day=per_day,
        span_days=(points[-1][0] - points[0][0]) / _DAY,
        change_24h=change_24h,
        eta_days=eta,
    )


async def disk_trends(
    db: Database, filesystems: list[dict[str, Any]], window_s: float, now: float
) -> dict[str, DiskTrend]:
    rows = await db.fetchall(
        "SELECT ts, json_extract(data, '$.filesystems') AS fs FROM collector_samples "
        "WHERE id IN (SELECT MIN(id) FROM collector_samples WHERE collector = 'system' "
        "AND ok = 1 AND ts >= ? GROUP BY CAST(ts / 3600 AS INTEGER)) ORDER BY ts",
        (now - window_s,),
    )
    series: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        try:
            fs_list = json.loads(row["fs"] or "[]")
        except ValueError:
            continue
        for fs in fs_list if isinstance(fs_list, list) else []:
            if isinstance(fs, dict) and fs.get("mount") and fs.get("used") is not None:
                series.setdefault(fs["mount"], []).append((row["ts"], num(fs["used"])))
    out: dict[str, DiskTrend] = {}
    for fs in filesystems:
        mount = fs.get("mount")
        points = series.get(mount, [])
        # Append the live value so the trend ends "now".
        if fs.get("used") is not None:
            points = [*points, (now, num(fs["used"]))]
        avail = fs.get("avail")
        trend = disk_trend_from_points(mount, points, None if avail is None else num(avail), now)
        if trend:
            out[mount] = trend
    return out


# --------------------------------------------------------------------------- restarts


async def _sample_at_or_after(
    db: Database, collector: str, ts: float, max_age: float
) -> tuple[float, dict[str, Any]] | None:
    row = await db.fetchone(
        "SELECT ts, data FROM collector_samples WHERE collector = ? AND ok = 1 "
        "AND ts >= ? AND ts <= ? ORDER BY ts LIMIT 1",
        (collector, ts, ts + max_age),
    )
    if row is None:
        return None
    try:
        data = json.loads(row["data"])
    except ValueError:
        return None
    return (row["ts"], data) if isinstance(data, dict) else None


def _delta(current: int, base: int | None) -> int:
    if base is None:
        return 0
    return current - base if current >= base else current  # recreated container/unit: reset


async def docker_restarts_24h(
    db: Database, containers: list[dict[str, Any]], now: float
) -> dict[str, int]:
    """Restarts per container over (about) the last 24 h, from `restart_count`."""
    base = await _sample_at_or_after(db, "docker", now - _DAY, 6 * 3600)
    if base is None:
        return {}
    before = {c.get("name"): c.get("restart_count") for c in base[1].get("containers", [])}
    out: dict[str, int] = {}
    for c in containers:
        name = c.get("name")
        if name in before and before[name] is not None:
            n = _delta(int(num(c.get("restart_count"))), int(num(before[name])))
            if n:
                out[name] = n
    return out


async def unit_restarts_24h(
    db: Database, units: list[dict[str, Any]], now: float
) -> dict[str, int]:
    base = await _sample_at_or_after(db, "systemd", now - _DAY, 6 * 3600)
    if base is None:
        return {}
    before = {u.get("name"): u.get("n_restarts") for u in base[1].get("restarting", [])}
    out: dict[str, int] = {}
    for u in units:
        name = u.get("name")
        cur = int(num(u.get("n_restarts")))
        n = _delta(cur, int(num(before[name]))) if before.get(name) is not None else 0
        if n:
            out[name] = n
    return out


@dataclass
class Trends:
    disks: dict[str, DiskTrend] = field(default_factory=dict)
    docker_restarts_24h: dict[str, int] = field(default_factory=dict)
    unit_restarts_24h: dict[str, int] = field(default_factory=dict)


async def compute_trends(
    db: Database, system: dict[str, Any], docker: dict[str, Any], systemd: dict[str, Any],
    window_s: float, now: float,
) -> Trends:  # fmt: skip
    """Never raises: a broken history must not stop the digest."""
    trends = Trends()
    try:
        trends.disks = await disk_trends(db, system.get("filesystems") or [], window_s, now)
        trends.docker_restarts_24h = await docker_restarts_24h(
            db, docker.get("containers") or [], now
        )
        trends.unit_restarts_24h = await unit_restarts_24h(db, systemd.get("restarting") or [], now)
    except Exception:
        log.exception("computing trends failed")
    return trends


# --------------------------------------------------------------------------- journal windows


@dataclass
class JournalSummary:
    """Journal activity over a period, summed over the collector's windows."""

    seconds: float = 0.0
    entries: int = 0
    by_priority: dict[str, int] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)  # merged top messages
    notable: dict[str, dict[str, Any]] = field(default_factory=dict)
    windows: int = 0

    @property
    def errors(self) -> int:
        """Entries at priority err or worse."""
        return sum(self.by_priority.get(p, 0) for p in ("emerg", "alert", "crit", "err"))


_ERR_PRIOS = {"emerg": 0, "alert": 1, "crit": 2, "err": 3, "warning": 4, "notice": 5, "info": 6}


def summarize_windows(windows: list[dict[str, Any]]) -> JournalSummary:
    summary = JournalSummary(windows=len(windows))
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for w in windows:
        summary.seconds += num(w.get("window_end")) - num(w.get("window_start"))
        summary.entries += int(num(w.get("entries")))
        for prio, count in (w.get("by_priority") or {}).items():
            summary.by_priority[prio] = summary.by_priority.get(prio, 0) + int(num(count))
        for m in w.get("top_messages") or []:
            key = (str(m.get("source")), str(m.get("sample"))[:80])
            cur = merged.get(key)
            if cur is None:
                merged[key] = dict(m)
            else:
                cur["count"] = int(num(cur.get("count"))) + int(num(m.get("count")))
                if _ERR_PRIOS.get(str(m.get("priority")), 9) < _ERR_PRIOS.get(
                    str(cur.get("priority")), 9
                ):
                    cur["priority"] = m.get("priority")
        for name, hit in (w.get("notable") or {}).items():
            cur_hit = summary.notable.setdefault(name, {"count": 0, "samples": []})
            cur_hit["count"] += int(num(hit.get("count")))
            for sample in hit.get("samples") or []:
                if len(cur_hit["samples"]) < 3 and sample not in cur_hit["samples"]:
                    cur_hit["samples"].append(sample)
    summary.messages = sorted(
        merged.values(),
        key=lambda m: (_ERR_PRIOS.get(str(m.get("priority")), 9), -int(num(m.get("count")))),
    )
    return summary


class WindowObserver:
    """Remembers each journal window the collector produces.

    The journal collector's latest result covers only its last run (~1 min), but a report
    covers several minutes. A cheap job polls the collector manager and stores new windows
    here (in memory, last `keep_s` seconds), so the digest can sum the whole period.
    """

    def __init__(self, keep_s: float = 3 * 3600):
        self.keep_s = keep_s
        self._windows: deque[dict[str, Any]] = deque()
        self._seen_end: float = 0.0

    def observe(self, snapshot: dict[str, dict[str, Any]]) -> None:
        result = (snapshot.get("journal") or {}).get("result") or {}
        data = result.get("data") if result.get("ok") else None
        if not isinstance(data, dict):
            return
        end = num(data.get("window_end"))
        if end <= self._seen_end:
            return
        self._seen_end = end
        self._windows.append(data)
        while self._windows and num(self._windows[0].get("window_end")) < end - self.keep_s:
            self._windows.popleft()

    def summary(self, since: float) -> JournalSummary | None:
        windows = [w for w in self._windows if num(w.get("window_end")) > since]
        return summarize_windows(windows) if windows else None
