"""SQLite storage: connection management, migrations, retention pruning, small KV store."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

_MIGRATION_NAME = re.compile(r"^(\d{4})_[\w-]+\.sql$")

# Tables pruned by retention, with the timestamp column used. `actions` is deliberately
# absent: the audit log is permanent.
_PRUNABLE: dict[str, str] = {
    "collector_samples": "ts",
    "reports": "ts",
    "chat_sessions": "updated",  # messages go via ON DELETE CASCADE
}


def load_migrations() -> list[tuple[int, str, str]]:
    """Return [(version, filename, sql)] sorted by version; versions must be 1..N contiguous."""
    found: list[tuple[int, str, str]] = []
    for entry in resources.files("servermon.migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    for expected, (version, name, _) in enumerate(found, start=1):
        if version != expected:
            raise RuntimeError(f"migration {name}: expected version {expected:04d}")
    return found


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
        self._conn = conn
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def schema_version(self) -> int:
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def migrate(self) -> int:
        current = await self.schema_version()
        migrations = load_migrations()
        if current > len(migrations):
            raise RuntimeError(
                f"database schema v{current} is newer than this build (v{len(migrations)})"
            )
        for version, name, sql in migrations[current:]:
            log.info("applying migration %s", name)
            # executescript commits any open transaction first; the BEGIN/COMMIT makes
            # the migration and its version bump atomic.
            await self.conn.executescript(
                f"BEGIN;\n{sql}\n;PRAGMA user_version = {version};\nCOMMIT;"
            )
        return len(migrations)

    # ------------------------------------------------------------------ query helpers

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute a write statement and commit. Returns lastrowid (or rowcount if none)."""
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid if cur.lastrowid else cur.rowcount

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        await self.conn.executemany(sql, rows)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Group several statements; commits on success, rolls back on error."""
        await self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            await self.conn.rollback()
            raise
        await self.conn.commit()

    async def ping(self) -> bool:
        try:
            await self.fetchone("SELECT 1")
            return True
        except Exception:
            log.exception("database ping failed")
            return False

    # ------------------------------------------------------------------ retention

    async def prune(self, retention: timedelta, now: float | None = None) -> dict[str, int]:
        cutoff = (now if now is not None else time.time()) - retention.total_seconds()
        deleted: dict[str, int] = {}
        async with self.transaction() as conn:
            for table, column in _PRUNABLE.items():
                cur = await conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                deleted[table] = cur.rowcount
        if any(deleted.values()):
            log.info("pruned rows older than %s: %s", retention, deleted)
        return deleted

    # ------------------------------------------------------------------ kv

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated = excluded.updated",
            (key, json.dumps(value), time.time()),
        )
