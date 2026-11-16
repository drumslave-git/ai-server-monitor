"""The shared audit trail of the chat's own calls, and the prompt notes that depend on tools."""

from __future__ import annotations

from fastapi.testclient import TestClient

from ai_server_monitor.chat import audit
from ai_server_monitor.chat.prompts import MAX_PROM_LINES, prom_lines, system_prompt
from ai_server_monitor.config import Bootstrap
from ai_server_monitor.db import Database

from .test_chat import FakeLLM, make_app


async def test_record_call_keeps_the_newest_entries_and_filters_by_kind(db: Database) -> None:
    for i in range(audit.KEEP + 20):
        await audit.record_call(db, "shell" if i % 2 else "api", "s1", n=i)
    rows = await audit.recent(db, limit=500)
    assert len(rows) == audit.KEEP
    assert rows[0]["n"] == audit.KEEP + 19  # newest first
    assert {r["kind"] for r in await audit.recent(db, 10, kind="api")} == {"api"}
    assert len(await audit.recent(db, 3)) == 3
    assert (await audit.recent(db, 5, kind="mcp")) == []


async def test_audit_hook_files_records_under_its_kind(db: Database) -> None:
    hook = audit.audit_hook(db, "api", "sess")
    await hook({"service": "radarr", "method": "GET", "path": "/api/v3/queue"})
    [row] = await audit.recent(db)
    assert row["kind"] == "api" and row["session"] == "sess" and row["service"] == "radarr"


async def test_record_call_never_raises(db: Database) -> None:
    await db.close()
    await audit.record_call(db, "shell", None, cmd="ls")  # the db is gone: swallowed and logged


def test_audit_endpoint(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM(), mode="none")
    with TestClient(app) as client:
        ctx = app.state.ctx

        async def fill() -> None:
            await audit.record_call(ctx.db, "shell", "s1", cmd="ps aux", exit=0, seconds=0.1)
            await audit.record_call(ctx.db, "api", "s1", path="/api/v3/queue")

        client.portal.call(fill)  # type: ignore[union-attr]
        items = client.get("/api/chat/audit").json()["items"]
        assert [i["kind"] for i in items] == ["api", "shell"]
        assert items[1]["cmd"] == "ps aux" and items[1]["exit"] == 0  # kind-specific fields kept
        only = client.get("/api/chat/audit", params={"kind": "shell", "limit": 5}).json()
        assert [i["cmd"] for i in only["items"]] == ["ps aux"]


def test_prompt_mentions_api_tools_and_auto_prometheus_queries_only_when_present() -> None:
    queries = [
        {"name": f"q{i}", "description": f"does thing {i}", "query": "up"} for i in range(15)
    ]
    plain = system_prompt("en", tool_names={"get_status"})
    assert "api_call" not in plain and "Prometheus queries" not in plain
    full = system_prompt(
        "en",
        tool_names={"api_call", "api_endpoints", "promql_query"},
        prom_queries=queries,
    )
    assert "api_endpoints" in full and "api_call" in full
    assert "- q0: does thing 0" in full
    assert f"- q{MAX_PROM_LINES - 1}:" in full and f"- q{MAX_PROM_LINES}:" not in full
    # listed queries mean nothing without the tool to run them
    assert "Prometheus queries" not in system_prompt(
        "en", tool_names={"get_status"}, prom_queries=queries
    )
    assert prom_lines([{"name": "n", "query": "rate(x[5m])"}]) == ["- n: rate(x[5m])"]


def test_prompt_asks_for_named_items_not_just_counts() -> None:
    for tools in ({"get_status"}, {"api_call", "api_endpoints"}):
        prompt = " ".join(system_prompt("en", tool_names=tools).split())
        assert "asks WHICH or WHAT exactly" in prompt
        assert "NAME the items" in prompt and "up to about 50" in prompt
        assert "say how many more" in prompt and "A count alone is not an answer" in prompt


def test_prompt_wording_rules_for_pending_writes_compact_lists_and_ranking() -> None:
    prompt = " ".join(system_prompt("en", tool_names={"get_status"}).split())
    # a write that only filed an approval is never reported as done
    assert "NOTHING has been done" in prompt and "waiting for your approval" in prompt
    assert 'never "saved" or "done", not even with "but it is pending"' in prompt
    assert 'Only "executed" means done' in prompt
    # long lists are written compactly, not as ~100 bullet lines
    assert "From about 20 items on" in prompt and "not one bullet line per item" in prompt
    # ranked lists arrive sorted; the model keeps the order
    assert "`rank` 1 = first" in prompt and "keep that order" in prompt


def test_every_pending_note_leads_with_not_done() -> None:
    from ai_server_monitor.apiaccess.tools import _NOTES as api_notes
    from ai_server_monitor.chat.tools import _NOTES as chat_notes
    from ai_server_monitor.mcp_client.tools import _NOTES as mcp_notes

    for notes in (api_notes, chat_notes, mcp_notes):
        pending = notes["pending"]
        assert pending.startswith("NOT DONE: waiting for")
        assert "never" in pending and "saved" in pending and "NOT been done" in pending
