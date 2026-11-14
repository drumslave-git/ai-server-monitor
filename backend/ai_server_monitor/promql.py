"""PromQL over the Prometheus HTTP API: instant and range queries, normalized results.

Independent of the collector so that other parts (e.g. the chat's `promql_query` tool) can
reuse it. Read-only: only `GET /api/v1/query` and `GET /api/v1/query_range`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_SERIES = 50
DEFAULT_MAX_POINTS = 120
MAX_ERROR_LENGTH = 300


class PromQLError(Exception):
    """A query failed. `unreachable` = no HTTP answer at all (network, DNS, timeout)."""

    def __init__(self, message: str, *, unreachable: bool = False):
        super().__init__(message)
        self.unreachable = unreachable


Value = float | str
"""A sample value. Non-finite values stay strings ('NaN', '+Inf') — they are not valid JSON."""


def parse_value(raw: Any) -> Value:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    return value if math.isfinite(value) else str(raw)


@dataclass
class QueryResult:
    result_type: str
    """vector | scalar | matrix | string (as Prometheus reports it)."""
    series: list[dict[str, Any]] = field(default_factory=list)
    """Instant: [{labels, value}]. Range: [{labels, values: [[ts, value], ...]}]."""
    total: int = 0
    """Number of series before the cap."""
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "result_type": self.result_type,
            "series": self.series,
            "total": self.total,
            "truncated": self.truncated,
        }
        if self.warnings:
            out["warnings"] = self.warnings
        return out


def _downsample(points: list[list[Any]], limit: int) -> list[list[Any]]:
    """At most `limit` points, evenly spread, always keeping the first and the last."""
    if len(points) <= limit:
        return points
    if limit < 2:
        return points[-1:]
    step = (len(points) - 1) / (limit - 1)
    return [points[round(i * step)] for i in range(limit)]


def normalize(
    data: dict[str, Any],
    *,
    max_series: int = DEFAULT_MAX_SERIES,
    max_points: int = DEFAULT_MAX_POINTS,
) -> QueryResult:
    """Turn the `data` member of a Prometheus response into a QueryResult."""
    kind = str(data.get("resultType", ""))
    raw = data.get("result")
    if kind in ("scalar", "string"):
        # [ts, "value"]
        if not isinstance(raw, list) or len(raw) != 2:
            raise PromQLError(f"unexpected {kind} result")
        value = parse_value(raw[1]) if kind == "scalar" else str(raw[1])
        return QueryResult(kind, [{"labels": {}, "value": value}], total=1)
    if kind not in ("vector", "matrix") or not isinstance(raw, list):
        raise PromQLError(f"unsupported result type {kind!r}")
    series: list[dict[str, Any]] = []
    for item in raw[:max_series]:
        labels = item.get("metric") or {}
        if kind == "vector":
            series.append({"labels": labels, "value": parse_value(item["value"][1])})
        else:
            points = [[float(ts), parse_value(v)] for ts, v in item.get("values") or []]
            series.append({"labels": labels, "values": _downsample(points, max_points)})
    return QueryResult(kind, series, total=len(raw), truncated=len(raw) > max_series)


class PromQL:
    """Async Prometheus client. One instance per Prometheus; call `close()` when done."""

    def __init__(
        self,
        url: str,
        *,
        token: str = "",
        username: str = "",
        password: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        max_series: int = DEFAULT_MAX_SERIES,
        max_points: int = DEFAULT_MAX_POINTS,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url.strip().rstrip("/")
        self.timeout = timeout
        self.max_series = max_series
        self.max_points = max_points
        headers: dict[str, str] = {"Accept": "application/json"}
        auth: httpx.Auth | None = None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        elif username:
            auth = httpx.BasicAuth(username, password)
        self._http = httpx.AsyncClient(
            headers=headers,
            auth=auth,
            transport=transport,
            timeout=httpx.Timeout(timeout + 5, connect=min(timeout, 5.0)),
            follow_redirects=True,
        )

    async def instant(self, query: str, *, at: float | None = None) -> QueryResult:
        """Evaluate `query` at one point in time (default: now)."""
        params: dict[str, Any] = {"query": query}
        if at is not None:
            params["time"] = at
        return await self._call("/api/v1/query", params)

    async def range(
        self, query: str, *, start: float, end: float, step: float | str = 60
    ) -> QueryResult:
        """Evaluate `query` over [start, end] (unix seconds) with a resolution of `step`."""
        if end <= start:
            raise PromQLError("end must be after start")
        return await self._call(
            "/api/v1/query_range", {"query": query, "start": start, "end": end, "step": step}
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, path: str, params: dict[str, Any]) -> QueryResult:
        params = {**params, "timeout": f"{self.timeout:g}s"}
        try:
            response = await self._http.get(self.url + path, params=params)
        except httpx.TimeoutException as exc:
            raise PromQLError(
                f"timed out after {self.timeout:g}s ({type(exc).__name__})", unreachable=True
            ) from exc
        except httpx.HTTPError as exc:
            raise PromQLError(
                f"cannot reach Prometheus at {self.url}: {type(exc).__name__}: {exc}".rstrip(": "),
                unreachable=True,
            ) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise PromQLError(
                f"HTTP {response.status_code}: " + response.text.strip()[:MAX_ERROR_LENGTH]
                if response.status_code >= 400
                else "response is not a Prometheus API reply (is the URL a Prometheus?)"
            )
        if body.get("status") != "success":
            kind = body.get("errorType")
            text = str(body.get("error") or f"HTTP {response.status_code}")
            raise PromQLError((f"{kind}: " if kind else "") + text[:MAX_ERROR_LENGTH])
        result = normalize(
            body.get("data") or {}, max_series=self.max_series, max_points=self.max_points
        )
        result.warnings = [str(w)[:MAX_ERROR_LENGTH] for w in body.get("warnings") or []]
        return result
