"""Several job runs share the one SQLite connection; their transactions must not interleave.

Live 0.7.2: the hourly jobs fire in the same second and most failed with
"cannot start a transaction within a transaction"."""

from __future__ import annotations

import asyncio

from ai_server_monitor.db import Database
from ai_server_monitor.jobs.store import JobStore

SLUGS = ("journal", "systemd", "summary", "docker", "resources")


async def test_concurrent_job_runs_do_not_interleave_transactions(db: Database) -> None:
    store = JobStore(db)
    await store.seed_builtins()

    async def run(slug: str) -> int:
        run_id = await store.start_run(slug, "schedule", None)
        await store.finish_run(run_id, status="ok", summary=slug)
        return run_id

    ids = await asyncio.gather(*(run(slug) for slug in SLUGS))

    assert len(set(ids)) == len(SLUGS)
    rows = await db.fetchall("SELECT job_slug, status, summary FROM job_runs ORDER BY id")
    assert sorted((r["job_slug"], r["status"], r["summary"]) for r in rows) == sorted(
        (slug, "ok", slug) for slug in SLUGS
    )


async def test_rollback_does_not_undo_a_concurrent_write(db: Database) -> None:
    """A failing transaction rolls back only its own statements, not another task's write."""

    async def failing() -> None:
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO kv (key, value, updated) VALUES ('a', '1', 0)")
            await asyncio.sleep(0)
            raise RuntimeError("boom")

    async def writer() -> None:
        await db.kv_set("b", 2)

    results = await asyncio.gather(failing(), writer(), return_exceptions=True)

    assert isinstance(results[0], RuntimeError) and str(results[0]) == "boom"
    assert results[1] is None
    assert await db.kv_get("a") is None
    assert await db.kv_get("b") == 2
