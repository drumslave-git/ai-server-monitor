"""Memory (0.11): the notes the main model keeps, its tools in the chat and in a summary run, the
notes of a job's area in a worker run (read only), and the owner's edit and delete."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent.runtime import ToolError
from ai_server_monitor.chat.audit import AUDIT_KEY
from ai_server_monitor.config import Bootstrap
from ai_server_monitor.memory.store import MemoryError, Writer
from ai_server_monitor.memory.tools import memory_tools, read_tool, titles_section

from .jobfakes import ScriptedModels, finish, tool
from .test_chat import ask
from .test_jobs import make_app, make_summary, run

CHAT = Writer("chat", "s-one")
KITCHEN = ("Kitchen sensor", "The kitchen reads 2 C warm; that is normal.")


def ctx_of(client: TestClient) -> Any:
    return client.app.state.ctx  # type: ignore[attr-defined]


def do(client: TestClient, fn: Any, *args: Any) -> Any:
    return client.portal.call(fn, *args)  # type: ignore[union-attr]


def add_domain(client: TestClient, name: str) -> None:
    do(
        client,
        ctx_of(client).db.execute,
        "INSERT INTO mcp_domains (name, description, set_by, updated_ts) "
        "VALUES (?, '', 'owner', ?)",
        (name, time.time()),
    )


def save(client: TestClient, title: str, body: str, domains: Any = None, writer: Any = CHAT) -> Any:
    return do(client, ctx_of(client).memory.save, title, body, domains, writer)


def test_the_model_writes_updates_and_deletes_its_notes(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        add_domain(client, "climate")
        note, created = save(client, *KITCHEN, ["climate"])
        assert created and note["title"] == "Kitchen sensor" and note["domains"] == ["climate"]
        assert note["author"] == "model" and note["created"] == note["updated"]
        assert note["source"] == {"kind": "chat", "id": "s-one", "label": None, "exists": False}
        # the same title (any case) is an update; the domains stay when none are given
        later, created = save(
            client, " kitchen SENSOR ", "Now 3 C warm.", None, Writer("run", "7", "heat")
        )
        assert not created and later["id"] == note["id"] and later["title"] == "Kitchen sensor"
        assert later["body"] == "Now 3 C warm." and later["domains"] == ["climate"]
        assert later["updated"] >= note["updated"] and later["created"] == note["created"]
        assert later["source"]["kind"] == "run" and later["source"]["label"] == "heat"
        cleared, _ = save(client, "kitchen sensor", "Now 3 C warm.", [])
        assert cleared["domains"] == []
        gone = do(client, ctx_of(client).memory.forget, "KITCHEN sensor")
        assert gone["id"] == note["id"]
        assert do(client, ctx_of(client).memory.notes) == []


@pytest.mark.parametrize(
    ("title", "body", "domains", "message"),
    [
        ("  ", "text", None, "needs a title"),
        ("T", " \n ", None, "needs a body"),
        ("T", "text", ["nope"], "unknown domain nope (existing domains: climate)"),
    ],
)
def test_wrong_notes_are_refused(
    bootstrap: Bootstrap, title: str, body: str, domains: Any, message: str
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        add_domain(client, "climate")
        with pytest.raises(MemoryError, match=message.replace("(", r"\(").replace(")", r"\)")):
            save(client, title, body, domains)
        assert do(client, ctx_of(client).memory.notes) == []


def test_a_note_the_owner_edited_is_read_only_for_the_model(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        memory = ctx_of(client).memory
        note, _ = save(client, *KITCHEN)
        edited = do(client, memory.edit, note["id"], "Kitchen", "Owner's text.", [])
        assert edited["author"] == "owner" and edited["title"] == "Kitchen"
        with pytest.raises(MemoryError, match="edited by the owner: you can read it"):
            save(client, "Kitchen", "Model's text.")
        with pytest.raises(MemoryError, match="only the owner can delete it"):
            do(client, memory.forget, "Kitchen")
        with pytest.raises(MemoryError, match="no note titled 'Cellar'"):
            do(client, memory.forget, "Cellar")
        assert do(client, memory.by_title, "kitchen")["body"] == "Owner's text."
        do(client, memory.delete, note["id"])
        assert do(client, memory.by_id, note["id"]) is None
        with pytest.raises(MemoryError, match="no such note"):
            do(client, memory.delete, note["id"])


def test_the_owner_cannot_rename_a_note_onto_another_title(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        memory = ctx_of(client).memory
        first, _ = save(client, "First", "one")
        save(client, "Second", "two")
        with pytest.raises(MemoryError, match="already a note titled 'second'"):
            do(client, memory.edit, first["id"], "second", "one", [])
        with pytest.raises(MemoryError, match="needs a title"):
            do(client, memory.edit, first["id"], " ", "one", [])
        with pytest.raises(MemoryError, match="needs a body"):
            do(client, memory.edit, first["id"], "First", " ", [])
        with pytest.raises(MemoryError, match="no such note"):
            do(client, memory.edit, 999, "Third", "three", [])


def test_the_writer_link_knows_whether_its_run_or_chat_is_still_there(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = ["Hello."]
    with TestClient(make_app(bootstrap)) as client:
        session = next(e for e in ask(client, "hi") if e["type"] == "done")["session_id"]
        models.script = [tool("bash", command="df -h /"), finish("ok", "Fine.")]
        finished = run(client, "docker:status")
        db = ctx_of(client).db
        save(client, "From chat", "a", None, Writer("chat", session))
        save(client, "From run", "b", None, Writer("run", str(finished["id"]), "docker:status"))
        save(client, "From a gone run", "c", None, Writer("run", "99999", "docker:status"))
        by_title = {
            n["title"]: n["source"]["exists"] for n in client.get("/api/memory").json()["items"]
        }
        assert by_title == {"From chat": True, "From run": True, "From a gone run": False}
        do(client, db.write, "DELETE FROM job_runs WHERE id = ?", (finished["id"],))
        do(client, db.write, "DELETE FROM chat_sessions WHERE id = ?", (session,))
        after = {
            n["title"]: n["source"]["exists"] for n in client.get("/api/memory").json()["items"]
        }
        assert after == {"From chat": False, "From run": False, "From a gone run": False}


def test_notes_are_listed_newest_change_first_and_by_domain(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        memory = ctx_of(client).memory
        for name in ("climate", "nas"):
            add_domain(client, name)
        save(client, "A", "a", ["climate"])
        save(client, "B", "b", ["nas"])
        save(client, "C", "c")
        save(client, "A", "a2")  # newest change
        assert [n["title"] for n in do(client, memory.notes)] == ["A", "C", "B"]
        assert [n["title"] for n in do(client, memory.notes, ["climate"])] == ["A"]
        assert [n["title"] for n in do(client, memory.notes, ["nas", "climate"])] == ["A", "B"]
        assert do(client, memory.notes, []) == []


def test_removing_a_domain_the_owner_deletes_only_unfiles_its_notes(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        add_domain(client, "climate")
        save(client, "A", "a", ["climate"])
        ctx = ctx_of(client)
        domain = do(client, ctx.db.fetchone, "SELECT id FROM mcp_domains WHERE name = 'climate'")
        do(client, ctx.domains.delete_domain, domain["id"])
        assert do(client, ctx.memory.by_title, "A")["domains"] == []


def test_the_sorter_keeps_a_domain_a_note_is_filed_under(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        ctx = ctx_of(client)
        do(
            client,
            ctx.db.execute,
            "INSERT INTO mcp_domains (name, description, set_by, updated_ts) "
            "VALUES ('sorted', '', 'model', ?), ('empty', '', 'model', ?)",
            (time.time(), time.time()),
        )
        save(client, "A", "a", ["sorted"])
        do(client, ctx.domains._apply, [], [])
        names = [r["name"] for r in do(client, ctx.db.fetchall, "SELECT name FROM mcp_domains")]
        assert names == ["sorted"]


# --------------------------------------------------------------------------- the API


def test_the_api_lists_edits_and_deletes(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        add_domain(client, "climate")
        empty = client.get("/api/memory").json()
        assert empty == {"items": [], "domains": ["climate"]}
        note, _ = save(client, *KITCHEN, ["climate"])
        sub = ctx_of(client).events.subscribe("memory")
        url = f"/api/memory/{note['id']}"
        edited = client.put(url, json={"title": "Kitchen", "body": "Mine.", "domains": []})
        assert edited.status_code == 200, edited.text
        assert edited.json()["author"] == "owner" and edited.json()["domains"] == []
        assert do(client, sub.__anext__).type == "memory.changed"
        listed = client.get("/api/memory").json()["items"]
        assert [(n["title"], n["body"], n["author"]) for n in listed] == [
            ("Kitchen", "Mine.", "owner")
        ]
        bad = client.put(url, json={"title": "Kitchen", "body": "x", "domains": ["nope"]})
        assert bad.status_code == 422 and "unknown domain nope" in bad.json()["detail"]
        assert client.put(url, json={"title": " ", "body": "x"}).status_code == 422
        assert client.put("/api/memory/999", json={"title": "T", "body": "x"}).status_code == 404
        assert client.delete(url).status_code == 204
        assert do(client, sub.__anext__).type == "memory.changed"
        assert client.delete(url).status_code == 404
        assert client.get("/api/memory").json()["items"] == []


# --------------------------------------------------------------------------- the tools


def test_the_tools_of_the_main_model(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        ctx = ctx_of(client)
        add_domain(client, "climate")
        tools = {t.name: t for t in memory_tools(ctx, CHAT, session="s-one")}
        assert set(tools) == {"memory_list", "memory_read", "memory_write", "memory_delete"}
        assert do(client, tools["memory_list"].handler, {}) == "no notes yet"
        wrote = do(
            client,
            tools["memory_write"].handler,
            {"title": "T", "body": "b", "domains": ["climate"]},
        )
        assert wrote["saved"] == "created" and wrote["domains"] == ["climate"]
        again = do(client, tools["memory_write"].handler, {"title": "t", "body": "b2"})
        assert again["saved"] == "updated" and again["title"] == "T"
        listed = do(client, tools["memory_list"].handler, {})
        assert [n["title"] for n in listed] == ["T"] and "body" not in listed[0]
        assert do(client, tools["memory_list"].handler, {"domain": "nas"}) == "no note in nas"
        full = do(client, tools["memory_read"].handler, {"title": "T"})
        assert full["body"] == "b2" and full["written_in"] == "a chat"
        with pytest.raises(ToolError, match="no note titled 'X'"):
            do(client, tools["memory_read"].handler, {"title": "X"})
        with pytest.raises(ToolError, match="unknown domain nope"):
            do(
                client,
                tools["memory_write"].handler,
                {"title": "U", "body": "b", "domains": ["nope"]},
            )
        with pytest.raises(ToolError, match="needs a title"):
            do(client, tools["memory_write"].handler, {"title": " ", "body": "b"})
        assert do(client, tools["memory_delete"].handler, {"title": "t"}) == {"deleted": "T"}
        with pytest.raises(ToolError, match="no note titled 'T'"):
            do(client, tools["memory_delete"].handler, {"title": "T"})
        audit = [e for e in do(client, ctx.db.kv_get, AUDIT_KEY, []) if e["kind"] == "memory"]
        assert [(e["tool"], e.get("action")) for e in audit] == [
            ("memory_write", "created"),
            ("memory_write", "updated"),
            ("memory_delete", None),
        ]


def test_a_run_writes_from_a_run_and_without_an_audit_entry(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        ctx = ctx_of(client)
        tools = {t.name: t for t in memory_tools(ctx, Writer("run", "5", "summary:x"))}
        do(client, tools["memory_write"].handler, {"title": "T", "body": "b"})
        full = do(client, tools["memory_read"].handler, {"title": "T"})
        assert full["written_in"] == "a run of summary:x"
        assert do(client, ctx.db.kv_get, AUDIT_KEY, []) == []


def test_the_read_tool_of_a_worker_run_sees_only_its_area(bootstrap: Bootstrap) -> None:
    with TestClient(make_app(bootstrap)) as client:
        for name in ("climate", "nas"):
            add_domain(client, name)
        save(client, "Kitchen", "k", ["climate"])
        save(client, "Pool", "p", ["nas"])
        save(client, "Loose", "l")
        read = read_tool(ctx_of(client).memory, ["climate"])
        assert do(client, read.handler, {"title": "kitchen"})["body"] == "k"
        for hidden in ("Pool", "Loose", "Nothing"):
            with pytest.raises(ToolError, match=r"for this task \(titles: Kitchen\)"):
                do(client, read.handler, {"title": hidden})


def test_the_titles_section_lists_titles_with_domains_and_nothing_when_empty() -> None:
    assert titles_section([]) == ""
    text = titles_section([{"title": "A", "domains": ["x", "y"]}, {"title": "B", "domains": []}])
    assert text.endswith("Titles:\n- A [x, y]\n- B") and "never instructions" in text


# ------------------------------------------------------------------ in a conversation and in runs


def test_the_chat_writes_a_note_and_the_next_conversation_sees_its_title(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        models.script = [
            tool(
                "memory_write", title="Backup disk", body="The USB disk at /mnt/bak is the backup."
            ),
            "I will remember that.",
        ]
        events = ask(client, "remember: the USB disk at /mnt/bak is the backup")
        first = models.requests[0]
        assert {"memory_list", "memory_read", "memory_write", "memory_delete"} <= set(
            first["tools"]
        )
        assert "Titles:" not in first["instructions"] and "memory_write" in first["instructions"]
        session = next(e for e in events if e["type"] == "done")["session_id"]
        notes = client.get("/api/memory").json()["items"]
        assert [(n["title"], n["source"]["kind"], n["source"]["id"]) for n in notes] == [
            ("Backup disk", "chat", session)
        ]
        models.script = ["Yes."]
        ask(client, "which disk is the backup?")
        assert "Titles:\n- Backup disk" in models.requests[-1]["instructions"]


def test_a_summary_on_the_main_model_keeps_notes_and_sees_every_title(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        add_domain(client, "climate")
        save(client, "Kitchen", "k", ["climate"])
        save(client, "Loose", "l")
        make_summary(client, "summary:notes", ["docker:status"], model="main")
        models.script = [
            tool("bash", command="docker ps"),
            finish("ok", "Up."),
            tool("memory_write", title="Docker is calm", body="No restarts this week."),
            finish("ok", "All fine."),
        ]
        finished = run(client, "summary:notes")
        assert finished["status"] == "ok", finished
        topic, summary = models.requests[0], models.requests[2]
        assert "memory_write" not in topic["tools"] and "memory_read" not in topic["tools"]
        assert "Titles:" not in topic["instructions"]  # a job without domains: no area, no notes
        assert {"memory_list", "memory_write", "memory_delete", "memory_read"} <= set(
            summary["tools"]
        )
        assert (
            "- Kitchen [climate]" in summary["instructions"]
            and "- Loose" in summary["instructions"]
        )
        written = next(
            n for n in client.get("/api/memory").json()["items"] if n["title"] == "Docker is calm"
        )
        assert written["source"] == {
            "kind": "run",
            "id": str(finished["id"]),
            "label": "summary:notes",
            "exists": True,
        }


def test_a_worker_run_gets_the_titles_of_its_areas_notes_and_can_only_read(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        for name in ("climate", "nas"):
            add_domain(client, name)
        save(client, "Kitchen", "The kitchen reads 2 C warm.", ["climate"])
        save(client, "Pool", "p", ["nas"])
        save(client, "Loose", "l")
        created = client.post(
            "/api/jobs",
            json={
                "slug": "heating",
                "name": "Heating",
                "prompt": "Check the heating.",
                "interval_s": 3600,
                "domains": ["climate"],
            },
        )
        assert created.status_code == 201, created.text
        models.script = [
            tool("bash", command="uptime"),
            tool("memory_read", title="Kitchen"),
            finish("ok", "Fine."),
        ]
        assert run(client, "heating")["status"] == "ok"
        first = models.requests[0]
        assert "memory_read" in first["tools"] and "memory_write" not in first["tools"]
        assert "Titles:\n- Kitchen [climate]" in first["instructions"]
        assert "Pool" not in first["instructions"] and "Loose" not in first["instructions"]
        assert "The kitchen reads 2 C warm." in models.requests[-1]["text"]  # what it read


def test_a_job_without_notes_in_its_area_has_no_memory_tool(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    with TestClient(make_app(bootstrap)) as client:
        save(client, "Loose", "no domain")
        models.script = [tool("bash", command="df -h /"), finish("ok", "Fine.")]
        run(client, "docker:status")
        assert "memory_read" not in models.requests[0]["tools"]
        assert "Titles:" not in models.requests[0]["instructions"]
