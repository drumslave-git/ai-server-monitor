"""Chat persistence: sessions and their messages (including tool calls and results)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..db import Database

TITLE_LENGTH = 60


def make_title(message: str, limit: int = TITLE_LENGTH) -> str:
    """One-line session title: the message with whitespace collapsed, cut at a word boundary."""
    text = " ".join(message.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    if text[limit - 1] != " " and " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


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
            (session_id, now, now, channel, make_title(title)),
        )
        return session_id

    async def set_title(self, session_id: str, title: str) -> None:
        # (does not touch `updated`: a title is not new activity)
        await self.db.execute(
            "UPDATE chat_sessions SET title = ? WHERE id = ?", (title[:200], session_id)
        )

    async def session_exists(self, session_id: str) -> bool:
        row = await self.db.fetchone("SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,))
        return row is not None

    async def session_updated(self, session_id: str) -> float | None:
        """When the session last got a message (None = no such session)."""
        row = await self.db.fetchone(
            "SELECT updated FROM chat_sessions WHERE id = ?", (session_id,)
        )
        return float(row["updated"]) if row else None

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

    async def update_message(self, message_id: int, content: str, meta: dict[str, Any]) -> None:
        await self.db.execute(
            "UPDATE chat_messages SET content = ?, meta = ? WHERE id = ?",
            (content, json.dumps(meta), message_id),
        )

    async def waiting_note(self, session_id: str, action_ids: set[int]) -> dict[str, Any] | None:
        """The newest assistant message of the session that waits for one of these actions."""
        rows = await self.db.fetchall(
            "SELECT * FROM chat_messages WHERE session_id = ? AND role = 'assistant' "
            "AND meta LIKE '%waiting_for%' ORDER BY id DESC LIMIT 20",
            (session_id,),
        )
        for row in rows:
            message = _message(row)
            if set((message["meta"] or {}).get("waiting_for") or []) & action_ids:
                return message
        return None

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

    async def loaded_domains(self, session_id: str) -> list[str]:
        """The MCP domains the model loaded (`load_capability`) in earlier turns of this
        conversation (turn log: assistant message meta `mcp_domains.loaded`)."""
        rows = await self.db.fetchall(
            "SELECT DISTINCT value AS name FROM chat_messages, "
            "json_each(json_extract(meta, '$.mcp_domains.loaded')) "
            "WHERE session_id = ? AND role = 'assistant' AND json_valid(meta)",
            (session_id,),
        )
        return sorted(r["name"] for r in rows)

    async def used_tools(self, session_id: str, limit: int = 60) -> list[str]:
        """Names of the tools called in this conversation (newest results first, distinct)."""
        rows = await self.db.fetchall(
            "SELECT name FROM chat_messages WHERE session_id = ? AND role = 'tool' "
            "AND name IS NOT NULL ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        return list(dict.fromkeys(r["name"] for r in rows))

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
        # Messages go with it (ON DELETE CASCADE).
        return await self.db.write("DELETE FROM chat_sessions WHERE id = ?", (session_id,)) > 0
