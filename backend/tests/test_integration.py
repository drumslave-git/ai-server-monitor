"""Cross-lane wiring: migrations, action layer -> event bus/metrics, *arr -> digest."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any

import httpx

from ai_server_monitor.collectors import HostEnv
from ai_server_monitor.collectors.arr import ArrCollector
from ai_server_monitor.collectors.base import CollectorResult
from ai_server_monitor.config import Bootstrap, ReportSettings
from ai_server_monitor.db import Database, load_migrations
from ai_server_monitor.digest import build_digest
from ai_server_monitor.main import create_app

from .arrfakes import HASH_A, FakeArrWorld, make_settings, torrent
from .live_server import live_server
from .test_digest import NOW, digest_of, entry, snapshot, titles
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
    ]
    names = {e.name for e in resources.files("ai_server_monitor.migrations").iterdir()}
    assert not any("placeholder" in n for n in names)


async def test_fresh_database_migrates_to_latest(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    await db.connect()
    try:
        assert await db.schema_version() == 6
        report_cols = {r["name"] for r in await db.fetchall("PRAGMA table_info(reports)")}
        assert {"status", "summary", "digest_hash", "parsed"} <= report_cols
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
    conn.execute("INSERT INTO reports (ts, markdown) VALUES (1, 'old report')")
    conn.execute("INSERT INTO kv (key, value, updated) VALUES ('keep', '1', 1)")
    conn.commit()
    conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await db.schema_version() == 6
        old = await db.fetchone("SELECT status, markdown, parsed FROM reports")
        assert old is not None and old["status"] == "ok" and old["markdown"] == "old report"
        assert await db.kv_get("keep") == 1  # data survives
        await db.execute(
            "INSERT INTO actions (ts, updated, action, policy, status, source) "
            "VALUES (1, 1, 'a', 'approve', 'pending', 's')"
        )
    finally:
        await db.close()


# --------------------------------------------------------------------------- digest


def arr_entry(**over: Any) -> dict[str, Any]:
    data = {
        "summary": {
            "instances": 3,
            "unreachable": ["sonarr/sonarr"],
            "queue_total": 12,
            "queue_problems": 2,
            "wanted_missing": 40,
            "wanted_cutoff_unmet": 5,
            "indexers_failing": 1,
            "stalled_candidates": 3,
        },
        "findings": [
            {"severity": "crit", "source": "sonarr/sonarr", "message": "unreachable: refused"},
            {"severity": "warn", "source": "qbittorrent", "message": "3 stalled/broken torrent(s)"},
        ],
        "apps": [],
    }
    data.update(over)
    return entry(data)


async def test_arr_section_and_flags(db: Database) -> None:
    d = await digest_of(db, snapshot(arr=arr_entry()))
    assert "## *arr and downloads" in d.text
    assert "UNREACHABLE: sonarr/sonarr" in d.text and "queues 12 items (2 with problems)" in d.text
    assert "[WARN] qbittorrent: 3 stalled/broken torrent(s)" in d.text
    found = titles(d)
    # The collector's "crit" is capped: a broken *arr app is not a critical server.
    assert ("warn", "sonarr/sonarr: unreachable: refused") in found
    assert ("warn", "qbittorrent: 3 stalled/broken torrent(s)") in found
    assert not any(sev == "crit" for sev, _ in found)
    section = next(s for s in d.sections if s["key"] == "arr")
    assert section["priority"] < 50  # dropped before the host sections


async def test_no_arr_section_when_arr_is_not_configured(db: Database) -> None:
    unconfigured = {"state": "unavailable", "reason": "no *arr app configured", "result": None}
    d = await digest_of(db, snapshot(arr=unconfigured))
    assert "arr: not available (no *arr app configured)" in d.text
    assert not any(s["key"] == "arr" for s in d.sections)
    gone = await digest_of(db, snapshot())
    assert not any(s["key"] == "arr" for s in gone.sections)


async def test_arr_section_is_dropped_first_under_a_tight_budget(db: Database) -> None:
    many = [
        {"severity": "warn", "source": f"radarr/r{i}", "message": "queue item stuck " + "x" * 80}
        for i in range(8)
    ]
    snap = snapshot(arr=arr_entry(findings=many))
    full = await build_digest(snap, db, ReportSettings(), now=NOW)
    tight = await build_digest(snap, db, ReportSettings(digest_token_budget=500), now=NOW)
    full_arr = next(s for s in full.sections if s["key"] == "arr")
    tight_arr = next(s for s in tight.sections if s["key"] == "arr")
    assert full_arr["kept_lines"] == full_arr["lines"]
    assert tight_arr["kept_lines"] <= full_arr["kept_lines"]
    assert "## Rule-based flags" in tight.text


async def test_pending_approvals_flag(db: Database) -> None:
    await db.execute(
        "INSERT INTO actions (ts, updated, action, policy, status, source) "
        "VALUES (1, 1, 'qbit_delete_torrent', 'approve', 'pending', 'scheduler')"
    )
    d = await digest_of(db, snapshot(arr=arr_entry()))
    assert ("info", "1 action(s) waiting for your approval") in titles(d)
    assert "1 action(s) waiting for approval" in d.text


# --------------------------------------------------------------------------- end to end


def test_approval_request_reaches_the_stream_and_the_metrics(bootstrap: Bootstrap) -> None:
    world = FakeArrWorld()
    world.torrents = {HASH_A: torrent(HASH_A, "Stuck.Movie.2026", "stalledDL")}
    settings = make_settings({"stalled_cleanup": {"policy": "approve"}})
    app = create_app(
        bootstrap, collector_registry={}, seed_settings=settings, arr_transport=world.transport()
    )
    holder: dict[str, Any] = {}

    @app.get("/_loop", include_in_schema=False)
    async def _loop() -> dict[str, bool]:  # captures the server's event loop for the test
        holder["loop"] = asyncio.get_running_loop()
        return {"ok": True}

    with live_server(app) as base, httpx.Client(base_url=base, timeout=15) as client:
        client.get("/_loop")
        ctx = app.state.ctx

        # Real *arr data (from the fake world) as the arr collector would have produced it.
        collector = ArrCollector(settings.collectors.arr, HostEnv(), transport=world.transport())
        collector.app_settings = settings
        data = asyncio.run_coroutine_threadsafe(collector.collect(), holder["loop"]).result(10)
        ctx.collectors.latest["arr"] = CollectorResult("arr", time.time(), ok=True, data=data)

        def request() -> dict[str, Any]:
            time.sleep(0.4)  # the stream below is opened first
            return asyncio.run_coroutine_threadsafe(
                ctx.actions.request(
                    "qbit_delete_torrent",
                    {"hash": HASH_A, "title": "Stuck.Movie.2026"},
                    "scheduler",
                    reason="stalled",
                ),
                holder["loop"],
            ).result(10)

        result: dict[str, Any] = {}
        threading.Thread(target=lambda: result.update(request())).start()
        with client.stream("GET", "/api/stream", params={"types": "approval,action"}) as resp:
            frames = read_sse(resp, until="approval.pending")
        pending = frames[-1]
        assert pending["event"] == "approval.pending"
        assert pending["data"]["action"] == "qbit_delete_torrent"
        assert pending["data"]["status"] == "pending" and pending["data"]["id"] == result["id"]
        assert "args" not in pending["data"] and "result" not in pending["data"]  # compact

        scrape = client.get("/metrics").text
        assert "asm_pending_approvals 1\n" in scrape
        assert 'asm_actions_total{action="qbit_delete_torrent",result="pending"} 1' in scrape
        assert 'asm_arr_up{app="qbittorrent"} 1' in scrape
        assert 'asm_arr_up{app="radarr"} 1' in scrape
        assert 'asm_arr_stalled_downloads{app="qbittorrent"}' in scrape
        assert 'asm_qbittorrent_torrents{state="stalledDL"} 1' in scrape
        assert 'asm_arr_queue_size{app="radarr"} 0' in scrape
        assert client.get("/api/status").json()["pending_approvals"] == 1

        # Approve over HTTP: decided + result events, and the gauge drops back.
        with client.stream("GET", "/api/stream", params={"types": "approval,action"}) as resp:
            threading.Thread(
                target=lambda: (
                    time.sleep(0.4),
                    httpx.post(
                        f"{base}/api/approvals/{result['id']}", json={"decision": "approve"}
                    ),
                )
            ).start()
            frames = read_sse(resp, until="action.result")
        events = [(f["event"], f["data"]) for f in frames if "event" in f]
        assert [e for e, _ in events] == ["approval.decided", "action.result"]
        assert events[0][1]["decision"] == "approved" and events[0][1]["decided_by"] == "ui"
        assert events[1][1]["status"] == "executed"
        after = client.get("/metrics").text
        assert "asm_pending_approvals 0\n" in after
        assert 'asm_actions_total{action="qbit_delete_torrent",result="executed"} 1' in after
        assert client.get("/api/status").json()["pending_approvals"] == 0
