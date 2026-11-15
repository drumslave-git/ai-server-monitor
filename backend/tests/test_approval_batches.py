"""Approvals are batches: one approval per run and action type, items decided together or one
by one, caps applied at execution, expiry for the whole batch, and the upgrade from the
one-approval-per-item layout."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.actions import ActionService, Conflict, NotFound
from ai_server_monitor.arr.automations import AutomationEnv, search, stalled, triage
from ai_server_monitor.config import Bootstrap
from ai_server_monitor.db import Database, load_migrations
from ai_server_monitor.main import create_app

from .arrfakes import HASH_B, FakeArrWorld, make_settings, torrent

HOURS = 3600
REMOVE = "arr_remove_queue_item"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def iso_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def blocked(item_id: int, title: str | None = None, **extra: Any) -> dict[str, Any]:
    """A Sonarr queue item that finished but cannot be imported."""
    return {
        "id": item_id,
        "title": title or f"Show.S01E{item_id:02d}.1080p",
        "status": "completed",
        "trackedDownloadStatus": "warning",
        "trackedDownloadState": "importBlocked",
        "downloadId": None,
        "added": iso_ago(30),
        "statusMessages": [{"title": "x", "messages": ["No files found are eligible"]}],
        **extra,
    }


@pytest.fixture
def world() -> FakeArrWorld:
    return FakeArrWorld()


@pytest.fixture
def clock() -> Clock:
    return Clock()


Events = list[tuple[str, dict[str, Any]]]


def make_env(
    db: Database,
    world: FakeArrWorld,
    clock: Clock | None = None,
    *,
    policy: str = "approve",
    cap: int = 10,
    **arr: Any,
) -> tuple[AutomationEnv, Events]:
    actions = {
        "stalled_cleanup": {"policy": policy, "stalled_after": "6h", "max_per_hour": cap},
        "missing_search": {"policy": policy, "batch": 4},
        "cutoff_search": {"policy": "suggest", "batch": 2},
        "indexer_triage": {"policy": policy},
        "approval_ttl": "1h",
    }
    settings = make_settings(actions, **arr)
    events: Events = []
    kw: dict[str, Any] = {"clock": clock} if clock else {}
    service = ActionService(
        db,
        settings,
        transport=world.transport(),
        notifiers=[lambda event, record: events.append((event, record))],
        **kw,
    )
    return AutomationEnv(service, settings, **kw), events


def names(events: Events) -> list[str]:
    return [e for e, _ in events]


# --------------------------------------------------------------------------- automations


async def test_stalled_cleanup_files_one_batch_for_forty_import_blocked_items(
    db: Database, world: FakeArrWorld
) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 41)]
    world.torrents = {
        HASH_B: torrent(HASH_B, "Hand.Added", "stalledDL", 0.2, inactive_s=9 * HOURS),
    }
    env, events = make_env(db, world)

    summary = await stalled.run(env)

    # 40 removals for approval + the hand-added torrent that is only ever suggested.
    assert summary == {"requested": 41, "outcomes": {"pending": 40, "suggested": 1}}
    [batch] = await env.service.pending_batches()
    assert batch["title"] == (
        "Stalled cleanup: remove 40 queue items from sonarr (blocklist + re-search)"
    )
    assert batch["total"] == 40 and batch["pending"] == 40 and batch["counts"] == {"pending": 40}
    assert all(i["batch_id"] == batch["id"] and i["status"] == "pending" for i in batch["items"])
    assert batch["source"] == "scheduler" and batch["label"] == "Stalled cleanup"
    # ONE notification for the whole run (one Telegram message), and nothing was sent to sonarr.
    assert names(events).count("approval.pending") == 1
    assert names(events).count("approval.updated") == 0
    assert world.calls("sonarr.test", "DELETE") == []
    suggestion = (await env.service.list_actions(status="suggested"))["items"][0]
    assert suggestion["action"] == "qbit_delete_torrent" and suggestion["batch_id"] is None

    # A second run does not ask again for items waiting in the open batch.
    assert (await stalled.run(env))["requested"] == 0
    assert len(await env.service.pending_batches()) == 1
    assert (await env.service.pending_batches())[0]["total"] == 40


async def test_approve_all_executes_every_item_and_reports_per_item(
    db: Database, world: FakeArrWorld
) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 6)]
    env, events = make_env(db, world, cap=0)  # no hourly cap
    await stalled.run(env)
    [batch] = await env.service.pending_batches()

    view = await env.service.decide_batch(batch["id"], "approve", decided_by="ui")

    assert view["counts"] == {"executed": 5} and view["outcome"] == "5 executed"
    assert view["pending"] == 0 and view["decided"] == 5 and view["decision"] == "approve"
    assert all(i["decided_by"] == "ui" and i["result"]["queue_id"] for i in view["items"])
    assert len(world.calls("sonarr.test", "DELETE")) == 5
    assert names(events).count("action.result") == 5  # the audit stays per item
    assert names(events)[-1] == "approval.decided"
    assert await env.service.pending_batches() == []
    with pytest.raises(Conflict):
        await env.service.decide_batch(batch["id"], "approve")


async def test_subset_approval_leaves_the_rest_pending(db: Database, world: FakeArrWorld) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 5)]
    env, _ = make_env(db, world, cap=0)
    await stalled.run(env)
    [batch] = await env.service.pending_batches()
    ids = [i["id"] for i in batch["items"]]

    view = await env.service.decide_batch(batch["id"], "approve", ids[:2])
    assert view["counts"] == {"executed": 2, "pending": 2} and view["pending"] == 2
    assert len(world.calls("sonarr.test", "DELETE")) == 2
    still = await env.service.pending_batches()
    assert [b["id"] for b in still] == [batch["id"]]  # the card stays, with 2 items left

    # Deciding an item twice does not redo it; the others can still be rejected as a group.
    with pytest.raises(Conflict):
        await env.service.decide_batch(batch["id"], "approve", ids[:2])
    view = await env.service.decide_batch(batch["id"], "reject", ids[1:], decided_by="telegram:bo")
    assert view["counts"] == {"executed": 2, "rejected": 2}
    assert view["skipped"] == 1  # ids[1] was already executed
    assert len(world.calls("sonarr.test", "DELETE")) == 2
    assert await env.service.pending_batches() == []

    other, _ = make_env(db, world)
    with pytest.raises(NotFound):  # an item that is not in this batch
        await other.service.decide_batch(batch["id"], "approve", [987654])


async def test_items_over_the_cap_stay_approved_and_run_as_the_cap_frees_up(
    db: Database, world: FakeArrWorld, clock: Clock
) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 41)]
    env, events = make_env(db, world, clock, cap=10)
    await stalled.run(env)  # approve policy: all 40 are filed, the cap is not applied yet
    [batch] = await env.service.pending_batches()
    assert batch["total"] == 40

    view = await env.service.decide_batch(batch["id"], "approve")

    # Approved items are never asked about again: 10 ran, 30 wait in the queue.
    assert view["counts"] == {"executed": 10, "queued": 30} and view["queued"] == 30
    assert view["outcome"] == "10 executed, 30 queued, next in ~60 min"
    queued = [i for i in view["items"] if i["status"] == "queued"]
    assert all("limit reached: 10 per hour" in i["result"]["detail"] for i in queued)
    assert all(i["decided_by"] == "ui" for i in queued)
    assert len(world.calls("sonarr.test", "DELETE")) == 10  # nothing beyond the cap was sent
    assert view["pending"] == 0  # nothing is left to decide: no buttons, no second question
    assert [b["id"] for b in await env.service.pending_batches()] == [batch["id"]]  # card stays

    # The next automation run does not file the queued items again (nor the executed ones).
    assert (await stalled.run(env))["requested"] == 0
    assert await env.service.pending() == []
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM approval_batches"))["n"] == 1

    # Nothing moves while the window is full ...
    events.clear()
    clock.now += 30 * 60
    assert await env.service.drain_queue() == 0
    assert (await env.service.get_batch(batch["id"]))["outcome"].endswith("next in ~30 min")
    # ... and as the cap frees up, the queue runs by itself, ten per hour, oldest first.
    clock.now += 31 * 60
    assert await env.service.drain_queue() == 10
    view = await env.service.get_batch(batch["id"])
    assert view["counts"] == {"executed": 20, "queued": 20}
    assert [i["status"] for i in view["items"][:20]] == ["executed"] * 20  # oldest first
    assert len(world.calls("sonarr.test", "DELETE")) == 20
    progress = [e for n, e in events if n == "approval.progress"]
    assert len(progress) == 1 and progress[0]["queued"] == 20  # one update for the whole pass
    assert progress[0]["decision"] == "approve" and progress[0]["decided_by"] == "ui"
    assert await env.service.drain_queue() == 0  # cap is full again
    clock.now += 61 * 60
    assert await env.service.drain_queue() == 10
    clock.now += 61 * 60
    assert await env.service.drain_queue() == 10
    done = await env.service.get_batch(batch["id"])
    assert done["counts"] == {"executed": 40} and done["queued"] == 0
    assert await env.service.pending_batches() == []
    assert len(world.calls("sonarr.test", "DELETE")) == 40
    assert (await stalled.run(env))["requested"] == 0  # executed items are still remembered


async def test_queued_items_can_be_cancelled_by_rejecting(
    db: Database, world: FakeArrWorld, clock: Clock
) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 6)]
    env, _ = make_env(db, world, clock, cap=2)
    await stalled.run(env)
    [batch] = await env.service.pending_batches()
    await env.service.decide_batch(batch["id"], "approve")
    view = await env.service.decide_batch(batch["id"], "reject", decided_by="ui")
    assert view["counts"] == {"executed": 2, "rejected": 3}
    clock.now += 2 * HOURS
    assert await env.service.drain_queue() == 0


async def test_a_new_run_asks_again_only_about_what_nobody_decided(
    db: Database, world: FakeArrWorld, clock: Clock
) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 5)]
    env, _ = make_env(db, world, clock, cap=1)
    await stalled.run(env)
    [batch] = await env.service.pending_batches()
    ids = [i["id"] for i in batch["items"]]
    await env.service.decide_batch(batch["id"], "approve", ids[:3])  # 1 executed, 2 queued
    world.queue["sonarr"].append(blocked(9))  # something new appears meanwhile
    clock.now += 60  # the next run
    summary = await stalled.run(env)
    # Only the new item is asked about: the pending one is in the open batch, the others approved.
    assert summary["requested"] == 1
    batches = await env.service.pending_batches()
    assert [b["pending"] for b in batches] == [1, 1]


async def test_policy_auto_keeps_to_the_hourly_cap_instead_of_filing_deferred_rows(
    db: Database, world: FakeArrWorld
) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 16)]
    env, _ = make_env(db, world, policy="auto", cap=4)
    summary = await stalled.run(env)
    assert summary == {"requested": 4, "outcomes": {"executed": 4}}
    assert await env.service.pending_batches() == []


async def test_missing_search_and_indexer_triage_file_one_batch_per_run(
    db: Database, world: FakeArrWorld
) -> None:
    world.wanted[("radarr", "missing")] = [
        {
            "id": i,
            "title": f"Movie{i}",
            "year": 2020,
            "lastSearchTime": f"2026-01-0{i + 1}T00:00:00Z",
        }
        for i in range(3)
    ]
    world.wanted[("sonarr", "missing")] = [
        {
            "id": 100 + i,
            "series": {"title": "Show"},
            "seasonNumber": 1,
            "episodeNumber": i,
            "lastSearchTime": f"2026-01-0{i + 1}T00:00:00Z",
        }
        for i in range(3)
    ]
    env, events = make_env(db, world, whisparr=[])

    summary = await search.run(env, "missing")

    assert summary == {"requested": 2, "outcomes": {"pending": 2}}  # radarr + sonarr
    [batch] = await env.service.pending_batches()
    assert batch["action"] == "arr_search" and batch["total"] == 2
    assert batch["title"].startswith("Missing search: search for 4 missing items in ")
    assert names(events).count("approval.pending") == 1

    # Prowlarr: every failing indexer goes into one batch.
    world.indexers = [{"id": n, "name": f"Idx{n}", "enable": True} for n in (1, 2, 3)]
    world.indexer_status = [
        {"indexerId": n, "disabledTill": "2999-01-01T00:00:00Z"} for n in (1, 2, 3)
    ]
    from ai_server_monitor.collectors.arr import ArrCollector
    from ai_server_monitor.collectors.base import CollectorResult, HostEnv

    collector = ArrCollector(env.settings.collectors.arr, HostEnv(), transport=world.transport())
    collector.app_settings = env.settings
    result = CollectorResult("arr", 1.0, ok=True, data=await collector.collect())
    await collector.close()
    env.collector_results = lambda: {"arr": result}
    assert (await triage.run(env))["outcomes"] == {"pending": 3}
    triage_batches = [
        b for b in await env.service.pending_batches() if b["label"] == "Indexer triage"
    ]
    assert len(triage_batches) == 1
    assert triage_batches[0]["title"] == "Indexer triage: test 3 indexers in Prowlarr"


# --------------------------------------------------------------------------- the service


async def test_requests_of_one_chat_turn_share_a_batch(db: Database, world: FakeArrWorld) -> None:
    env, events = make_env(db, world)
    svc = env.service
    args = {"app": "sonarr", "instance": "sonarr", "queue_id": 1, "title": "A"}

    first = await svc.request(REMOVE, args, "chat:s1", run_key="turn:a", label="Chat")
    second = await svc.request(REMOVE, {**args, "queue_id": 2}, "chat:s1", run_key="turn:a")
    other_turn = await svc.request(REMOVE, {**args, "queue_id": 3}, "chat:s1", run_key="turn:b")
    other_chat = await svc.request(REMOVE, {**args, "queue_id": 4}, "chat:s2", run_key="turn:a")
    single = await svc.request(REMOVE, {**args, "queue_id": 5}, "chat:s1")  # no run: batch of one

    assert first["batch_id"] == second["batch_id"]
    assert len({first["batch_id"], other_turn["batch_id"], other_chat["batch_id"]}) == 3
    assert single["batch_id"] not in (first["batch_id"], other_turn["batch_id"])
    assert names(events).count("approval.pending") == 4
    assert names(events).count("approval.updated") == 1  # the second request of the turn
    view = await svc.get_batch(first["batch_id"])
    assert view["title"] == "Chat: remove 2 queue items from sonarr (blocklist)"

    # A decided batch is closed: the same turn asking again opens a new one.
    await svc.decide_batch(first["batch_id"], "reject")
    third = await svc.request(REMOVE, {**args, "queue_id": 6}, "chat:s1", run_key="turn:a")
    assert third["batch_id"] != first["batch_id"]


async def test_expiry_marks_everything_still_pending_in_the_batch(
    db: Database, world: FakeArrWorld, clock: Clock
) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 7)]
    env, events = make_env(db, world, clock, cap=0)
    await stalled.run(env)
    [batch] = await env.service.pending_batches()
    ids = [i["id"] for i in batch["items"]]
    await env.service.decide_batch(batch["id"], "approve", ids[:2])  # part executed
    events.clear()

    clock.now += 1 * HOURS + 1
    assert await env.service.sweep_expired() == 4

    view = await env.service.get_batch(batch["id"])
    assert view["counts"] == {"executed": 2, "expired": 4} and view["pending"] == 0
    assert all(i["decided_by"] == "timeout" for i in view["items"] if i["status"] == "expired")
    assert names(events).count("approval.expired") == 1  # one event for the batch
    assert names(events).count("action.result") == 4  # and one per item for the audit/metrics
    assert next(e for n, e in events if n == "approval.expired")["decision"] == "expired"
    assert await env.service.pending_batches() == []
    with pytest.raises(Conflict):
        await env.service.decide_batch(batch["id"], "approve")
    # Expired but not yet swept: deciding is refused and marks it expired.
    clock.now = 1_800_000_000.0 + 5 * HOURS
    fresh = await env.service.request_many(
        REMOVE,
        [({"app": "sonarr", "instance": "sonarr", "queue_id": 50}, None)],
        "scheduler",
        run_key="x",
    )
    clock.now += 2 * HOURS
    with pytest.raises(Conflict, match="expired"):
        await env.service.decide_batch(fresh[0]["batch_id"], "approve")
    assert (await env.service.get(fresh[0]["id"]))["status"] == "expired"


async def test_batches_hold_only_what_needs_approval(db: Database, world: FakeArrWorld) -> None:
    world.queue["sonarr"] = [blocked(1)]
    env, _ = make_env(db, world, policy="suggest")
    await stalled.run(env)
    assert await env.service.pending_batches() == []
    row = await db.fetchone("SELECT COUNT(*) AS n FROM approval_batches")
    assert row["n"] == 0
    [item] = (await env.service.list_actions())["items"]
    assert item["status"] == "suggested" and item["batch_id"] is None


# --------------------------------------------------------------------------- HTTP API


def test_http_api_decides_batches_and_subsets(bootstrap: Bootstrap, world: FakeArrWorld) -> None:
    world.queue["sonarr"] = [blocked(i) for i in range(1, 6)]
    settings = make_settings({"stalled_cleanup": {"policy": "approve", "max_per_hour": 0}})
    app = create_app(
        bootstrap, collector_registry={}, seed_settings=settings, arr_transport=world.transport()
    )
    with TestClient(app) as client:
        ctx = app.state.ctx
        env = AutomationEnv(ctx.actions, settings)
        client.portal.call(stalled.run, env)  # type: ignore[union-attr]

        listing = client.get("/api/approvals").json()
        [batch] = listing["batches"]
        assert batch["total"] == 5 and len(listing["items"]) == 5  # flat list stays
        ids = [i["id"] for i in batch["items"]]
        assert client.get(f"/api/approvals/{batch['id']}").json()["title"] == batch["title"]
        assert client.get("/api/approvals/999").status_code == 404

        part = client.post(
            f"/api/approvals/{batch['id']}", json={"decision": "approve", "items": ids[:3]}
        )
        assert part.status_code == 200 and part.json()["counts"] == {"executed": 3, "pending": 2}
        assert (
            client.post(
                f"/api/approvals/{batch['id']}", json={"decision": "approve", "items": [12345]}
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/approvals/{batch['id']}", json={"decision": "approve", "items": []}
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/approvals/{batch['id']}", json={"decision": "approve", "items": ids[:1]}
            ).status_code
            == 409
        )  # already executed

        rest = client.post(f"/api/approvals/{batch['id']}", json={"decision": "reject"})
        assert rest.json()["counts"] == {"executed": 3, "rejected": 2}
        assert client.get("/api/approvals").json() == {"batches": [], "items": []}

        audit = client.get("/api/actions", params={"batch": batch["id"]}).json()
        assert audit["total"] == 5 and {i["batch_id"] for i in audit["items"]} == {batch["id"]}
        assert client.get("/api/actions", params={"batch": 424242}).json()["total"] == 0


# --------------------------------------------------------------------------- upgrade


async def test_upgrade_wraps_pending_approvals_and_moves_telegram_messages(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for _, _, sql in load_migrations()[:9]:
        conn.executescript(sql)
    conn.execute("PRAGMA user_version = 9")
    base = 1_800_000_000

    def action(
        ts: float, action: str, source: str, status: str, key: str, target: str = "t"
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO actions (ts, updated, action, target, args, policy, policy_key, status, "
            "source, expires_at) VALUES (?, ?, ?, ?, '{}', 'approve', ?, ?, ?, ?)",
            (ts, ts, action, target, key, status, source, ts + 86400),
        )
        return int(cursor.lastrowid or 0)

    run_a = [action(base + i, REMOVE, "scheduler", "pending", "stalled_cleanup") for i in range(3)]
    next_run = action(base + 600, REMOVE, "scheduler", "pending", "stalled_cleanup")
    chat = action(base + 5, "arr_search", "chat:abc", "pending", "missing_search")
    done = action(base + 7, REMOVE, "scheduler", "executed", "stalled_cleanup")  # history
    shown = action(base + 9, "qbit_delete_torrent", "telegram:1", "rejected", "stalled_cleanup")
    for item_id, chat_id, message_id in ((run_a[0], 11, 501), (shown, 11, 502)):
        conn.execute(
            "INSERT INTO telegram_approval_messages (approval_id, chat_id, message_id, created_ts) "
            "VALUES (?, ?, ?, ?)",
            (item_id, chat_id, message_id, base),
        )
    conn.commit()
    conn.close()

    db = Database(path)
    await db.connect()
    try:
        assert await db.schema_version() >= 10
        rows = {
            r["id"]: r["batch_id"] for r in await db.fetchall("SELECT id, batch_id FROM actions")
        }
        # Nothing is lost: every pending approval has a batch; the same run is one batch.
        assert len({rows[i] for i in run_a}) == 1 and rows[run_a[0]] is not None
        assert rows[next_run] not in (None, rows[run_a[0]])
        assert rows[chat] not in (None, rows[run_a[0]], rows[next_run])
        assert rows[done] is None  # finished history is left as it is
        assert rows[shown] is not None  # has a Telegram message: wrapped, so it can be edited
        batches = {r["id"]: dict(r) for r in await db.fetchall("SELECT * FROM approval_batches")}
        assert batches[rows[run_a[0]]]["label"] == "Stalled cleanup"
        assert batches[rows[chat]]["label"] == "Chat"
        statuses = {r["status"] for r in await db.fetchall("SELECT status FROM actions")}
        assert statuses == {"pending", "executed", "rejected"}

        # Telegram messages follow their approval to the batch; the old table is gone.
        moved = {
            (r["batch_id"], r["message_id"])
            for r in await db.fetchall("SELECT * FROM telegram_batch_messages")
        }
        assert moved == {(rows[run_a[0]], 501), (rows[shown], 502)}
        tables = {
            r["name"]
            for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "telegram_approval_messages" not in tables
    finally:
        await db.close()

    # The upgraded database serves the new API: the wrapped batch can be decided.
    db = Database(path)
    await db.connect()
    try:
        service = ActionService(db, make_settings({}), clock=lambda: base + 100)
        batch = await service.get_batch(rows[run_a[0]])
        assert batch["pending"] == 3 and batch["total"] == 3
        assert json.dumps(batch["counts"], sort_keys=True) == '{"pending": 3}'
        view = await service.decide_batch(batch["id"], "reject")
        assert view["counts"] == {"rejected": 3}
    finally:
        await db.close()


async def test_batches_survive_a_second_start(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    await db.connect()
    await db.close()
    db = Database(tmp_path / "x.db")  # migrations are not applied twice
    await db.connect()
    assert await db.schema_version() >= 10
    await db.close()
