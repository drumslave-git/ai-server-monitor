"""Persistence of benchmark runs, attempts and captured snapshots (tables from migration 0008)."""

from __future__ import annotations

import json
import time
from typing import Any

from ..db import Database

_RUN_JSON = ("group_scores", "summary", "meta")
_ATTEMPT_JSON = ("checks", "trace_ids", "detail", "hw")
_ATTEMPT_LIST = (
    "id, run_id, scenario_id, grp, attempt, started, status, score, error, ttft_ms, total_ms, "
    "tokens_in, tokens_out, tokens_per_s, llm_calls, tool_calls, tool_mode, report_overlap, "
    "judge_score, json_extract(detail, '$.reload_suspected') AS reload_suspected, hw, effort, "
    "reasoning_chars, answer_issue"
)


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None


def _run(row: Any, *, full: bool) -> dict[str, Any]:
    out = dict(row)
    for key in _RUN_JSON:
        out[key] = _loads(out.get(key))
    if not full:
        meta = out.pop("meta") or {}
        out["gpu"] = meta.get("gpu")
        out["tool_mode"] = meta.get("tool_mode")
        out["temperature"] = (meta.get("llm") or {}).get("temperature")
        out["cold_start"] = meta.get("cold_start")
    return out


def _attempt(row: Any, *, full: bool) -> dict[str, Any]:
    out = dict(row)
    out["report_overlap"] = bool(out.get("report_overlap"))
    for key in _ATTEMPT_JSON:
        if key in out:
            out[key] = _loads(out[key])
    return out


class BenchStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ runs

    async def create_run(self, **fields: Any) -> int:
        fields.setdefault("started", time.time())
        fields.setdefault("status", "running")
        cols = list(fields)
        values = [_dumps(v) if k in _RUN_JSON else v for k, v in fields.items()]
        return await self.db.execute(
            f"INSERT INTO bench_runs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            values,
        )

    async def update_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = [_dumps(v) if k in _RUN_JSON else v for k, v in fields.items()]
        await self.db.execute(f"UPDATE bench_runs SET {sets} WHERE id = ?", (*values, run_id))

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM bench_runs WHERE id = ?", (run_id,))
        return _run(row, full=True) if row else None

    async def list_runs(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM bench_runs ORDER BY started DESC")
        return [_run(r, full=False) for r in rows]

    async def delete_run(self, run_id: int) -> bool:
        cur = await self.db.conn.execute("DELETE FROM bench_runs WHERE id = ?", (run_id,))
        await self.db.conn.commit()
        return cur.rowcount > 0

    async def recover(self) -> int:
        """Runs still marked running at start-up were interrupted by a restart."""
        return await self.db.execute(
            "UPDATE bench_runs SET status = 'failed', finished = ?, error = ? "
            "WHERE status = 'running'",
            (time.time(), "interrupted: the monitor was restarted during the run"),
        )

    # ------------------------------------------------------------------ attempts

    async def add_attempt(self, run_id: int, row: dict[str, Any]) -> int:
        row = {**row, "run_id": run_id}
        cols = list(row)
        values = [_dumps(v) if k in _ATTEMPT_JSON else v for k, v in row.items()]
        return await self.db.execute(
            f"INSERT INTO bench_attempts ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            values,
        )

    async def set_judge(self, attempt_id: int, score: int | None, reason: str | None) -> None:
        await self.db.execute(
            "UPDATE bench_attempts SET judge_score = ?, judge_reason = ? WHERE id = ?",
            (score, reason, attempt_id),
        )

    async def flag_reload(self, attempt_id: int, reason: str) -> None:
        """Mark an attempt as "the model may have been (re)loaded meanwhile" (kept in `detail`)."""
        await self.db.execute(
            "UPDATE bench_attempts SET detail = json_set(coalesce(detail, '{}'), "
            "'$.reload_suspected', ?) WHERE id = ?",
            (reason, attempt_id),
        )

    async def attempts(self, run_id: int) -> list[dict[str, Any]]:
        """Every attempt of a run without the bulky fields (answer, checks, details)."""
        rows = await self.db.fetchall(
            f"SELECT {_ATTEMPT_LIST} FROM bench_attempts WHERE run_id = ? "
            "ORDER BY scenario_id, attempt",
            (run_id,),
        )
        return [_attempt(r, full=False) for r in rows]

    async def attempt(self, attempt_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM bench_attempts WHERE id = ?", (attempt_id,))
        return _attempt(row, full=True) if row else None

    async def failed_attempts(self, run_id: int) -> list[dict[str, Any]]:
        """The attempts that did not pass (an error or timeout, or a score under 100 %) with their
        checks and answer, for the PDF appendix. Passed attempts are not read."""
        rows = await self.db.fetchall(
            "SELECT id, scenario_id, grp, attempt, effort, status, score, error, checks, answer, "
            "answer_issue FROM bench_attempts WHERE run_id = ? "
            "AND (status != 'ok' OR coalesce(score, 0) < 0.999) "
            "ORDER BY scenario_id, effort, attempt",
            (run_id,),
        )
        return [_attempt(r, full=True) for r in rows]

    async def explain_rows(self, run_id: int) -> list[dict[str, Any]]:
        """What the per-scenario explanations are built from: status, error, score and checks of
        every attempt (not the answers)."""
        rows = await self.db.fetchall(
            "SELECT scenario_id, status, error, score, judge_score, checks, answer_issue "
            "FROM bench_attempts WHERE run_id = ?",
            (run_id,),
        )
        return [_attempt(r, full=True) for r in rows]

    async def full_attempts(self, run_id: int) -> list[dict[str, Any]]:
        """Everything, for the JSON export."""
        rows = await self.db.fetchall(
            "SELECT * FROM bench_attempts WHERE run_id = ? ORDER BY scenario_id, attempt",
            (run_id,),
        )
        return [_attempt(r, full=True) for r in rows]

    # ------------------------------------------------------------------ hardware samples

    async def hardware_samples(self, run_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT ts, gpu_util, vram_used, vram_total, llm_vram, power_w, temp_c, cpu, "
            "ram_used, ram_total FROM bench_hw_samples WHERE run_id = ? ORDER BY ts",
            (run_id,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ snapshots

    async def save_snapshot(self, name: str, doc: dict[str, Any], description: str = "") -> None:
        await self.db.execute(
            "INSERT INTO bench_snapshots (name, created, description, doc) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET created = excluded.created, "
            "description = excluded.description, doc = excluded.doc",
            (name, time.time(), description, json.dumps(doc, ensure_ascii=False)),
        )

    async def snapshot_doc(self, name: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT doc FROM bench_snapshots WHERE name = ?", (name,))
        return _loads(row["doc"]) if row else None

    async def list_snapshots(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT name, created, description, length(doc) AS size FROM bench_snapshots "
            "ORDER BY created DESC"
        )
        return [dict(r) for r in rows]

    async def delete_snapshot(self, name: str) -> bool:
        cur = await self.db.conn.execute("DELETE FROM bench_snapshots WHERE name = ?", (name,))
        await self.db.conn.commit()
        return cur.rowcount > 0
