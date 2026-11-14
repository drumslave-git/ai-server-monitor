"""Prometheus: runs the configured PromQL queries and reports their results.

Read-only (instant queries over the HTTP API). Unreachable Prometheus → "unavailable";
a failing single query is reported in that query's entry and does not affect the others.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import httpx

from ..config import PrometheusCollectorSettings, PromQuery
from ..promql import PromQL, PromQLError
from .base import Collector, CollectorUnavailable, HostEnv

_CONCURRENCY = 4
_PERSIST_SERIES = 10


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
    ):
        super().__init__(cfg, env)
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

    async def collect(self) -> dict[str, Any]:
        queries = self.cfg.queries
        sem = asyncio.Semaphore(_CONCURRENCY)

        async def run(q: PromQuery) -> tuple[dict[str, Any], bool]:
            entry: dict[str, Any] = {
                "name": q.name,
                "description": q.description,
                "query": q.query,
            }
            async with sem:
                try:
                    result = await self.client.instant(q.query)
                except PromQLError as exc:
                    return {**entry, "ok": False, "error": str(exc)}, exc.unreachable
            return {**entry, "ok": True, **result.to_dict()}, False

        outcomes = await asyncio.gather(*(run(q) for q in queries))
        if outcomes and all(unreachable for _, unreachable in outcomes):
            raise CollectorUnavailable(outcomes[0][0]["error"])
        entries = [entry for entry, _ in outcomes]
        failed = [e["name"] for e in entries if not e["ok"]]
        return {
            "url": self.cfg.url,
            "summary": {"queries": len(entries), "failed": failed},
            "queries": entries,
        }

    def persist_view(self, data: dict[str, Any]) -> dict[str, Any]:
        slim = []
        for q in data.get("queries", []):
            kept = {k: v for k, v in q.items() if k not in ("query", "description")}
            if "series" in kept:
                kept["series"] = kept["series"][:_PERSIST_SERIES]
            slim.append(kept)
        return {**data, "queries": slim}
