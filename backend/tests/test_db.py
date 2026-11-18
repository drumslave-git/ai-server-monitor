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
    assert {"chat_sessions", "chat_messages", "actions", "kv"} <= tables
    assert not {"collector_samples", "reports", "prom_queries"} & tables
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
            "INSERT INTO telegram_batch_messages (batch_id, chat_id, message_id, created_ts) "
            "VALUES (?, 1, 2, ?)",
            (int(ts), ts),
        )

    deleted = await db.prune(timedelta(days=30), now=now)

    assert deleted == {
        "chat_messages": 1,
        "chat_sessions": 1,
        "telegram_batch_messages": 1,
        "job_runs": 0,
    }
    assert (await db.fetchone("SELECT COUNT(*) FROM chat_messages"))[0] == 0  # cascade
    assert (await db.fetchone("SELECT COUNT(*) FROM actions"))[0] == 2  # audit log kept


async def test_prune_old_chat_messages_inside_a_live_session(db: Database) -> None:
    now = time.time()
    day = 86400
    await db.execute(
        "INSERT INTO chat_sessions (id, created, updated, channel) "
        "VALUES ('live', ?, ?, 'telegram')",
        (now - 90 * day, now - 60),
    )
    for ts, text in ((now - 90 * day, "ancient"), (now - 40 * day, "old"), (now - 60, "fresh")):
        await db.execute(
            "INSERT INTO chat_messages (session_id, ts, role, content) "
            "VALUES ('live', ?, 'user', ?)",
            (ts, text),
        )
    # a session whose messages are all old, and an empty old one; a brand-new empty one stays
    await db.execute(
        "INSERT INTO chat_sessions (id, created, updated, channel) VALUES ('dead', ?, ?, 'web')",
        (now - 50 * day, now - 50 * day),
    )
    await db.execute(
        "INSERT INTO chat_messages (session_id, ts, role, content) VALUES ('dead', ?, 'user', 'x')",
        (now - 50 * day,),
    )
    await db.execute(
        "INSERT INTO chat_sessions (id, created, updated, channel) VALUES ('empty', ?, ?, 'web')",
        (now - 45 * day, now - 45 * day),
    )
    await db.execute(
        "INSERT INTO chat_sessions (id, created, updated, channel) VALUES ('newborn', ?, ?, 'web')",
        (now - 5, now - 5),
    )

    deleted = await db.prune(timedelta(days=30), now=now)

    assert deleted["chat_messages"] == 3  # ancient, old, and dead's one
    texts = [r[0] for r in await db.fetchall("SELECT content FROM chat_messages")]
    assert texts == ["fresh"]
    ids = {r[0] for r in await db.fetchall("SELECT id FROM chat_sessions")}
    assert ids == {"live", "newborn"}


async def test_kv_roundtrip(db: Database) -> None:
    assert await db.kv_get("k", default={"d": 1}) == {"d": 1}
    await db.kv_set("k", {"a": [1, 2]})
    await db.kv_set("k", {"a": [3]})
    assert await db.kv_get("k") == {"a": [3]}


async def test_transaction_rollback(db: Database) -> None:
    try:
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO kv (key, value, updated) VALUES ('k', '1', 1)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert (await db.fetchone("SELECT COUNT(*) FROM kv WHERE key = 'k'"))[0] == 0
    assert await db.ping()


async def test_database_at_schema_4_migrates_to_latest(tmp_path: Path) -> None:
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
        assert await db.schema_version() == 21
        info = await db.fetchall("PRAGMA table_info(telegram_batch_messages)")
        cols = {r["name"] for r in info}
        assert cols == {"batch_id", "chat_id", "message_id", "created_ts"}
        assert await db.kv_get("keep") == 1
    finally:
        await db.close()


async def test_database_at_schema_5_loses_the_prometheus_query_table(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "v5.db"
    conn = sqlite3.connect(path)
    for _, _, sql in load_migrations()[:5]:
        conn.executescript(sql)
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await db.schema_version() == 21
        assert not await db.fetchall("PRAGMA table_info(prom_queries)")
    finally:
        await db.close()


async def test_schema_18_settings_lose_the_mcp_token_budget(tmp_path: Path) -> None:
    import json
    import sqlite3

    path = tmp_path / "v18.db"
    conn = sqlite3.connect(path)
    for _, _, sql in load_migrations()[:18]:
        conn.executescript(sql)
    conn.execute("PRAGMA user_version = 18")
    stored = {"mcp": {"servers": [], "max_inline_tool_tokens": 1200, "dry_run": True}}
    for key in ("settings", "settings.backup.1"):
        conn.execute(
            "INSERT INTO kv (key, value, updated) VALUES (?, ?, 1)", (key, json.dumps(stored))
        )
    conn.commit()
    conn.close()

    db = Database(path)
    await db.connect()
    try:
        for key in ("settings", "settings.backup.1"):
            assert await db.kv_get(key) == {"mcp": {"servers": [], "dry_run": True}}
    finally:
        await db.close()
