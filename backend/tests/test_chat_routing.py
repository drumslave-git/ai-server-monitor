"""Tool routing: only the tool groups a question needs go to the model (chat/routing.py)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.chat import routing
from ai_server_monitor.chat.tools import build_tools
from ai_server_monitor.config import Bootstrap, Settings

from .test_chat import FakeLLM, answer, ask, call, make_app, prepare, say

# --------------------------------------------------------------------------- the guard test


def _every_tool() -> dict[str, Any]:
    """The tools of a fully configured install (Prometheus, Docker, shell)."""
    settings = Settings(collectors={"prometheus": {"url": "http://prom:9090"}})
    return build_tools(settings, assume_linux=True)


def test_every_tool_belongs_to_a_group() -> None:
    tools = _every_tool()
    assert len(tools) > 10  # (the guard would pass vacuously on an empty set)
    assert routing.ungrouped(tools, mcp_servers=["any"]) == [], (
        "a tool without a group is never sent when routing is on: add it to TOOL_GROUPS"
    )


def test_group_table_has_no_stale_names() -> None:
    names = set(_every_tool())
    assert set(routing.TOOL_GROUPS) <= names, set(routing.TOOL_GROUPS) - names


def test_groups_of_and_mcp_tools() -> None:
    assert routing.groups_of("get_status") == () and routing.groups_of(routing.MORE_TOOLS) == ()
    assert routing.groups_of("disk_usage") == ("host",)
    assert routing.groups_of("grafana__query") == ("mcp:grafana",)
    assert routing.groups_of("mcp_call", ["a", "b"]) == ("mcp:a", "mcp:b")
    assert routing.groups_of("totally_new_tool") == ()
    assert routing.ungrouped(["totally_new_tool", "get_status"]) == ["totally_new_tool"]
    found = routing.catalog(_every_tool(), [])
    assert set(found) == {"host", "metrics", "containers", "actions"}
    assert "promql_query" in found["metrics"] and "run_command" in found["host"]


def test_the_classifier_reply_is_parsed_strictly() -> None:
    known = ["host", "containers", "mcp:ha"]
    assert routing.parse_groups('{"groups": ["host", "containers"]}', known) == [
        "host",
        "containers",
    ]
    assert routing.parse_groups('```json\n{"groups": ["CONTAINERS"]}\n```', known) == ["containers"]
    assert routing.parse_groups('["host"]', known) == ["host"]
    assert routing.parse_groups('{"groups": "host"}', known) == ["host"]
    assert routing.parse_groups('{"groups": ["host", "nonsense", "host"]}', known) == ["host"]
    assert routing.parse_groups('{"groups": []}', known) == []
    assert routing.parse_groups('<think>hm</think>{"groups": ["mcp:ha"]}', known) == ["mcp:ha"]
    assert routing.parse_groups("I would pick host.", known) is None
    assert routing.parse_groups('{"other": 1}', known) is None


def test_auto_routing_only_above_the_token_budget() -> None:
    small = [{"type": "function", "function": {"name": "x", "description": "d"}}]
    big = [{"type": "function", "function": {"name": "x", "description": "d" * 4000}}]
    assert not routing.wanted("off", big, 100)
    assert routing.wanted("always", small, 100_000)
    assert not routing.wanted("auto", small, 500) and routing.wanted("auto", big, 500)


def test_setting_defaults_and_validation() -> None:
    chat = Settings().chat
    assert chat.tool_routing == "auto" and chat.tool_routing_tokens == 2000
    with pytest.raises(ValueError):
        Settings.model_validate({"chat": {"tool_routing": "sometimes"}})


# --------------------------------------------------------------------------- in a chat


def tool_names(request: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools") or []}


def routed_app(bootstrap: Bootstrap, llm: FakeLLM, mode: str = "always") -> Any:
    return make_app(bootstrap, llm, chat={"tool_routing": mode})


def test_only_the_chosen_group_is_sent_and_the_choice_is_traced(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("Ollama."))
    llm.route_reply = '{"groups": ["host"]}'
    app = routed_app(bootstrap, llm)
    with TestClient(app) as client:
        prepare(client, app)
        events = ask(client, "what uses the most memory?")
        (request,) = llm.requests
        names = tool_names(request)
        assert {"get_status", "more_tools", "get_collector_data"} <= names
        assert not names & {"container_inspect", "promql_query", "list_recent_actions"}
        system = request["messages"][0]["content"]
        assert "more_tools" in system and "host" in system
        assert len(llm.route_requests) == 1
        question = llm.route_requests[0]["messages"][-1]["content"]
        assert question == "what uses the most memory?"
        route = next(e for e in events if e["type"] == "route")
        assert route["groups"] == ["host"] and route["classifier_s"] >= 0
        assert events[-1]["route"]["groups"] == ["host"]
        # the classifier call is its own trace purpose, tied to the same chat turn
        calls = client.get("/api/logs/llm", params={"purpose": "chat_route"}).json()
        assert calls["total"] == 1
        assert answer(events) == "Ollama."


def test_more_tools_loads_a_group_in_the_same_answer(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(
        call("more_tools", {"group": "containers"}),
        call("container_inspect", {"name": "nginx"}, "c2"),
        say("It is running."),
    )
    llm.route_reply = '{"groups": ["host"]}'
    app = routed_app(bootstrap, llm)
    with TestClient(app) as client:
        prepare(client, app)
        events = ask(client, "is nginx ok?")
        first, second, third = llm.requests
        assert "container_inspect" not in tool_names(first)
        assert "container_inspect" in tool_names(second) and "container_inspect" in tool_names(
            third
        )
        loaded = next(e for e in events if e["type"] == "tool_result")
        assert loaded["ok"] and "container_inspect" in loaded["preview"]
        assert events[-1]["route"]["loaded_later"] == ["containers"]
        assert answer(events) == "It is running."


def test_a_tool_that_is_not_loaded_says_how_to_get_it(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(call("container_inspect", {"name": "nginx"}), say("ok"))
    llm.route_reply = '{"groups": ["host"]}'
    app = routed_app(bootstrap, llm)
    with TestClient(app) as client:
        prepare(client, app)
        events = ask(client, "nginx?")
        result = next(e for e in events if e["type"] == "tool_result")
        assert not result["ok"]
        assert "more_tools" in result["preview"] and "containers" in result["preview"]


def test_follow_ups_keep_the_groups_in_use(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(
        call("more_tools", {"group": "containers"}),
        call("container_inspect", {"name": "nginx"}, "c2"),
        say("Fine."),
        say("Same there."),
    )
    llm.route_reply = '{"groups": ["host"]}'
    app = routed_app(bootstrap, llm)
    with TestClient(app) as client:
        prepare(client, app)
        first = ask(client, "nginx?")
        llm.route_reply = '{"groups": []}'
        second = ask(client, "and the other one?", first[0]["session_id"])
        last = llm.requests[-1]
        assert "container_inspect" in tool_names(last)  # the containers tool used before stays
        assert second[-1]["route"]["groups"] == ["containers"]
        used = llm.route_requests[-1]["messages"][0]["content"]
        assert "already used in this conversation: containers" in used


def test_an_unusable_classifier_reply_loads_every_tool(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("fine"))
    llm.route_reply = "I think the host."
    app = routed_app(bootstrap, llm)
    with TestClient(app) as client:
        prepare(client, app)
        events = ask(client, "anything")
        names = tool_names(llm.requests[0])
        assert {"container_inspect", "get_collector_data", "more_tools"} <= names
        assert "fallback" in events[-1]["route"]


def test_a_failing_classifier_call_loads_every_tool(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("fine"))
    llm.route_reply = lambda body: httpx.Response(500, json={"error": "boom"})
    app = routed_app(bootstrap, llm)
    with TestClient(app) as client:
        prepare(client, app)
        events = ask(client, "anything")
        assert "container_inspect" in tool_names(llm.requests[0])
        assert answer(events) == "fine"


def test_routing_off_sends_everything_and_makes_no_extra_call(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("fine"))
    app = routed_app(bootstrap, llm, mode="off")
    with TestClient(app) as client:
        prepare(client, app)
        events = ask(client, "anything")
        names = tool_names(llm.requests[0])
        assert "container_inspect" in names and "more_tools" not in names
        assert not llm.route_requests and not [e for e in events if e["type"] == "route"]
        assert "route" not in events[-1]


def test_auto_routing_is_skipped_while_the_tools_fit_the_budget(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(say("a"), say("b"))
    llm.route_reply = '{"groups": ["host"]}'
    app = make_app(
        bootstrap,
        llm,
        chat={"tool_routing": "auto", "tool_routing_tokens": 100_000},
    )
    with TestClient(app) as client:
        prepare(client, app)
        ask(client, "q")
        assert not llm.route_requests and "container_inspect" in tool_names(llm.requests[0])
    llm2 = FakeLLM(say("c"))
    llm2.route_reply = '{"groups": ["host"]}'
    app2 = make_app(
        bootstrap,
        llm2,
        chat={"tool_routing": "auto", "tool_routing_tokens": 200},
    )
    with TestClient(app2) as client:
        prepare(client, app2)
        ask(client, "q")
        assert len(llm2.route_requests) == 1
        assert "container_inspect" not in tool_names(llm2.requests[0])


def test_json_protocol_lists_only_the_loaded_tools(bootstrap: Bootstrap) -> None:
    llm = FakeLLM(
        say(json.dumps({"tool": "more_tools", "args": {"group": "containers"}})),
        say("done"),
    )
    llm.route_reply = '{"groups": ["host"]}'
    app = make_app(bootstrap, llm, mode="json", chat={"tool_routing": "always"})
    with TestClient(app) as client:
        prepare(client, app)
        ask(client, "nginx?")
        first, second = llm.requests
        before = first["messages"][0]["content"]
        after = second["messages"][0]["content"]
        assert "- get_collector_data:" in before and "- container_inspect:" not in before
        assert "- container_inspect:" in after
