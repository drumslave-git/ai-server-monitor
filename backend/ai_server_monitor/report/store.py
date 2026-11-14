"""Report persistence."""

from __future__ import annotations

import json
from typing import Any

import aiosqlite

from ..db import Database

_LIST_COLUMNS = (
    "id, ts, status, health, score, summary, findings, model, tokens_in, tokens_out, "
    "latency_ms, error, parsed, digest_hash"
)


def _row(row: aiosqlite.Row, *, full: bool) -> dict[str, Any]:
    out = dict(row)
    try:
        out["findings"] = json.loads(out["findings"]) if out.get("findings") else []
    except ValueError:
        out["findings"] = []
    out["parsed"] = bool(out.get("parsed"))
    if not full:
        out.pop("markdown", None)
        out.pop("digest", None)
    return out


class ReportStore:
    def __init__(self, db: Database):
        self.db = db

    async def add(
        self,
        *,
        ts: float,
        status: str,
        markdown: str = "",
        health: str | None = None,
        score: int | None = None,
        summary: str | None = None,
        findings: list[dict[str, Any]] | None = None,
        digest: str | None = None,
        digest_hash: str | None = None,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
        parsed: bool = True,
    ) -> int:
        return await self.db.execute(
            "INSERT INTO reports (ts, status, health, score, summary, findings, markdown, digest, "
            "digest_hash, model, tokens_in, tokens_out, latency_ms, error, parsed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts, status, health, score, summary, json.dumps(findings or []), markdown, digest,
                digest_hash, model, tokens_in, tokens_out, latency_ms, error, int(parsed),
            ),
        )  # fmt: skip

    async def record_problem(
        self, *, ts: float, status: str, error: str, model: str | None, digest_hash: str | None
    ) -> int:
        """Store a failed/skipped attempt. A repeat of the previous identical problem only
        moves its timestamp, so an unconfigured or unreachable LLM does not fill the table."""
        last = await self.db.fetchone(
            "SELECT id, status, error FROM reports ORDER BY id DESC LIMIT 1"
        )
        if last is not None and last["status"] == status and last["error"] == error:
            await self.db.execute("UPDATE reports SET ts = ? WHERE id = ?", (ts, last["id"]))
            return int(last["id"])
        return await self.add(
            ts=ts, status=status, error=error, model=model, digest_hash=digest_hash, parsed=False
        )

    async def get(self, report_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM reports WHERE id = ?", (report_id,))
        return _row(row, full=True) if row else None

    async def latest(self, *, status: str | None = "ok") -> dict[str, Any] | None:
        sql = f"SELECT {_LIST_COLUMNS} FROM reports"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        row = await self.db.fetchone(sql + " ORDER BY ts DESC, id DESC LIMIT 1", params)
        return _row(row, full=False) if row else None

    async def page(
        self, *, limit: int = 50, offset: int = 0, status: str | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        where, params = ("WHERE status = ?", (status,)) if status else ("", ())
        total_row = await self.db.fetchone(f"SELECT COUNT(*) AS n FROM reports {where}", params)
        rows = await self.db.fetchall(
            f"SELECT {_LIST_COLUMNS} FROM reports {where} "
            "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [_row(r, full=False) for r in rows], int(total_row["n"]) if total_row else 0
