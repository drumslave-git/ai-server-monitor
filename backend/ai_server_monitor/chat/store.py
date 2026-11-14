"""Chat persistence: sessions and their messages (including tool calls and results)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..db import Database

TITLE_LENGTH = 60


def _message(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("tool_calls", "meta"):
        out[key] = json.loads(out[key]) if out.get(key) else None
    return out


class ChatStore:
    def __init__(self, db: Database):
        self.db = db

    async def create_session(self, title: str, channel: str = "web") -> str:
        session_id = uuid.uuid4().hex
        now = time.time()
        await self.db.execute(
            "INSERT INTO chat_sessions (id, created, updated, channel, title) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, now, now, channel, title[:TITLE_LENGTH]),
        )
        return session_id

    async def session_exists(self, session_id: str) -> bool:
        row = await self.db.fetchone("SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,))
        return row is not None

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str | None,
        *,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        now = time.time()
        message_id = await self.db.execute(
            "INSERT INTO chat_messages (session_id, ts, role, content, tool_calls, tool_call_id, "
            "name, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                now,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                name,
                json.dumps(meta) if meta else None,
            ),
        )
        await self.db.execute(
            "UPDATE chat_sessions SET updated = ? WHERE id = ?", (now, session_id)
        )
        return message_id

    async def history(self, session_id: str, limit: int) -> list[dict[str, str]]:
        """The last `limit` user/assistant texts as LLM messages (tool traffic of earlier
        turns is kept in the database for display but not replayed to the model)."""
        if limit <= 0:
            return []
        rows = await self.db.fetchall(
            "SELECT role, content FROM chat_messages WHERE session_id = ? "
            "AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != '' "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def list_sessions(
        self, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        total = await self.db.fetchone("SELECT COUNT(*) AS n FROM chat_sessions")
        rows = await self.db.fetchall(
            "SELECT s.id, s.created, s.updated, s.channel, s.title, "
            "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id = s.id "
            " AND m.role IN ('user', 'assistant') "
            " AND m.content IS NOT NULL AND m.content != '') AS messages "
            "FROM chat_sessions s ORDER BY s.updated DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return [dict(r) for r in rows], int(total["n"]) if total else 0

    async def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT id, created, updated, channel, title FROM chat_sessions WHERE id = ?",
            (session_id,),
        )
        if row is None:
            return None
        messages = await self.db.fetchall(
            "SELECT id, ts, role, content, tool_calls, tool_call_id, name, meta "
            "FROM chat_messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        )
        return {**dict(row), "messages": [_message(m) for m in messages]}

    async def delete_session(self, session_id: str) -> bool:
        cursor = await self.db.conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
        await self.db.conn.commit()  # messages go with it (ON DELETE CASCADE)
        return cursor.rowcount > 0
