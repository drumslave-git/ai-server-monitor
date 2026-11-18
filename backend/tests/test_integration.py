"""Cross-lane wiring: migrations, action layer -> event bus/metrics."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from ai_server_monitor.config import Bootstrap
from ai_server_monitor.db import Database, load_migrations
from ai_server_monitor.main import create_app

from .actionfakes import NAME, FakeAction, settings_with
from .live_server import live_server
from .test_stream import read_sse

# --------------------------------------------------------------------------- migrations


def test_migrations_are_contiguous_and_complete() -> None:
    assert [(v, name) for v, name, _ in load_migrations()] == [
        (1, "0001_initial.sql"),
        (2, "0002_reports.sql"),
        (3, "0003_actions.sql"),
        (4, "0004_chat.sql"),
        (5, "0005_telegram.sql"),
        (6, "0006_prom_queries.sql"),
        (7, "0007_llm_trace.sql"),
        (8, "0008_bench.sql"),
        (9, "0009_chat_message_ts.sql"),
        (10, "0010_approval_batches.sql"),
        (11, "0011_bench_hardware.sql"),
        (12, "0012_bench_thinking.sql"),
        (13, "0013_remove_arr_bench_api.sql"),
        (14, "0014_remove_collection.sql"),
        (15, "0015_jobs.sql"),
        (16, "0016_telegram_summary.sql"),
        (17, "0017_chat_pending_runs.sql"),
        (18, "0018_summary_topics.sql"),
        (19, "0019_mcp_on_pydantic_ai.sql"),
        (20, "0020_mcp_domains.sql"),
    ]
    names = {e.name for e in resources.files("ai_server_monitor.migrations").iterdir()}
    assert not any("placeholder" in n for n in names)


async def test_fresh_database_migrates_to_latest(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    await db.connect()
    try:
        assert await db.schema_version() == 20
        tables = {
            r["name"]
            for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"actions", "chat_sessions", "llm_calls", "jobs", "job_runs"} <= tables
        assert not {"collector_samples", "reports", "prom_queries"} & tables
        action_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(actions)")}
        assert {"status", "policy_key"} <= action_cols or "status" in action_cols
    finally:
        await db.close()


async def test_database_at_the_old_main_schema_upgrades(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    first = load_migrations()[0][2]
    conn = sqlite3.connect(path)  # what v0.3.2 (user_version 1) left behind
    conn.executescript(first)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO reports (ts, markdown) VALUES (1, 'old report')")  # dropped later
    conn.execute("INSERT INTO kv (key, value, updated) VALUES ('keep', '1', 1)")
    conn.commit()
    conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await db.schema_version() == 20
        assert await db.kv_get("keep") == 1  # data survives
        await db.execute(
            "INSERT INTO actions (ts, updated, action, policy, status, source) "
            "VALUES (1, 1, 'a', 'approve', 'pending', 's')"
        )
    finally:
        await db.close()


# --------------------------------------------------------------------------- end to end


def metrics_with(client: Any, line: str, timeout: float = 5.0) -> str:
    """The scrape once it contains `line` (the metrics follow the events asynchronously)."""
    deadline = time.monotonic() + timeout
    while True:
        text = client.get("/metrics").text
        if line in text or time.monotonic() > deadline:
            return text
        time.sleep(0.05)


def test_approval_request_reaches_the_stream_and_the_metrics(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    app = create_app(bootstrap, seed_settings=settings_with("1h"))
    holder: dict[str, Any] = {}

    @app.get("/_loop", include_in_schema=False)
    async def _loop() -> dict[str, bool]:  # captures the server's event loop for the test
        holder["loop"] = asyncio.get_running_loop()
        return {"ok": True}

    with live_server(app) as base, httpx.Client(base_url=base, timeout=15) as client:
        client.get("/_loop")
        ctx = app.state.ctx

        def request() -> dict[str, Any]:
            time.sleep(0.4)  # the stream below is opened first
            return asyncio.run_coroutine_threadsafe(
                ctx.actions.request(
                    NAME,
                    {"target": "alpha", "title": "Alpha"},
                    "scheduler",
                    reason="stalled",
                ),
                holder["loop"],
            ).result(10)

        result: dict[str, Any] = {}
        requester = threading.Thread(target=lambda: result.update(request()))
        requester.start()
        with client.stream("GET", "/api/stream", params={"types": "approval,action"}) as resp:
            frames = read_sse(resp, until="approval.pending")
        # The event is published inside `request()`, before it returns its record: wait for it.
        requester.join(10)
        assert "batch_id" in result, "the request did not finish"
        pending = frames[-1]
        assert pending["event"] == "approval.pending"
        assert pending["data"]["action"] == NAME
        assert pending["data"]["pending"] == 1 and pending["data"]["id"] == result["batch_id"]
        assert "items" not in pending["data"] and "args" not in pending["data"]  # compact

        scrape = metrics_with(client, "asm_pending_approvals 1.0\n")
        assert "asm_pending_approval_batches 1.0\n" in scrape
        assert 'asm_actions_total{action="fake_write",result="pending"} 1.0' in scrape
        assert client.get("/api/status").json()["pending_approvals"] == 1

        # Approve over HTTP: decided + result events, and the gauge drops back.
        with client.stream("GET", "/api/stream", params={"types": "approval,action"}) as resp:
            threading.Thread(
                target=lambda: (
                    time.sleep(0.4),
                    httpx.post(
                        f"{base}/api/approvals/{result['batch_id']}", json={"decision": "approve"}
                    ),
                )
            ).start()
            frames = read_sse(resp, until="approval.decided")
        events = [(f["event"], f["data"]) for f in frames if "event" in f]
        assert [e for e, _ in events] == ["action.result", "approval.decided"]
        assert events[0][1]["status"] == "executed"
        assert events[1][1]["decision"] == "approve" and events[1][1]["decided_by"] == "ui"
        assert events[1][1]["counts"] == {"executed": 1}
        after = metrics_with(client, "asm_pending_approvals 0.0\n")
        assert 'asm_actions_total{action="fake_write",result="executed"} 1.0' in after
        assert "asm_pending_approval_batches 0.0\n" in after
        assert fake_action.calls == ["alpha"]
        assert client.get("/api/status").json()["pending_approvals"] == 0
