"""SQLite storage of the trace: LLM calls, chat tool calls and the persisted app log."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from ..db import Database
from . import codec
from .config import TraceConfig

SEARCH_SCAN_LIMIT = 2000
"""Bodies are compressed, so text search decompresses the newest rows; this bounds the work."""
SEARCH_MAX_HITS = 500

_BLOBS = ("messages", "tools", "content", "reasoning", "tool_calls")
_SUMMARY_COLS = (
    "id, ts, purpose, link_kind, link_id, link_step, turn, group_id, attempt, model, base_url,"
    " stream, status, http_status, error, finish_reason, prompt_tokens, completion_tokens,"
    " usage_estimated, queued_ms, ttft_ms, total_ms, tokens_per_s, messages_count, tools_count,"
    " tools_tokens, request_bytes, response_chars, tool_calls_count, request_preview,"
    " response_preview, truncated, bodies, size_bytes"
)


@dataclass
class CallRecord:
    ts: float
    purpose: str
    status: str
    link_kind: str | None = None
    link_id: str | None = None
    link_step: int | None = None
    turn: str | None = None
    group_id: str | None = None
    attempt: int = 1
    model: str | None = None
    base_url: str | None = None
    stream: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    http_status: int | None = None
    error: str | None = None
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    usage_estimated: bool = False
    queued_ms: int | None = None
    ttft_ms: int | None = None
    total_ms: int | None = None
    request_bytes: int | None = None
    messages: list[Any] = field(default_factory=list)
    tools: list[Any] | None = None
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolRecord:
    ts: float
    tool: str
    ok: bool
    link_kind: str | None = None
    link_id: str | None = None
    link_step: int | None = None
    turn: str | None = None
    duration_ms: int | None = None
    result_size: int | None = None
    error: str | None = None
    args: Any = None
    result: Any = None


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def _preview(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def request_preview(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return _preview(_message_text(message))
    return _preview(_message_text(messages[-1])) if messages else ""


def response_preview(content: str, tool_calls: list[dict[str, Any]]) -> str:
    if content.strip():
        return _preview(content)
    if tool_calls:
        return _preview("→ " + ", ".join(f"{c['name']}({c['arguments']})" for c in tool_calls))
    return ""


class TraceStore:
    def __init__(self, db: Database, config: TraceConfig, scrubber: Any = None):
        self.db = db
        self.config = config
        self._scrubber = scrubber
        """None, or a callable returning an object with `clean(value)` and `text(str)`."""

    def _scrub(self) -> Any | None:
        return self._scrubber() if self._scrubber is not None else None

    # ------------------------------------------------------------------ writing

    def _field(self, value: Any, scrubber: Any, truncated: list[str], name: str) -> bytes:
        text, cut = codec.cap_json(codec.scrub_value(value, scrubber), self.config.field_cap_bytes)
        if cut:
            truncated.append(name)
        return codec.pack(text)

    def _text_field(self, value: str, scrubber: Any, truncated: list[str], name: str) -> bytes:
        text, cut = codec.cap_text(codec.scrub_str(value, scrubber), self.config.field_cap_bytes)
        if cut:
            truncated.append(name)
        return codec.pack(text)

    async def insert_call(self, rec: CallRecord) -> int:
        cfg = self.config
        scrub = self._scrub()
        truncated: list[str] = []
        blobs: dict[str, bytes | None] = dict.fromkeys(_BLOBS)
        previews = ("", "")
        tools_tokens: int | None = None
        if rec.tools:
            tools_tokens = estimate_tokens(codec.dumps(rec.tools))
        if cfg.store_bodies:
            blobs["messages"] = self._field(rec.messages, scrub, truncated, "messages")
            if rec.tools:
                blobs["tools"] = self._field(rec.tools, scrub, truncated, "tools")
            blobs["content"] = self._text_field(rec.content, scrub, truncated, "content")
            if rec.reasoning:
                blobs["reasoning"] = self._text_field(rec.reasoning, scrub, truncated, "reasoning")
            if rec.tool_calls:
                blobs["tool_calls"] = self._field(rec.tool_calls, scrub, truncated, "tool_calls")
            previews = (
                codec.scrub_str(request_preview(rec.messages), scrub),
                codec.scrub_str(response_preview(rec.content, rec.tool_calls), scrub),
            )
        size = sum(len(b) for b in blobs.values() if b) + 400
        generation_ms = (rec.total_ms or 0) - (rec.ttft_ms or 0)
        tokens_per_s = None
        if rec.completion_tokens and generation_ms > 0:
            tokens_per_s = round(rec.completion_tokens / (generation_ms / 1000), 2)
        error = codec.scrub_str(rec.error, scrub) if rec.error else None
        params = codec.scrub_value(rec.params, scrub)
        row = await self.db.execute(
            "INSERT INTO llm_calls (ts, purpose, link_kind, link_id, link_step, turn, group_id,"
            " attempt, model, base_url, stream, params, status, http_status, error, finish_reason,"
            " prompt_tokens, completion_tokens, usage_estimated, queued_ms, ttft_ms, total_ms,"
            " tokens_per_s, messages_count, tools_count, tools_tokens, request_bytes,"
            " response_chars, reasoning_chars, tool_calls_count, request_preview, response_preview,"
            " messages, tools, content, reasoning, tool_calls, truncated, bodies, size_bytes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?)",
            (
                rec.ts,
                rec.purpose,
                rec.link_kind,
                rec.link_id,
                rec.link_step,
                rec.turn,
                rec.group_id,
                rec.attempt,
                rec.model,
                rec.base_url,
                int(rec.stream),
                codec.dumps(params),
                rec.status,
                rec.http_status,
                error,
                rec.finish_reason,
                rec.prompt_tokens,
                rec.completion_tokens,
                int(rec.usage_estimated),
                rec.queued_ms,
                rec.ttft_ms,
                rec.total_ms,
                tokens_per_s,
                len(rec.messages),
                len(rec.tools or []),
                tools_tokens,
                rec.request_bytes,
                len(rec.content) + len(rec.reasoning),
                len(rec.reasoning),
                len(rec.tool_calls),
                previews[0] or None,
                previews[1] or None,
                blobs["messages"],
                blobs["tools"],
                blobs["content"],
                blobs["reasoning"],
                blobs["tool_calls"],
                json.dumps(truncated) if truncated else None,
                int(cfg.store_bodies),
                size,
            ),
        )
        return row

    async def insert_tool(self, rec: ToolRecord) -> int:
        cfg = self.config
        scrub = self._scrub()
        truncated: list[str] = []
        args = result = None
        if cfg.store_bodies:
            args = self._field(rec.args, scrub, truncated, "args")
            if rec.result is not None:
                if isinstance(rec.result, str):
                    result = self._text_field(rec.result, scrub, truncated, "result")
                else:
                    result = self._field(rec.result, scrub, truncated, "result")
        size = (len(args) if args else 0) + (len(result) if result else 0) + 200
        return await self.db.execute(
            "INSERT INTO llm_tool_calls (ts, link_kind, link_id, link_step, turn, tool, ok,"
            " duration_ms, result_size, error, args, result, truncated, size_bytes)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rec.ts,
                rec.link_kind,
                rec.link_id,
                rec.link_step,
                rec.turn,
                rec.tool,
                int(rec.ok),
                rec.duration_ms,
                rec.result_size,
                codec.scrub_str(rec.error, scrub) if rec.error else None,
                args,
                result,
                int(bool(truncated)),
                size,
            ),
        )

    async def attach(self, call_ids: list[int], link_kind: str, link_id: str | int) -> int:
        if not call_ids:
            return 0
        marks = ",".join("?" * len(call_ids))
        return await self._rowcount(
            f"UPDATE llm_calls SET link_kind = ?, link_id = ? WHERE id IN ({marks})",
            (link_kind, str(link_id), *call_ids),
        )

    async def _rowcount(self, sql: str, params: Any) -> int:
        return await self.db.write(sql, params)

    # ------------------------------------------------------------------ reading

    @staticmethod
    def _where(
        *,
        purpose: str | None,
        status: str | None,
        since: float | None,
        until: float | None,
        link_kind: str | None,
        link_id: str | None,
        turn: str | None,
        model: str | None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("purpose", purpose),
            ("status", status),
            ("link_kind", link_kind),
            ("link_id", link_id),
            ("turn", turn),
            ("model", model),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    async def list_calls(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        purpose: str | None = None,
        status: str | None = None,
        since: float | None = None,
        until: float | None = None,
        link_kind: str | None = None,
        link_id: str | None = None,
        turn: str | None = None,
        model: str | None = None,
        q: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Newest first. Returns (items, total, searched_all). With `q` the match is done on
        the newest SEARCH_SCAN_LIMIT rows only (`searched_all` False when older rows exist)."""
        where, params = self._where(
            purpose=purpose,
            status=status,
            since=since,
            until=until,
            link_kind=link_kind,
            link_id=link_id,
            turn=turn,
            model=model,
        )
        if q:
            return await self._search(where, params, q, limit, offset)
        total_row = await self.db.fetchone(f"SELECT COUNT(*) FROM llm_calls{where}", params)
        rows = await self.db.fetchall(
            f"SELECT {_SUMMARY_COLS} FROM llm_calls{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return [self._summary(r) for r in rows], int(total_row[0]) if total_row else 0, True

    async def _search(
        self, where: str, params: list[Any], q: str, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int, bool]:
        needle = q.lower()
        hits: list[int] = []
        scanned = 0
        last_id: int | None = None
        while scanned < SEARCH_SCAN_LIMIT and len(hits) < SEARCH_MAX_HITS:
            extra = "" if last_id is None else (" AND" if where else " WHERE") + " id < ?"
            rows = await self.db.fetchall(
                "SELECT id, messages, tools, content, reasoning, tool_calls, error, purpose,"
                f" model FROM llm_calls{where}{extra} ORDER BY id DESC LIMIT 100",
                [*params, *([last_id] if last_id is not None else [])],
            )
            if not rows:
                break
            for row in rows:
                scanned += 1
                last_id = row["id"]
                haystack = (
                    " ".join(codec.unpack(row[c]) or "" for c in _BLOBS if row[c] is not None)
                    + f" {row['error'] or ''}"
                )
                if needle in haystack.lower():
                    hits.append(row["id"])
            if len(rows) < 100:
                break
        searched_all = scanned < SEARCH_SCAN_LIMIT or len(hits) >= SEARCH_MAX_HITS
        page = hits[offset : offset + limit]
        items: list[dict[str, Any]] = []
        if page:
            marks = ",".join("?" * len(page))
            rows = await self.db.fetchall(
                f"SELECT {_SUMMARY_COLS} FROM llm_calls WHERE id IN ({marks}) ORDER BY id DESC",
                page,
            )
            items = [self._summary(r) for r in rows]
        return items, len(hits), searched_all

    @staticmethod
    def _summary(row: Any) -> dict[str, Any]:
        d = dict(row)
        d["stream"] = bool(d["stream"])
        d["usage_estimated"] = bool(d["usage_estimated"])
        d["bodies"] = bool(d["bodies"])
        d["truncated"] = json.loads(d["truncated"]) if d.get("truncated") else []
        return d

    async def get_call(self, call_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM llm_calls WHERE id = ?", (call_id,))
        if row is None:
            return None
        d = self._summary(row)
        d["params"] = json.loads(row["params"]) if row["params"] else {}
        for name in ("messages", "tools", "tool_calls"):
            text = codec.unpack(row[name])
            d[name] = json.loads(text) if text else None
        d["content"] = codec.unpack(row["content"])
        d["reasoning"] = codec.unpack(row["reasoning"])
        return d

    def _tool_summary(self, row: Any, *, full: bool = False) -> dict[str, Any]:
        d = {k: row[k] for k in row.keys() if k not in ("args", "result")}  # noqa: SIM118
        d["ok"] = bool(d["ok"])
        d["truncated"] = bool(d["truncated"])
        if full:
            args = codec.unpack(row["args"])
            d["args"] = json.loads(args) if args else None
            result = codec.unpack(row["result"])
            try:
                d["result"] = json.loads(result) if result else None
            except ValueError:
                d["result"] = result
        return d

    async def timeline(self, call_id: int) -> dict[str, Any] | None:
        """The LLM calls and tool calls of the same chat turn (or all calls linked to the same
        thing), in time order. Calls without a link or turn are their own timeline."""
        call = await self.db.fetchone(
            "SELECT id, link_kind, link_id, turn FROM llm_calls WHERE id = ?", (call_id,)
        )
        if call is None:
            return None
        if call["turn"]:
            cond, params = "turn = ?", [call["turn"]]
        elif call["link_kind"] and call["link_id"]:
            cond, params = "link_kind = ? AND link_id = ?", [call["link_kind"], call["link_id"]]
        else:
            cond, params = "id = ?", [call_id]
        calls = await self.db.fetchall(
            f"SELECT {_SUMMARY_COLS} FROM llm_calls WHERE {cond} ORDER BY ts, id", params
        )
        tools: list[Any] = []
        if call["turn"] or (call["link_kind"] and call["link_id"]):
            tools = await self.db.fetchall(
                "SELECT id, ts, link_kind, link_id, link_step, turn, tool, ok, duration_ms,"
                f" result_size, error, truncated, size_bytes FROM llm_tool_calls WHERE {cond}"
                " ORDER BY ts, id",
                params,
            )
        entries = [{"type": "llm", **self._summary(r)} for r in calls] + [
            {"type": "tool", **self._tool_summary(r)} for r in tools
        ]
        entries.sort(key=lambda e: (e["ts"], 0 if e["type"] == "llm" else 1))
        return {"selected": call_id, "entries": entries}

    async def get_tool(self, tool_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM llm_tool_calls WHERE id = ?", (tool_id,))
        return None if row is None else self._tool_summary(row, full=True)

    async def stats(self) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes, MIN(ts) AS oldest,"
            " SUM(status != 'ok') AS failed FROM llm_calls"
        )
        tools = await self.db.fetchone(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes FROM llm_tool_calls"
        )
        purposes = await self.db.fetchall(
            "SELECT purpose, COUNT(*) AS n FROM llm_calls GROUP BY purpose ORDER BY n DESC"
        )
        assert row is not None and tools is not None
        return {
            "calls": row["n"],
            "failed": row["failed"] or 0,
            "tool_calls": tools["n"],
            "bytes": row["bytes"] + tools["bytes"],
            "oldest": row["oldest"],
            "purposes": [{"purpose": p["purpose"], "count": p["n"]} for p in purposes],
        }

    # ------------------------------------------------------------------ app log

    async def insert_logs(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        scrub = self._scrub()
        await self.db.executemany(
            "INSERT INTO app_log (ts, level, levelno, logger, message, exc) VALUES (?,?,?,?,?,?)",
            [
                (
                    r["ts"],
                    r["level"],
                    r["levelno"],
                    r["logger"],
                    codec.scrub_str(r["message"], scrub),
                    codec.scrub_str(r["exc"], scrub) if r.get("exc") else None,
                )
                for r in records
            ],
        )

    async def list_logs(
        self,
        *,
        limit: int = 200,
        before: int | None = None,
        min_levelno: int = 0,
        logger: str | None = None,
        q: str | None = None,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["levelno >= ?"]
        params: list[Any] = [min_levelno]
        if before is not None:
            clauses.append("id < ?")
            params.append(before)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if logger:
            clauses.append("(logger = ? OR logger LIKE ? ESCAPE '\\')")
            params += [logger, logger.replace("\\", "\\\\").replace("%", "\\%") + ".%"]
        if q:
            clauses.append("(message LIKE ? ESCAPE '\\' OR COALESCE(exc,'') LIKE ? ESCAPE '\\')")
            like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like]
        rows = await self.db.fetchall(
            "SELECT id, ts, level, levelno, logger, message, exc FROM app_log WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id DESC LIMIT ?",
            [*params, limit],
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ retention

    async def prune(self, now: float | None = None) -> dict[str, int]:
        """Delete by age, then oldest-first until the stored bodies fit `max_total_mb`."""
        cfg = self.config
        now = now if now is not None else time.time()
        cutoff = now - cfg.retention_days * 86400
        deleted = {"llm_calls": 0, "llm_tool_calls": 0, "app_log": 0}
        async with self.db.transaction() as conn:
            for table in deleted:
                cur = await conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
                deleted[table] = cur.rowcount

        limit = int(cfg.max_total_mb * 1024 * 1024)
        total = await self._total_bytes()
        if total > limit:
            deleted["llm_calls"] += await self._shed("llm_calls", total - limit)
            floor = await self.db.fetchone("SELECT MIN(ts) FROM llm_calls")
            # Tool calls older than every remaining LLM call belong to deleted calls.
            deleted["llm_tool_calls"] += await self.db.write(
                "DELETE FROM llm_tool_calls WHERE ts < ?",
                (floor[0] if floor and floor[0] else now,),
            )

        extra = await self.db.fetchone("SELECT COUNT(*) FROM app_log")
        if extra and extra[0] > cfg.app_log_max_rows:
            deleted["app_log"] += await self.db.write(
                "DELETE FROM app_log WHERE id <= (SELECT id FROM app_log ORDER BY id DESC"
                " LIMIT 1 OFFSET ?)",
                (cfg.app_log_max_rows,),
            )
        return deleted

    async def _total_bytes(self) -> int:
        row = await self.db.fetchone(
            "SELECT (SELECT COALESCE(SUM(size_bytes),0) FROM llm_calls)"
            " + (SELECT COALESCE(SUM(size_bytes),0) FROM llm_tool_calls)"
        )
        return int(row[0]) if row else 0

    async def _shed(self, table: str, excess: int) -> int:
        """Delete the oldest rows of `table` until at least `excess` bytes are freed."""
        freed = 0
        last_id = 0
        removed_through = 0
        while freed < excess:
            rows = await self.db.fetchall(
                f"SELECT id, size_bytes FROM {table} WHERE id > ? ORDER BY id LIMIT 500",
                (last_id,),
            )
            if not rows:
                break
            for row in rows:
                freed += row["size_bytes"]
                removed_through = row["id"]
                last_id = row["id"]
                if freed >= excess:
                    break
        if not removed_through:
            return 0
        return await self._rowcount(f"DELETE FROM {table} WHERE id <= ?", (removed_through,))
