"""Approvals are batches: one approval per run and action type, items decided together or one
by one, caps applied at execution, expiry for the whole batch, and the upgrade from the
one-approval-per-item layout."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.actions import ActionError, ActionService, Conflict, NotFound
from ai_server_monitor.config import Bootstrap
from ai_server_monitor.db import Database, load_migrations
from ai_server_monitor.main import create_app

from .actionfakes import NAME, FakeAction, settings_with

HOURS = 3600
LABEL = "Nightly run"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake() -> FakeAction:
    return FakeAction()


Events = list[tuple[str, dict[str, Any]]]


def make_service(
    db: Database, fake: FakeAction, clock: Clock | None = None
) -> tuple[ActionService, Events]:
    events: Events = []
    kw: dict[str, Any] = {"clock": clock} if clock else {}
    service = ActionService(
        db,
        settings_with("1h"),
        registry=fake.registry(),
        notifiers=[lambda event, record: events.append((event, record))],
        **kw,
    )
    return service, events


def names(events: Events) -> list[str]:
    return [e for e, _ in events]


async def file_run(
    service: ActionService, count: int, *, first: int = 1, run_key: str = "run:1"
) -> list[dict[str, Any]]:
    """What an automation run does: one request_many for `count` items of one source."""
    return await service.request_many(
        NAME,
        [({"target": f"t{i}"}, "needs a look") for i in range(first, first + count)],
        "scheduler",
        run_key=run_key,
        label=LABEL,
    )


# --------------------------------------------------------------------------- the service


async def test_one_run_files_one_batch_for_forty_items(db: Database, fake: FakeAction) -> None:
    service, events = make_service(db, fake)

    records = await file_run(service, 40)
    suggestion = await service.request(
        NAME, {"target": "only-suggested"}, "scheduler", suggest_only=True
    )

    assert [r["status"] for r in records] == ["pending"] * 40
    [batch] = await service.pending_batches()
    assert batch["title"] == f"{LABEL}: fake_write × 40"
    assert batch["total"] == 40 and batch["pending"] == 40 and batch["counts"] == {"pending": 40}
    assert all(i["batch_id"] == batch["id"] and i["status"] == "pending" for i in batch["items"])
    assert batch["source"] == "scheduler" and batch["label"] == LABEL
    # ONE notification for the whole run (one Telegram message), and nothing was executed.
    assert names(events).count("approval.pending") == 1
    assert names(events).count("approval.updated") == 0
    assert fake.calls == []
    # A suggestion needs no decision: it is no part of a batch.
    assert suggestion["status"] == "suggested" and suggestion["batch_id"] is None

    # More items of the same run join the open batch instead of opening a second one.
    await file_run(service, 2, first=41)
    [batch] = await service.pending_batches()
    assert batch["total"] == 42 and names(events).count("approval.updated") == 1


async def test_approve_all_executes_every_item_and_reports_per_item(
    db: Database, fake: FakeAction
) -> None:
    service, events = make_service(db, fake)
    await file_run(service, 5)
    [batch] = await service.pending_batches()

    view = await service.decide_batch(batch["id"], "approve", decided_by="ui")

    assert view["counts"] == {"executed": 5} and view["outcome"] == "5 executed"
    assert view["pending"] == 0 and view["decided"] == 5 and view["decision"] == "approve"
    assert all(i["decided_by"] == "ui" and i["result"]["done"] for i in view["items"])
    assert fake.calls == [f"t{i}" for i in range(1, 6)]
    assert names(events).count("action.result") == 5  # the audit stays per item
    assert names(events)[-1] == "approval.decided"
    assert await service.pending_batches() == []
    with pytest.raises(Conflict):
        await service.decide_batch(batch["id"], "approve")


async def test_subset_approval_leaves_the_rest_pending(db: Database, fake: FakeAction) -> None:
    service, _ = make_service(db, fake)
    await file_run(service, 4)
    [batch] = await service.pending_batches()
    ids = [i["id"] for i in batch["items"]]

    view = await service.decide_batch(batch["id"], "approve", ids[:2])
    assert view["counts"] == {"executed": 2, "pending": 2} and view["pending"] == 2
    assert fake.calls == ["t1", "t2"]
    still = await service.pending_batches()
    assert [b["id"] for b in still] == [batch["id"]]  # the card stays, with 2 items left

    # Deciding an item twice does not redo it; the others can still be rejected as a group.
    with pytest.raises(Conflict):
        await service.decide_batch(batch["id"], "approve", ids[:2])
    view = await service.decide_batch(batch["id"], "reject", ids[1:], decided_by="telegram:bo")
    assert view["counts"] == {"executed": 2, "rejected": 2}
    assert view["skipped"] == 1  # ids[1] was already executed
    assert fake.calls == ["t1", "t2"]
    assert await service.pending_batches() == []

    with pytest.raises(NotFound):  # an item that is not in this batch
        await service.decide_batch(batch["id"], "approve", [987654])


async def test_items_over_the_cap_stay_approved_and_run_as_the_cap_frees_up(
    db: Database, fake: FakeAction, clock: Clock
) -> None:
    fake.cap = 10
    service, events = make_service(db, fake, clock)
    await file_run(service, 40)  # approve policy: all 40 are filed, the cap is not applied yet
    [batch] = await service.pending_batches()
    assert batch["total"] == 40

    view = await service.decide_batch(batch["id"], "approve")

    # Approved items are never asked about again: 10 ran, 30 wait in the queue.
    assert view["counts"] == {"executed": 10, "queued": 30} and view["queued"] == 30
    assert view["outcome"] == "10 executed, 30 queued, next in ~60 min"
    queued = [i for i in view["items"] if i["status"] == "queued"]
    assert all("limit reached: 10 per hour" in i["result"]["detail"] for i in queued)
    assert all(i["decided_by"] == "ui" for i in queued)
    assert len(fake.calls) == 10  # nothing beyond the cap was run
    assert view["pending"] == 0  # nothing is left to decide: no buttons, no second question
    assert [b["id"] for b in await service.pending_batches()] == [batch["id"]]  # card stays
    assert await service.pending() == []
    assert (await db.fetchone("SELECT COUNT(*) AS n FROM approval_batches"))["n"] == 1

    # Nothing moves while the window is full ...
    events.clear()
    clock.now += 30 * 60
    assert await service.drain_queue() == 0
    assert (await service.get_batch(batch["id"]))["outcome"].endswith("next in ~30 min")
    # ... and as the cap frees up, the queue runs by itself, ten per hour, oldest first.
    clock.now += 31 * 60
    assert await service.drain_queue() == 10
    view = await service.get_batch(batch["id"])
    assert view["counts"] == {"executed": 20, "queued": 20}
    assert [i["status"] for i in view["items"][:20]] == ["executed"] * 20  # oldest first
    assert fake.calls == [f"t{i}" for i in range(1, 21)]
    progress = [e for n, e in events if n == "approval.progress"]
    assert len(progress) == 1 and progress[0]["queued"] == 20  # one update for the whole pass
    assert progress[0]["decision"] == "approve" and progress[0]["decided_by"] == "ui"
    assert await service.drain_queue() == 0  # cap is full again
    clock.now += 61 * 60
    assert await service.drain_queue() == 10
    clock.now += 61 * 60
    assert await service.drain_queue() == 10
    done = await service.get_batch(batch["id"])
    assert done["counts"] == {"executed": 40} and done["queued"] == 0
    assert await service.pending_batches() == []
    assert len(fake.calls) == 40


async def test_queued_items_can_be_cancelled_by_rejecting(
    db: Database, fake: FakeAction, clock: Clock
) -> None:
    fake.cap = 2
    service, _ = make_service(db, fake, clock)
    await file_run(service, 5)
    [batch] = await service.pending_batches()
    await service.decide_batch(batch["id"], "approve")
    view = await service.decide_batch(batch["id"], "reject", decided_by="ui")
    assert view["counts"] == {"executed": 2, "rejected": 3}
    clock.now += 2 * HOURS
    assert await service.drain_queue() == 0
    assert len(fake.calls) == 2


async def test_policy_auto_keeps_to_the_hourly_cap_without_filing_approvals(
    db: Database, fake: FakeAction, clock: Clock
) -> None:
    fake.policy, fake.cap = "auto", 4
    service, _ = make_service(db, fake, clock)
    records = await file_run(service, 15)
    statuses = [r["status"] for r in records]
    assert statuses == ["executed"] * 4 + ["queued"] * 11
    assert await service.pending_batches() == []  # nobody is asked: auto items have no batch
    assert await service.pending() == []
    clock.now += 61 * 60
    assert await service.drain_queue() == 4  # the queue drains by itself, four per hour


async def test_requests_of_one_chat_turn_share_a_batch(db: Database, fake: FakeAction) -> None:
    service, events = make_service(db, fake)

    async def ask(target: str, source: str, **kw: Any) -> dict[str, Any]:
        return await service.request(NAME, {"target": target, "title": "A"}, source, **kw)

    first = await ask("1", "chat:s1", run_key="turn:a", label="Chat")
    second = await ask("2", "chat:s1", run_key="turn:a")
    other_turn = await ask("3", "chat:s1", run_key="turn:b")
    other_chat = await ask("4", "chat:s2", run_key="turn:a")
    single = await ask("5", "chat:s1")  # no run: batch of one

    assert first["batch_id"] == second["batch_id"]
    assert len({first["batch_id"], other_turn["batch_id"], other_chat["batch_id"]}) == 3
    assert single["batch_id"] not in (first["batch_id"], other_turn["batch_id"])
    assert names(events).count("approval.pending") == 4
    assert names(events).count("approval.updated") == 1  # the second request of the turn
    view = await service.get_batch(first["batch_id"])
    assert view["title"] == "Chat: fake_write × 2"
    assert (await service.get_batch(single["batch_id"]))["title"] == "fake: 5"

    # A decided batch is closed: the same turn asking again opens a new one.
    await service.decide_batch(first["batch_id"], "reject")
    third = await ask("6", "chat:s1", run_key="turn:a")
    assert third["batch_id"] != first["batch_id"]


async def test_expiry_marks_everything_still_pending_in_the_batch(
    db: Database, fake: FakeAction, clock: Clock
) -> None:
    service, events = make_service(db, fake, clock)
    await file_run(service, 6)
    [batch] = await service.pending_batches()
    ids = [i["id"] for i in batch["items"]]
    await service.decide_batch(batch["id"], "approve", ids[:2])  # part executed
    events.clear()

    clock.now += 1 * HOURS + 1
    assert await service.sweep_expired() == 4

    view = await service.get_batch(batch["id"])
    assert view["counts"] == {"executed": 2, "expired": 4} and view["pending"] == 0
    assert all(i["decided_by"] == "timeout" for i in view["items"] if i["status"] == "expired")
    assert names(events).count("approval.expired") == 1  # one event for the batch
    assert names(events).count("action.result") == 4  # and one per item for the audit/metrics
    assert next(e for n, e in events if n == "approval.expired")["decision"] == "expired"
    assert await service.pending_batches() == []
    with pytest.raises(Conflict):
        await service.decide_batch(batch["id"], "approve")
    # Expired but not yet swept: deciding is refused and marks it expired.
    clock.now = 1_800_000_000.0 + 5 * HOURS
    fresh = await service.request_many(NAME, [({"target": "late"}, None)], "scheduler", run_key="x")
    clock.now += 2 * HOURS
    with pytest.raises(Conflict, match="expired"):
        await service.decide_batch(fresh[0]["batch_id"], "approve")
    assert (await service.get(fresh[0]["id"]))["status"] == "expired"


async def test_batches_hold_only_what_needs_approval(db: Database, fake: FakeAction) -> None:
    fake.policy = "suggest"
    service, _ = make_service(db, fake)
    await file_run(service, 1)
    assert await service.pending_batches() == []
    row = await db.fetchone("SELECT COUNT(*) AS n FROM approval_batches")
    assert row["n"] == 0
    [item] = (await service.list_actions())["items"]
    assert item["status"] == "suggested" and item["batch_id"] is None


# --------------------------------------------------------------------------- the rest of a run


async def ask_for(
    service: ActionService, target: str, note: str, run_key: str | None = "turn:a"
) -> dict[str, Any]:
    return await service.request(
        NAME, {"target": target, "note": note}, "chat:s1", run_key=run_key, label="Chat"
    )


async def test_an_approval_can_cover_the_rest_of_the_run(db: Database, fake: FakeAction) -> None:
    fake.kinds = True
    service, events = make_service(db, fake)
    first = [await ask_for(service, f"t{i}", "remove") for i in (1, 2)]
    batch_id = first[0]["batch_id"]
    view = await service.get_batch(batch_id)
    assert view["rest"] == ["fake remove"] and view["covered"] == []

    decided = await service.decide_batch(batch_id, "approve", rest=True)
    assert decided["counts"] == {"executed": 2} and decided["covered"] == ["fake remove"]
    assert decided["rest"] == []  # nothing is waiting any more

    events.clear()
    later = await ask_for(service, "t3", "remove")  # the same kind, the same run: no question
    assert later["status"] == "executed" and later["batch_id"] == batch_id
    assert later["decided_by"] == "ui: rest of the run"
    assert names(events) == ["action.result", "approval.progress"]
    assert events[-1][1]["counts"] == {"executed": 3}
    assert fake.calls == ["t1", "t2", "t3"]

    other_kind = await ask_for(service, "t4", "pause")
    other_run = await ask_for(service, "t5", "remove", run_key="turn:b")
    no_run = await ask_for(service, "t6", "remove", run_key=None)
    assert [r["status"] for r in (other_kind, other_run, no_run)] == ["pending"] * 3
    assert len({other_kind["batch_id"], other_run["batch_id"], no_run["batch_id"], batch_id}) == 4

    # a failure is the item's own outcome; the run is still covered
    fake.fail_targets.add("t7")
    failed = await ask_for(service, "t7", "remove")
    assert failed["status"] == "failed" and failed["result"]["error"] == "fake failure for t7"
    assert (await ask_for(service, "t8", "remove"))["status"] == "executed"

    found = await service.batches_of([first[0]["id"], later["id"], other_kind["id"]])
    assert [b["id"] for b in found] == [batch_id, other_kind["batch_id"]]


async def test_only_the_approved_kinds_are_covered(db: Database, fake: FakeAction) -> None:
    fake.kinds = True
    service, _ = make_service(db, fake)
    remove = await ask_for(service, "t1", "remove")
    pause = await ask_for(service, "t2", "pause")
    assert pause["batch_id"] == remove["batch_id"]
    assert (await service.get_batch(remove["batch_id"]))["rest"] == ["fake pause", "fake remove"]
    await service.decide_batch(remove["batch_id"], "approve", [remove["id"]], rest=True)
    assert (await ask_for(service, "t3", "remove"))["status"] == "executed"
    assert (await ask_for(service, "t4", "pause"))["status"] == "pending"


async def test_the_rest_needs_an_approval_a_run_and_an_action_with_kinds(
    db: Database, fake: FakeAction
) -> None:
    service, _ = make_service(db, fake)  # the action has no kinds
    record = await ask_for(service, "t1", "remove")
    assert (await service.get_batch(record["batch_id"]))["rest"] == []
    with pytest.raises(ActionError, match="cannot cover later requests"):
        await service.decide_batch(record["batch_id"], "approve", rest=True)

    fake.kinds = True
    service, _ = make_service(db, fake)
    with pytest.raises(ActionError, match="only an approval"):
        await service.decide_batch(record["batch_id"], "reject", rest=True)
    alone = await ask_for(service, "t2", "remove", run_key=None)
    with pytest.raises(ActionError, match="cannot cover later requests"):
        await service.decide_batch(alone["batch_id"], "approve", rest=True)
    assert (await service.get(record["id"]))["status"] == "pending"  # nothing was decided


# --------------------------------------------------------------------------- HTTP API


def test_http_api_decides_batches_and_subsets(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    app = create_app(bootstrap, seed_settings=settings_with("1h"))
    with TestClient(app) as client:
        ctx = app.state.ctx
        client.portal.call(file_run, ctx.actions, 5)  # type: ignore[union-attr]

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
        assert fake_action.calls == ["t1", "t2", "t3"]
        assert client.get("/api/approvals").json() == {"batches": [], "items": []}

        audit = client.get("/api/actions", params={"batch": batch["id"]}).json()
        assert audit["total"] == 5 and {i["batch_id"] for i in audit["items"]} == {batch["id"]}
        assert client.get("/api/actions", params={"batch": 424242}).json()["total"] == 0

        # ?of= finds the approval of an item whatever its state
        of = client.get("/api/approvals", params={"of": [ids[0], ids[4]]}).json()
        assert [b["id"] for b in of["batches"]] == [batch["id"]] and of["items"] == []
        assert client.get("/api/approvals", params={"of": [424242]}).json()["batches"] == []


def test_http_api_approves_the_rest_of_a_run(bootstrap: Bootstrap, fake_action: FakeAction) -> None:
    fake_action.kinds = True
    app = create_app(bootstrap, seed_settings=settings_with("1h"))
    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.actions.registry = fake_action.registry()
        first = client.portal.call(ask_for, ctx.actions, "t1", "remove")  # type: ignore[union-attr]
        url = f"/api/approvals/{first['batch_id']}"
        assert client.get(url).json()["rest"] == ["fake remove"]
        assert client.post(url, json={"decision": "reject", "rest": True}).status_code == 422
        done = client.post(url, json={"decision": "approve", "rest": True}).json()
        assert done["counts"] == {"executed": 1} and done["covered"] == ["fake remove"]
        later = client.portal.call(ask_for, ctx.actions, "t2", "remove")  # type: ignore[union-attr]
        assert later["status"] == "executed" and later["batch_id"] == first["batch_id"]
        assert client.get(url).json()["counts"] == {"executed": 2}


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

    run_a = [action(base + i, NAME, "scheduler", "pending", "stalled_cleanup") for i in range(3)]
    next_run = action(base + 600, NAME, "scheduler", "pending", "stalled_cleanup")
    chat = action(base + 5, "mcp_call", "chat:abc", "pending", "missing_search")
    done = action(base + 7, NAME, "scheduler", "executed", "stalled_cleanup")  # history
    shown = action(base + 9, NAME, "telegram:1", "rejected", "stalled_cleanup")
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
        service = ActionService(db, settings_with(), clock=lambda: base + 100)
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
