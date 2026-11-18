from __future__ import annotations

import json
import time

from ai_server_monitor.db import Database
from ai_server_monitor.tracing import codec
from ai_server_monitor.tracing.store import ToolRecord

from .tracing_helpers import API_KEY, call, make_tracing

# --------------------------------------------------------------------------- codec


def test_cap_json_shortens_strings_and_keeps_json_valid() -> None:
    messages = [{"role": "user", "content": "x" * 50_000}, {"role": "tool", "content": "short"}]
    text, cut = codec.cap_json(messages, 4_000)
    assert cut and len(text.encode()) <= 4_000
    parsed = json.loads(text)
    assert parsed[1]["content"] == "short"
    assert "chars cut" in parsed[0]["content"]


def test_cap_json_untouched_when_it_fits_and_cap_text_marks_the_cut() -> None:
    assert codec.cap_json({"a": 1}, 100) == ('{"a":1}', False)
    text, cut = codec.cap_text("é" * 1000, 200)
    assert cut and len(text.encode()) <= 240 and "chars cut" in text
    assert codec.cap_text("abc", 10) == ("abc", False)


def test_pack_roundtrip_compresses() -> None:
    text = "disk usage 88%\n" * 5000
    blob = codec.pack(text)
    assert codec.unpack(blob) == text and len(blob) < len(text) // 10
    assert codec.unpack(None) is None


# --------------------------------------------------------------------------- store


async def test_insert_and_get_roundtrip(db: Database) -> None:
    tracing = await make_tracing(db)
    store = tracing.store
    tools = [{"type": "function", "function": {"name": "disk_usage", "description": "x" * 400}}]
    call_id = await store.insert_call(
        call(
            tools=tools,
            reasoning="thinking about disks",
            tool_calls=[{"id": "c1", "name": "disk_usage", "arguments": '{"q":"up"}'}],
            params={"temperature": 0.2, "max_tokens": 900},
            link_kind="chat",
            link_id="s1",
            turn="t1",
        )
    )
    got = await store.get_call(call_id)
    assert got is not None
    assert got["messages"][1]["content"] == "Disk is 88% full"
    assert got["tools"] == tools and got["tools_count"] == 1 and got["tools_tokens"] > 50
    assert got["content"] == "Free some space." and got["reasoning"] == "thinking about disks"
    assert got["tool_calls"][0]["name"] == "disk_usage" and got["tool_calls_count"] == 1
    assert got["params"] == {"temperature": 0.2, "max_tokens": 900}
    assert got["request_preview"] == "Disk is 88% full"
    assert got["tokens_per_s"] == 5.0  # 5 tokens over (1500 - 500) ms
    assert got["messages_count"] == 2 and got["bodies"] is True and got["truncated"] == []
    assert await store.get_call(9999) is None


async def test_bodies_are_stored_compressed(db: Database) -> None:
    tracing = await make_tracing(db)
    payload = "node_load1 0.49\n" * 4000  # ~64 KB, very repetitive
    call_id = await tracing.store.insert_call(call(messages=[{"role": "user", "content": payload}]))
    row = await db.fetchone("SELECT messages, size_bytes FROM llm_calls WHERE id = ?", (call_id,))
    assert row is not None
    assert isinstance(row["messages"], bytes) and len(row["messages"]) < len(payload) // 8
    assert row["size_bytes"] < len(payload) // 8


async def test_field_cap_marks_truncation(db: Database) -> None:
    tracing = await make_tracing(db, field_cap_bytes=2_000)
    call_id = await tracing.store.insert_call(
        call(messages=[{"role": "user", "content": "a" * 10_000}], content="b" * 10_000)
    )
    got = await tracing.store.get_call(call_id)
    assert got is not None
    assert sorted(got["truncated"]) == ["content", "messages"]
    assert len(got["content"].encode()) <= 2_100 and "chars cut" in got["content"]
    assert json.loads(json.dumps(got["messages"]))  # still structured messages
    assert got["messages"][0]["role"] == "user"


async def test_secrets_never_reach_the_database(db: Database) -> None:
    tracing = await make_tracing(db)
    leaked = f"my key is {API_KEY} and Bearer abcdefghijklmnop1234"
    call_id = await tracing.store.insert_call(
        call(
            messages=[{"role": "user", "content": leaked}],
            content=f"echo {API_KEY}",
            reasoning=f"I should not repeat {API_KEY}",
            tool_calls=[{"id": "1", "name": "t", "arguments": json.dumps({"api_key": API_KEY})}],
            error=f"401 for {API_KEY}",
            params={"temperature": 0.1, "api_key": API_KEY},
        )
    )
    await tracing.store.insert_tool(
        ToolRecord(
            ts=time.time(), tool="t", ok=True, args={"token": API_KEY}, result=f"k={API_KEY}"
        )
    )
    got = await tracing.store.get_call(call_id)
    assert got is not None
    assert API_KEY not in json.dumps(got)
    assert "abcdefghijklmnop1234" not in json.dumps(got)
    assert "[redacted]" in got["messages"][0]["content"]
    # Not even in the raw compressed bytes once decompressed.
    for table in ("llm_calls", "llm_tool_calls"):
        for row in await db.fetchall(f"SELECT * FROM {table}"):
            for value in dict(row).values():
                text = codec.unpack(value) if isinstance(value, bytes) else str(value)
                assert API_KEY not in (text or "")


async def test_metadata_only_mode_stores_no_bodies(db: Database) -> None:
    tracing = await make_tracing(db, store_bodies=False)
    call_id = await tracing.store.insert_call(call(tools=[{"type": "function"}]))
    await tracing.store.insert_tool(ToolRecord(ts=time.time(), tool="t", ok=True, args={"a": 1}))
    got = await tracing.store.get_call(call_id)
    assert got is not None
    assert got["bodies"] is False
    assert got["messages"] is None and got["content"] is None and got["tools"] is None
    assert got["request_preview"] is None and got["response_preview"] is None
    # Metadata survives: sizes, tokens, timings.
    assert got["prompt_tokens"] == 20 and got["total_ms"] == 1500 and got["messages_count"] == 2
    row = await db.fetchone("SELECT args, size_bytes FROM llm_tool_calls")
    assert row is not None and row["args"] is None


async def test_list_filters_and_paging(db: Database) -> None:
    tracing = await make_tracing(db)
    store = tracing.store
    now = time.time()
    await store.insert_call(call(ts=now - 300, purpose="report", model="a"))
    await store.insert_call(call(ts=now - 200, purpose="chat", status="error", error="boom"))
    await store.insert_call(call(ts=now - 100, purpose="chat", link_kind="chat", link_id="s1"))
    items, total, complete = await store.list_calls()
    assert total == 3 and complete and [i["purpose"] for i in items] == ["chat", "chat", "report"]
    assert "messages" not in items[0]  # the list never carries bodies
    assert (await store.list_calls(purpose="chat"))[1] == 2
    assert (await store.list_calls(status="error"))[0][0]["error"] == "boom"
    assert (await store.list_calls(since=now - 250, until=now - 150))[1] == 1
    assert (await store.list_calls(link_kind="chat", link_id="s1"))[1] == 1
    assert (await store.list_calls(model="a"))[1] == 1
    page, total, _ = await store.list_calls(limit=1, offset=1)
    assert total == 3 and len(page) == 1 and page[0]["status"] == "error"


async def test_text_search_covers_messages_answers_reasoning_and_errors(db: Database) -> None:
    tracing = await make_tracing(db)
    store = tracing.store
    await store.insert_call(call(messages=[{"role": "user", "content": "Is nginx running?"}]))
    await store.insert_call(call(content="Restart the Plex container"))
    await store.insert_call(call(reasoning="maybe the zfs pool"))
    await store.insert_call(call(status="error", error="connection refused", content=""))
    for needle, expected in [("NGINX", 1), ("plex", 1), ("ZFS", 1), ("refused", 1), ("absent", 0)]:
        items, total, complete = await store.list_calls(q=needle)
        assert total == expected and len(items) == expected and complete, needle
    items, total, _ = await store.list_calls(q="disk", purpose="report")  # default messages
    assert total == 3  # the nginx call does not mention disks
    assert (await store.list_calls(q="disk", purpose="chat"))[1] == 0


async def test_attach_links_calls_afterwards(db: Database) -> None:
    tracing = await make_tracing(db)
    ids = [await tracing.store.insert_call(call()) for _ in range(2)]
    other = await tracing.store.insert_call(call())
    assert await tracing.attach(ids, "report", 42) == 2
    items, total, _ = await tracing.store.list_calls(link_kind="report", link_id="42")
    assert total == 2 and {i["id"] for i in items} == set(ids)
    assert other not in {i["id"] for i in items}


async def test_timeline_interleaves_llm_and_tool_calls_of_one_turn(db: Database) -> None:
    store = (await make_tracing(db)).store
    t0 = time.time()
    link = {"link_kind": "chat", "link_id": "s1", "turn": "turn-1"}
    first = await store.insert_call(call(ts=t0, link_step=1, **link))
    await store.insert_tool(
        ToolRecord(ts=t0 + 1, tool="disk_usage", ok=True, duration_ms=120, result_size=900, **link)
    )
    await store.insert_tool(
        ToolRecord(ts=t0 + 1.5, tool="logs", ok=False, error="timeout", duration_ms=5000, **link)
    )
    last = await store.insert_call(call(ts=t0 + 3, link_step=2, **link))
    await store.insert_call(call(ts=t0 + 4, link_kind="chat", link_id="s1", turn="turn-2"))
    await store.insert_call(call(ts=t0 - 50))  # unrelated

    timeline = await store.timeline(last)
    assert timeline is not None and timeline["selected"] == last
    assert [(e["type"], e.get("tool") or e["id"]) for e in timeline["entries"]] == [
        ("llm", first),
        ("tool", "disk_usage"),
        ("tool", "logs"),
        ("llm", last),
    ]
    tool = timeline["entries"][2]
    assert tool["ok"] is False and tool["error"] == "timeout" and tool["duration_ms"] == 5000
    assert await store.timeline(99999) is None


async def test_timeline_of_an_unlinked_call_is_just_that_call(db: Database) -> None:
    store = (await make_tracing(db)).store
    only = await store.insert_call(call())
    await store.insert_call(call())
    timeline = await store.timeline(only)
    assert timeline is not None and [e["id"] for e in timeline["entries"]] == [only]


async def test_tool_call_detail(db: Database) -> None:
    store = (await make_tracing(db)).store
    tool_id = await store.insert_tool(
        ToolRecord(
            ts=time.time(),
            tool="disk_usage",
            ok=True,
            args={"query": "up"},
            result={"series": [1, 2]},
        )
    )
    got = await store.get_tool(tool_id)
    assert (
        got is not None and got["args"] == {"query": "up"} and got["result"] == {"series": [1, 2]}
    )
    assert got["ok"] is True and got["truncated"] is False
    assert await store.get_tool(404) is None


# --------------------------------------------------------------------------- retention


async def test_prune_by_age(db: Database) -> None:
    tracing = await make_tracing(db, retention_days=7)
    store = tracing.store
    now = time.time()
    old = await store.insert_call(call(ts=now - 8 * 86400))
    fresh = await store.insert_call(call(ts=now - 6 * 86400))
    await store.insert_tool(ToolRecord(ts=now - 9 * 86400, tool="t", ok=True))
    await store.insert_tool(ToolRecord(ts=now - 1 * 86400, tool="t", ok=True))
    await store.insert_logs(
        [
            {
                "ts": now - 10 * 86400,
                "level": "ERROR",
                "levelno": 40,
                "logger": "x",
                "message": "o",
            },
            {"ts": now - 1, "level": "ERROR", "levelno": 40, "logger": "x", "message": "n"},
        ]
    )
    deleted = await store.prune(now)
    assert deleted == {"llm_calls": 1, "llm_tool_calls": 1, "app_log": 1}
    assert await store.get_call(old) is None and await store.get_call(fresh) is not None
    assert [r["message"] for r in await store.list_logs()] == ["n"]


async def test_prune_by_total_size_removes_oldest_first(db: Database) -> None:
    tracing = await make_tracing(db, retention_days=30, max_total_mb=1, field_cap_bytes=2_000_000)
    store = tracing.store
    import os

    now = time.time()
    ids = []
    for i in range(6):
        # incompressible ~300 KB bodies once compressed, oldest first
        body = os.urandom(300_000).hex()
        ids.append(
            await store.insert_call(
                call(ts=now - 600 + i, messages=[{"role": "user", "content": body}])
            )
        )
    before = await store.stats()
    assert before["bytes"] > 1024 * 1024
    deleted = await store.prune(now)
    after = await store.stats()
    assert deleted["llm_calls"] >= 1
    assert after["bytes"] <= 1024 * 1024
    remaining = {i["id"] for i in (await store.list_calls())[0]}
    assert ids[-1] in remaining and ids[0] not in remaining
    assert remaining == set(ids[len(ids) - len(remaining) :])  # a newest-first suffix


async def test_prune_by_size_also_drops_tool_calls_of_removed_calls(db: Database) -> None:
    import os

    tracing = await make_tracing(db, max_total_mb=1, field_cap_bytes=2_000_000)
    store = tracing.store
    now = time.time()
    await store.insert_tool(ToolRecord(ts=now - 500, tool="old", ok=True))
    for i in range(4):
        await store.insert_call(
            call(
                ts=now - 400 + i, messages=[{"role": "user", "content": os.urandom(300_000).hex()}]
            )
        )
    await store.insert_tool(ToolRecord(ts=now - 1, tool="new", ok=True))
    await store.prune(now)
    names = [r["tool"] for r in await db.fetchall("SELECT tool FROM llm_tool_calls")]
    assert names == ["new"]


async def test_prune_keeps_everything_within_limits(db: Database) -> None:
    store = (await make_tracing(db)).store
    await store.insert_call(call())
    assert await store.prune() == {"llm_calls": 0, "llm_tool_calls": 0, "app_log": 0}


async def test_app_log_row_cap(db: Database) -> None:
    store = (await make_tracing(db, app_log_max_rows=10)).store
    now = time.time()
    rows = [
        {"ts": now, "level": "ERROR", "levelno": 40, "logger": "x", "message": f"m{i}"}
        for i in range(25)
    ]
    await store.insert_logs(rows)
    await store.prune(now)
    messages = [r["message"] for r in await store.list_logs(limit=100)]
    assert len(messages) == 10 and messages[0] == "m24" and messages[-1] == "m15"


async def test_stats(db: Database) -> None:
    store = (await make_tracing(db)).store
    await store.insert_call(call(purpose="report"))
    await store.insert_call(call(purpose="chat", status="error"))
    await store.insert_call(call(purpose="chat"))
    stats = await store.stats()
    assert stats["calls"] == 3 and stats["failed"] == 1 and stats["bytes"] > 0
    assert stats["purposes"][0] == {"purpose": "chat", "count": 2}
