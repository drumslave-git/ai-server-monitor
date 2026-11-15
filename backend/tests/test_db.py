from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from ai_server_monitor.db import Database, load_migrations


async def test_migrations_apply_and_are_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    await db.connect()
    version = await db.schema_version()
    assert version == len(load_migrations()) >= 1
    tables = {r["name"] for r in await db.fetchall("SELECT name FROM sqlite_master")}
    assert {"collector_samples", "reports", "chat_sessions", "chat_messages", "actions", "kv"} <= (
        tables
    )
    journal = await db.fetchone("PRAGMA journal_mode")
    assert journal[0] == "wal"
    await db.close()

    # Reconnect: nothing re-applied, no error.
    db2 = Database(tmp_path / "x.db")
    await db2.connect()
    assert await db2.schema_version() == version
    await db2.close()


async def test_prune_respects_retention_and_keeps_actions(db: Database) -> None:
    now = time.time()
    old, recent = now - 40 * 86400, now - 86400
    for ts in (old, recent):
        await db.execute(
            "INSERT INTO collector_samples (ts, collector, data) VALUES (?, 'system', '{}')", (ts,)
        )
        await db.execute("INSERT INTO reports (ts, markdown) VALUES (?, 'r')", (ts,))
        await db.execute(
            "INSERT INTO actions (ts, updated, action, policy, status, source) "
            "VALUES (?, ?, 'x', 'auto', 'executed', 'scheduler')",
            (ts, ts),
        )
    await db.execute(
        "INSERT INTO chat_sessions (id, created, updated, channel) VALUES ('s1', ?, ?, 'web')",
        (old, old),
    )
    await db.execute(
        "INSERT INTO chat_messages (session_id, ts, role, content) VALUES ('s1', ?, 'user', 'hi')",
        (old,),
    )

    for ts in (old, recent):
        await db.execute(
            "INSERT INTO telegram_approval_messages (approval_id, chat_id, message_id, created_ts) "
            "VALUES (?, 1, 2, ?)",
            (int(ts), ts),
        )

    deleted = await db.prune(timedelta(days=30), now=now)

    assert deleted == {
        "collector_samples": 1,
        "reports": 1,
        "chat_sessions": 1,
        "telegram_approval_messages": 1,
    }
    assert (await db.fetchone("SELECT COUNT(*) FROM collector_samples"))[0] == 1
    assert (await db.fetchone("SELECT COUNT(*) FROM chat_messages"))[0] == 0  # cascade
    assert (await db.fetchone("SELECT COUNT(*) FROM actions"))[0] == 2  # audit log kept


async def test_kv_roundtrip(db: Database) -> None:
    assert await db.kv_get("k", default={"d": 1}) == {"d": 1}
    await db.kv_set("k", {"a": [1, 2]})
    await db.kv_set("k", {"a": [3]})
    assert await db.kv_get("k") == {"a": [3]}


async def test_transaction_rollback(db: Database) -> None:
    try:
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO reports (ts, markdown) VALUES (1, 'x')")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert (await db.fetchone("SELECT COUNT(*) FROM reports"))[0] == 0
    assert await db.ping()


async def test_database_at_schema_4_migrates_to_5(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "v4.db"
    conn = sqlite3.connect(path)
    for _, _, sql in load_migrations()[:4]:
        conn.executescript(sql)
    conn.execute("PRAGMA user_version = 4")
    conn.execute("INSERT INTO kv (key, value, updated) VALUES ('keep', '1', 1)")
    conn.commit()
    conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await db.schema_version() == 5
        info = await db.fetchall("PRAGMA table_info(telegram_approval_messages)")
        cols = {r["name"] for r in info}
        assert cols == {"approval_id", "chat_id", "message_id", "created_ts"}
        assert await db.kv_get("keep") == 1
    finally:
        await db.close()
