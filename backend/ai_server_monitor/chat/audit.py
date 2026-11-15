"""Audit trail of what the chat did on its own: commands run on the host, API calls made
through generic tools, MCP tool calls. One list for all of them, newest last, kept in `kv`
(the last KEEP entries; no table, so any part of the app can add a kind without a migration).

    await record_call(db, "shell", session_id, cmd="ps aux", exit=0, seconds=0.02, bytes=1200)
    hook = audit_hook(db, "api")        # for components with an `on_call(record)` callback

Entries: `{"ts", "kind", "session", ...fields}`. Never raises: auditing must not break an answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..db import Database

log = logging.getLogger(__name__)

AUDIT_KEY = "tool_audit"
KEEP = 300
_lock = asyncio.Lock()


async def record_call(db: Database, kind: str, session: str | None, **fields: Any) -> None:
    entry = {"ts": round(time.time(), 1), "kind": kind, "session": session, **fields}
    log.info("chat audit: %s", entry)
    try:
        async with _lock:
            rows = await db.kv_get(AUDIT_KEY, [])
            rows.append(entry)
            await db.kv_set(AUDIT_KEY, rows[-KEEP:])
    except Exception:
        log.exception("could not write the chat audit entry")


async def recent(db: Database, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
    """The newest `limit` entries (newest first), optionally of one kind."""
    rows: list[dict[str, Any]] = await db.kv_get(AUDIT_KEY, [])
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    return rows[::-1][:limit]


def audit_hook(
    db: Database, kind: str, session: str | None = None
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """An `on_call(record)` callback that files each record under `kind`."""

    async def hook(record: dict[str, Any]) -> None:
        await record_call(db, kind, session, **record)

    return hook
