"""The trace as wired into the app: every caller tags its calls, chat turns read as a timeline,
settings switch it."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap, Settings

from .conftest import LLM_URL
from .test_chat import FakeLLM, ask, call, completion, make_app, say


def flush(client: TestClient, app: Any) -> None:
    client.portal.call(app.state.tracing.flush)  # type: ignore[union-attr]


def calls(client: TestClient, **params: Any) -> list[dict[str, Any]]:
    body = client.get("/api/logs/llm", params={"limit": 100, **params}).json()
    return sorted(body["items"], key=lambda c: c["id"])


def test_a_chat_turn_with_a_tool_call_is_one_timeline(
    bootstrap: Bootstrap, shell: list[str]
) -> None:
    llm = FakeLLM(
        call("bash", {"command": "df -h /"}),
        say("The root disk is 40% full."),
    )
    llm.title_reply = "Memory hogs"
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        events = ask(client, "how full is the disk?")
        done = next(e for e in events if e["type"] == "done")
        assert done["trace_turn"]
        flush(client, app)

        rows = calls(client, link_kind="chat")
        assert [(r["purpose"], r["link_step"]) for r in rows if r["purpose"] == "chat"] == [
            ("chat", 1),
            ("chat", 2),
        ]
        assert {r["turn"] for r in rows if r["purpose"] == "chat"} == {done["trace_turn"]}
        assert {r["link_id"] for r in rows} == {done["session_id"]}
        title = [r for r in rows if r["purpose"] == "chat_title"]
        assert len(title) == 1 and title[0]["turn"] == done["trace_turn"]

        first = next(r for r in rows if r["link_step"] == 1)
        detail = client.get(f"/api/logs/llm/{first['id']}").json()
        assert detail["messages"][0]["role"] == "system"
        assert detail["messages"][-1] == {
            "role": "user",
            "content": "how full is the disk?",
        }
        assert {t["function"]["name"] for t in detail["tools"]} >= {"bash", "delegate"}
        assert detail["tool_calls"][0]["name"] == "bash"
        assert detail["stream"] is True and detail["ttft_ms"] is not None
        assert detail["prompt_tokens"] == 10 and detail["completion_tokens"] == 5

        timeline = client.get(f"/api/logs/llm/{first['id']}/timeline").json()["entries"]
        kinds = [(e["type"], e.get("tool") or e["link_step"]) for e in timeline]
        assert kinds[:3] == [("llm", 1), ("tool", "bash"), ("llm", 2)]
        tool = next(e for e in timeline if e["type"] == "tool")
        assert tool["ok"] is True and tool["result_size"] > 50 and tool["duration_ms"] is not None
        tool_detail = client.get(f"/api/logs/tools/{tool['id']}").json()
        assert tool_detail["args"] == {"command": "df -h /"}
        assert "/dev/sda1" in json.dumps(tool_detail["result"])

        # The chat answer carries the turn id, for the "trace" link of the chat page.
        session = client.get(f"/api/chat/sessions/{done['session_id']}").json()
        assert session["messages"][-1]["meta"]["trace_turn"] == done["trace_turn"]


def test_a_failing_tool_shows_up_failed_in_the_timeline(
    bootstrap: Bootstrap, shell: list[str]
) -> None:
    llm = FakeLLM(call("bash", {"command": ""}), say("I could not do that."))
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        events = ask(client, "do the impossible")
        flush(client, app)
        first = calls(client, purpose="chat")[0]
        timeline = client.get(f"/api/logs/llm/{first['id']}/timeline").json()["entries"]
        tool = next(e for e in timeline if e["type"] == "tool")
        assert tool["tool"] == "bash" and tool["ok"] is False
        assert "needs `command`" in tool["error"]
        assert events[-1]["type"] == "done"


def test_the_settings_llm_test_is_recorded_as_a_test(bootstrap: Bootstrap) -> None:
    from ai_server_monitor.main import create_app

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        if len(seen) == 0:
            seen.append(request)
            return completion(call("get_check_word", {}), 7, 1)
        return completion(say("OK"), 7, 1)

    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(handler),
        seed_settings=Settings.model_validate({"llm": {"base_url": LLM_URL, "model": "fake"}}),
    )
    with TestClient(app) as client:
        body = {"base_url": LLM_URL, "model": "fake"}
        assert client.post("/api/settings/test-llm", json=body).json()["ok"] is True
        flush(client, app)
        assert [c["purpose"] for c in calls(client)] == ["test", "test"]  # tool call, answer


def test_llm_trace_can_be_switched_off_and_bodies_dropped(bootstrap: Bootstrap) -> None:
    settings = {
        "llm": {"base_url": LLM_URL, "model": "fake", "max_retries": 0},
        "logs": {"llm_trace": False},
    }
    llm = FakeLLM(say("hello"))
    app = make_app_with(bootstrap, llm, settings)
    with TestClient(app) as client:
        ask(client, "hi")
        flush(client, app)
        assert calls(client) == []
        stats = client.get("/api/logs/llm/stats").json()
        assert stats["enabled"] is False

        # Turn it on, metadata only, through the settings API (no restart needed).
        values = client.get("/api/settings").json()["values"]
        values["logs"].update(llm_trace=True, store_bodies=False, retention="2d", max_total_mb=50)
        assert client.put("/api/settings", json=values).status_code == 200
        llm.script.append(say("again"))
        ask(client, "hi again")
        flush(client, app)
        (row,) = [c for c in calls(client) if c["purpose"] == "chat"]
        assert row["bodies"] is False and row["prompt_tokens"] == 10
        assert client.get(f"/api/logs/llm/{row['id']}").json()["messages"] is None
        stats = client.get("/api/logs/llm/stats").json()
        assert (stats["enabled"], stats["store_bodies"]) == (True, False)
        assert (stats["retention_days"], stats["max_total_mb"]) == (2, 50)


def make_app_with(bootstrap: Bootstrap, llm: FakeLLM, settings: dict[str, Any]) -> Any:
    from ai_server_monitor.main import create_app

    return create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(llm.handler),
        seed_settings=Settings.model_validate(settings),
    )


def test_app_log_is_attached_for_the_app_lifetime(bootstrap: Bootstrap) -> None:
    import logging

    app = make_app(bootstrap, FakeLLM())
    with TestClient(app) as client:
        logging.getLogger("ai_server_monitor.test_marker").warning("marker line from the test")
        body = client.get("/api/logs/app", params={"q": "marker line"}).json()
        assert [r["message"] for r in body["items"]] == ["marker line from the test"]
        client.portal.call(app.state.tracing.applog.flush)  # type: ignore[union-attr]
        stored = client.get("/api/logs/app", params={"source": "stored", "q": "marker line"}).json()
        assert len(stored["items"]) == 1
    logging.getLogger("ai_server_monitor.test_marker").warning("after shutdown")
    assert not app.state.tracing.applog.ring.query(q="after shutdown")
