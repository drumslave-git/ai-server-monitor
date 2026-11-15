"""Storage of the planner's queries (table `prom_queries`) and of its bookkeeping (`kv`)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Database
from ..thresholds import normalize_threshold

MAX_FAILS = 5
"""A query that fails this many runs in a row is disabled, with the reason."""
KV_PLAN = "prom_plan"
KV_DISMISSED = "prom_dismissed"
MAX_DISMISSED = 200
_NAME_BAD = re.compile(r"[^a-z0-9_]+")
MAX_NAME = 40
MAX_QUERY = 600
MAX_LAST_VALUE = 1500

_FIELDS = [
    "id",
    "name",
    "query",
    "description",
    "unit",
    "kind",
    "warn_if",
    "crit_if",
    "source",
    "job",
    "enabled",
    "pinned",
    "edited",
    "fail_count",
    "disabled_reason",
    "last_ok_ts",
    "last_error",
    "last_error_ts",
    "last_value",
    "created_ts",
    "updated_ts",
]


def clean_name(raw: object) -> str:
    """snake_case identifier, at most MAX_NAME characters; '' when nothing usable is left."""
    text = _NAME_BAD.sub("_", str(raw or "").strip().lower()).strip("_")
    return text[:MAX_NAME].strip("_")


@dataclass
class Proposal:
    """One validated query, ready to be stored."""

    name: str
    query: str
    description: str = ""
    unit: str = ""
    kind: str = "value"
    warn_if: str | None = None
    crit_if: str | None = None
    job: str = ""
    source: str = "auto"
    last_value: dict[str, Any] | None = field(default=None, repr=False)


def _row(row: Any) -> dict[str, Any]:
    out = {k: row[k] for k in _FIELDS}
    for flag in ("enabled", "pinned", "edited"):
        out[flag] = bool(out[flag])
    raw = out["last_value"]
    out["last_value"] = json.loads(raw) if raw else None
    return out


def compact_value(result: dict[str, Any]) -> dict[str, Any]:
    """The part of a query result worth keeping: up to 3 series, in short form."""
    series = [
        {"labels": s.get("labels") or {}, "value": s.get("value")}
        for s in (result.get("series") or [])[:3]
    ]
    out: dict[str, Any] = {"total": result.get("total", len(series)), "series": series}
    while len(json.dumps(out)) > MAX_LAST_VALUE and out["series"]:
        out["series"].pop()
    return out


class QueryStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ reading

    async def fetch(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = " WHERE enabled = 1" if enabled_only else ""
        rows = await self.db.fetchall(f"SELECT * FROM prom_queries{where} ORDER BY name")
        return [_row(r) for r in rows]

    async def get(self, query_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM prom_queries WHERE id = ?", (query_id,))
        return _row(row) if row else None

    # ------------------------------------------------------------------ results

    async def record_results(
        self, results: list[tuple[int, bool, dict[str, Any] | None, str | None]], now: float
    ) -> None:
        """Remember each run: (id, ok, result dict, error). Disables chronic failures."""
        if not results:
            return
        async with self.db.transaction() as conn:
            for query_id, ok, result, error in results:
                if ok:
                    value = json.dumps(compact_value(result or {}))
                    await conn.execute(
                        "UPDATE prom_queries SET fail_count = 0, last_ok_ts = ?, last_value = ?, "
                        "last_error = NULL WHERE id = ?",
                        (now, value, query_id),
                    )
                    continue
                text = (error or "failed")[:300]
                await conn.execute(
                    "UPDATE prom_queries SET fail_count = fail_count + 1, last_error = ?, "
                    "last_error_ts = ? WHERE id = ?",
                    (text, now, query_id),
                )
                await conn.execute(
                    "UPDATE prom_queries SET enabled = 0, disabled_reason = "
                    "'failed ' || fail_count || ' runs in a row: ' || ? "
                    "WHERE id = ? AND fail_count >= ? AND enabled = 1",
                    (text[:150], query_id, MAX_FAILS),
                )

    # ------------------------------------------------------------------ user actions

    async def update(self, query_id: int, changes: dict[str, Any]) -> dict[str, Any] | None:
        """Apply a user's edit. Changing what the query says marks it `edited` (kept on
        re-plan); re-enabling clears the failure state."""
        current = await self.get(query_id)
        if current is None:
            return None
        sets: dict[str, Any] = {}
        content = ("name", "query", "description", "unit", "warn_if", "crit_if", "kind")
        for key in content:
            if key not in changes:
                continue
            value = changes[key]
            if key in ("warn_if", "crit_if"):
                value = normalize_threshold(value)
            if value != current[key]:
                sets[key] = value
        if sets:
            sets["edited"] = 1
        if "pinned" in changes:
            sets["pinned"] = int(bool(changes["pinned"]))
        if "enabled" in changes:
            sets["enabled"] = int(bool(changes["enabled"]))
            if changes["enabled"]:
                sets.update(fail_count=0, disabled_reason=None, last_error=None)
            elif not current["enabled"]:
                sets.pop("enabled")
        if "query" in sets:
            sets.update(fail_count=0, last_error=None, disabled_reason=None)
        if not sets:
            return current
        sets["updated_ts"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in sets)
        await self.db.execute(
            f"UPDATE prom_queries SET {cols} WHERE id = ?", (*sets.values(), query_id)
        )
        return await self.get(query_id)

    async def delete(self, query_id: int) -> bool:
        """Delete a query; an auto-planned one is remembered so a re-plan does not bring it
        back."""
        current = await self.get(query_id)
        if current is None:
            return False
        await self.db.execute("DELETE FROM prom_queries WHERE id = ?", (query_id,))
        if current["source"] in ("auto", "fallback"):
            dismissed = [n for n in await self.dismissed() if n != current["name"]]
            dismissed.append(current["name"])
            await self.db.kv_set(KV_DISMISSED, dismissed[-MAX_DISMISSED:])
        return True

    async def dismissed(self) -> list[str]:
        value = await self.db.kv_get(KV_DISMISSED, [])
        return [str(n) for n in value] if isinstance(value, list) else []

    # ------------------------------------------------------------------ planning

    async def apply_plan(self, accepted: list[Proposal], *, source: str) -> dict[str, int]:
        """Replace the planned queries by `accepted`.

        Pinned and edited queries stay as they are; unchanged ones keep their enabled/pinned
        state and history; planned queries that were not proposed again are removed.
        """
        now = time.time()
        existing = {q["name"]: q for q in await self.fetch()}
        counts = {"added": 0, "updated": 0, "removed": 0, "kept": 0}
        keep_names = {n for n, q in existing.items() if q["pinned"] or q["edited"]}
        accepted = [p for p in accepted if p.name not in keep_names]
        new_names = {p.name for p in accepted}
        async with self.db.transaction() as conn:
            for name, q in existing.items():
                if name in keep_names:
                    counts["kept"] += 1
                elif name not in new_names:
                    await conn.execute("DELETE FROM prom_queries WHERE id = ?", (q["id"],))
                    counts["removed"] += 1
            for p in accepted:
                old = existing.get(p.name)
                value = json.dumps(compact_value(p.last_value)) if p.last_value else None
                if old is None:
                    await conn.execute(
                        "INSERT INTO prom_queries (name, query, description, unit, kind, warn_if, "
                        "crit_if, source, job, last_ok_ts, last_value, created_ts, updated_ts) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            p.name, p.query, p.description, p.unit, p.kind, p.warn_if, p.crit_if,
                            source, p.job, now if value else None, value, now, now,
                        ),
                    )  # fmt: skip
                    counts["added"] += 1
                else:
                    await conn.execute(
                        "UPDATE prom_queries SET query = ?, description = ?, unit = ?, kind = ?, "
                        "warn_if = ?, crit_if = ?, source = ?, job = ?, updated_ts = ?, "
                        "last_ok_ts = COALESCE(?, last_ok_ts), "
                        "last_value = COALESCE(?, last_value) WHERE id = ?",
                        (
                            p.query, p.description, p.unit, p.kind, p.warn_if, p.crit_if, source,
                            p.job, now, now if value else None, value, old["id"],
                        ),
                    )  # fmt: skip
                    counts["updated"] += 1
        return counts


async def list_enabled_queries(db: Database) -> list[dict[str, Any]]:
    """The enabled auto-planned queries, for other parts (e.g. the chat's PromQL tool).

    Each entry: name, query, description, unit, kind, warn_if, crit_if, job, source,
    last_ok_ts and last_value (the last result in compact form, or None).
    """
    keys = [
        "name",
        "query",
        "description",
        "unit",
        "kind",
        "warn_if",
        "crit_if",
        "job",
        "source",
        "last_ok_ts",
        "last_value",
    ]
    return [{k: q[k] for k in keys} for q in await QueryStore(db).fetch(enabled_only=True)]
