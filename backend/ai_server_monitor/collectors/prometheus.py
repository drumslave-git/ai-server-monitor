"""Prometheus: runs PromQL queries and reports their results, plus targets and alerts.

Read-only (instant queries and the targets/alerts endpoints over the HTTP API). The queries
are the ones typed in Settings and the enabled ones the planner chose (`promauto`).
Unreachable Prometheus → "unavailable"; a failing single query is reported in that query's
entry and does not affect the others. With `auto_queries` on, down targets and firing
alerts are read directly (no LLM involved) so they can never be missed.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import timedelta
from typing import Any

import httpx

from ..config import PrometheusCollectorSettings
from ..promauto.hosts import instance_host, is_other_host
from ..promauto.store import QueryStore
from ..promql import PromQL, PromQLError
from .base import Collector, CollectorUnavailable, HostEnv

_CONCURRENCY = 4
_PERSIST_SERIES = 10
_MAX_DOWN = 10
_MAX_ALERTS = 20


class PrometheusCollector(Collector):
    name = "prometheus"
    timeout = timedelta(seconds=90)
    cfg: PrometheusCollectorSettings

    def __init__(
        self,
        cfg: PrometheusCollectorSettings,
        env: HostEnv,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        store: QueryStore | None = None,
    ):
        super().__init__(cfg, env)
        self.store = store
        self.client = PromQL(
            cfg.url,
            token=cfg.token.get_secret_value(),
            username=cfg.username,
            password=cfg.password.get_secret_value(),
            timeout=cfg.timeout.total_seconds(),
            max_series=cfg.max_series,
            transport=transport,
        )

    async def check(self) -> None:
        if not self.cfg.url:
            raise CollectorUnavailable("no Prometheus URL set")

    async def close(self) -> None:
        await self.client.close()

    async def _planned(self) -> list[dict[str, Any]]:
        if self.store is None or not self.cfg.auto_queries:
            return []
        try:
            return await self.store.fetch(enabled_only=True)
        except Exception:
            return []

    async def collect(self) -> dict[str, Any]:
        entries_in: list[dict[str, Any]] = [
            {
                "name": q.name,
                "description": q.description,
                "query": q.query,
                "source": "user",
                "unit": q.unit,
                "warn_if": q.warn_if,
                "crit_if": q.crit_if,
            }
            for q in self.cfg.queries
        ]
        taken = {e["name"] for e in entries_in}
        for q in await self._planned():
            if q["name"] in taken:
                continue
            entries_in.append(
                {
                    key: q[key]
                    for key in (
                        [
                            "name",
                            "description",
                            "query",
                            "source",
                            "unit",
                            "kind",
                            "warn_if",
                            "crit_if",
                            "job",
                            "id",
                        ]
                    )
                }
            )
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def run(entry: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            async with sem:
                try:
                    result = await self.client.instant(entry["query"])
                except PromQLError as exc:
                    return {**entry, "ok": False, "error": str(exc)}, exc.unreachable
            return {**entry, "ok": True, **result.to_dict()}, False

        outcomes = list(await asyncio.gather(*(run(e) for e in entries_in)))
        extras: dict[str, Any] = {}
        extras_unreachable = False
        if self.cfg.auto_queries:
            extras, extras_unreachable = await self._targets_and_alerts()
        if (outcomes and all(u for _, u in outcomes)) or (not outcomes and extras_unreachable):
            first = outcomes[0][0]["error"] if outcomes else extras.get("error", "unreachable")
            raise CollectorUnavailable(first)
        entries = [entry for entry, _ in outcomes]
        await self._remember(entries)
        failed = [e["name"] for e in entries if not e["ok"]]
        return {
            "url": self.cfg.url,
            "summary": {"queries": len(entries), "failed": failed},
            "queries": entries,
            **{k: v for k, v in extras.items() if k != "error"},
        }

    async def _remember(self, entries: list[dict[str, Any]]) -> None:
        """Tell the store how each planned query did (and let it disable chronic failures)."""
        if self.store is None:
            return
        rows = [
            (e["id"], bool(e["ok"]), e if e["ok"] else None, e.get("error"))
            for e in entries
            if e.get("id") is not None
        ]
        with contextlib.suppress(Exception):  # bookkeeping must never fail a collection
            await self.store.record_results(rows, time.time())

    async def _targets_and_alerts(self) -> tuple[dict[str, Any], bool]:
        out: dict[str, Any] = {}
        error: str | None = None
        unreachable = False
        try:
            data = await self.client.get_data("/api/v1/targets", {"state": "active"})
            out["targets"] = _summarize_targets((data or {}).get("activeTargets") or [])
        except PromQLError as exc:
            error, unreachable = str(exc), exc.unreachable
            out["targets_error"] = str(exc)
        try:
            data = await self.client.get_data("/api/v1/alerts")
            out["alerts"] = _summarize_alerts((data or {}).get("alerts") or [])
        except PromQLError as exc:
            error = error or str(exc)
            unreachable = unreachable and exc.unreachable
            out["alerts_error"] = str(exc)
        if error:
            out["error"] = error
        return out, unreachable

    def persist_view(self, data: dict[str, Any]) -> dict[str, Any]:
        slim = []
        for q in data.get("queries", []):
            kept = {
                k: v
                for k, v in q.items()
                if k not in ("query", "description", "id", "source", "job", "kind")
            }
            if "series" in kept:
                kept["series"] = kept["series"][:_PERSIST_SERIES]
            slim.append(kept)
        return {**data, "queries": slim}


def _summarize_targets(active: list[dict[str, Any]]) -> dict[str, Any]:
    down: list[dict[str, str]] = []
    other: dict[str, str] = {}
    up = 0
    for t in active:
        labels = t.get("labels") or {}
        job, instance = str(labels.get("job") or "?"), str(labels.get("instance") or "?")
        if is_other_host(instance, job):
            other[job] = instance_host(instance)
        if t.get("health") == "up":
            up += 1
        else:
            down.append(
                {
                    "job": job,
                    "instance": instance,
                    "error": str(t.get("lastError") or "")[:160],
                }
            )
    return {
        "total": len(active),
        "up": up,
        "down": down[:_MAX_DOWN],
        "down_count": len(down),
        "other_hosts": other,
    }


def _summarize_alerts(alerts: list[dict[str, Any]]) -> dict[str, Any]:
    firing = [a for a in alerts if a.get("state") == "firing"]
    shown = []
    for a in firing[:_MAX_ALERTS]:
        labels = a.get("labels") or {}
        annotations = a.get("annotations") or {}
        shown.append(
            {
                "name": str(labels.get("alertname") or "?"),
                "severity": str(labels.get("severity") or ""),
                "instance": str(labels.get("instance") or labels.get("job") or ""),
                "summary": str(annotations.get("summary") or annotations.get("description") or "")[
                    :160
                ],
                "active_at": a.get("activeAt"),
            }
        )
    return {
        "firing": shown,
        "firing_count": len(firing),
        "pending_count": sum(1 for a in alerts if a.get("state") == "pending"),
    }
