"""Settings "Structured output" (Main and Worker model): the helper calls (domain
classification, command marks, tool sorting) and the Settings test send `response_format` with
the JSON schema (json_schema, default) or use the `final_result` tool (tool_call). An endpoint
that rejects `response_format` leads to each call's existing failure path, with its text."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.chat.marks import MARKS_SYSTEM
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .mcp_fake import FakeMcp
from .test_chat import FakeLLM, call, completion, final, make_app, say, streamed
from .test_check_model import echo_shell, run_test, word_of  # noqa: F401 (fixture)
from .test_mcp_chat import McpLLM, ask, start_app
from .test_mcp_turns import domain_of, settings, turn_meta

REJECT = "response_format json_schema is not supported by this server"


@pytest.fixture
async def fake46() -> AsyncIterator[FakeMcp]:
    server = FakeMcp("streamable_http", extra_tools=40)
    await server.start()
    yield server
    await server.stop()


class Endpoint(McpLLM):
    """McpLLM that answers HTTP 400 to `response_format` for the calls named in `reject`
    ("sort", "classify", "marks")."""

    def __init__(self, *script: Any, reject: set[str] = frozenset()):  # type: ignore[assignment]
        super().__init__(*script)
        self.sort_into = domain_of
        self.reject = reject
        self.marks_requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            system = str(body["messages"][0]["content"])
            kind = (
                "sort"
                if system.startswith("You sort the tools")
                else "classify"
                if system.startswith("Pick the domains")
                else "marks"
                if system == MARKS_SYSTEM
                else None
            )
            if kind == "marks":
                self.marks_requests.append(body)
            if kind in self.reject and "response_format" in body:
                return httpx.Response(400, json={"error": {"message": REJECT}})
        return super().handler(request)


def schema_of(body: dict[str, Any]) -> dict[str, Any]:
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and "tools" not in body, body
    return fmt["json_schema"]["schema"]


def tool_used(body: dict[str, Any]) -> None:
    assert "response_format" not in body
    assert [t["function"]["name"] for t in body["tools"]] == ["final_result"]


def with_mode(url: str, mode: str) -> Settings:
    s = settings(url)
    s.llm.structured_output = mode  # type: ignore[assignment]
    return s


async def test_classify_and_sort_send_the_json_schema_and_none_is_an_empty_list(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = Endpoint(say("No tools needed."))
    llm.picks = []  # "no domain"
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        events = await ask(app, "hello")
        assert events[-1]["text"] == "No tools needed."
        sort_schema = schema_of(llm.sort_requests[0])
        assert sort_schema["required"] == ["response"]
        classify_schema = schema_of(llm.classify_requests[0])
        assert classify_schema["properties"]["response"]["type"] == "array"
        assert len(llm.classify_requests) == 1
        meta = (await turn_meta(app, events[-1]["session_id"]))["mcp_domains"]
        assert meta["picked"] == [] and meta.get("classify_error") is None


async def test_tool_call_setting_keeps_the_final_result_tool(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = Endpoint(say("It is warm."))
    llm.picks = ["climate"]
    async for app in start_app(tmp_path, llm, with_mode(fake46.url, "tool_call")):
        events = await ask(app, "how warm is it?")
        assert events[-1]["text"] == "It is warm."
        tool_used(llm.sort_requests[0])
        tool_used(llm.classify_requests[0])
        meta = (await turn_meta(app, events[-1]["session_id"]))["mcp_domains"]
        assert meta["picked"] == ["climate"]


async def test_a_rejected_classification_goes_on_with_domains_loadable(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = Endpoint(say("Fine."), reject={"classify"})
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        events = await ask(app, "how warm is it?")
        assert events[-1]["text"] == "Fine."
        meta = (await turn_meta(app, events[-1]["session_id"]))["mcp_domains"]
        assert meta["picked"] == []
        assert meta["classify_error"].startswith("classification failed: ")
        assert REJECT in meta["classify_error"]


async def test_a_rejected_sort_leaves_the_tools_unsorted_with_the_reason(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = Endpoint(say("Fine."), reject={"sort"})
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        await ask(app, "how warm is it?")
        rows = await app.db.fetchall("SELECT domain_id, sort_error FROM mcp_tools")
        assert rows and all(r["domain_id"] is None for r in rows)
        assert all(REJECT in (r["sort_error"] or "") for r in rows)
        assert llm.classify_requests == []  # nothing sorted: nothing to classify


ANSWER = "Check it:\n\n```bash\nuptime\n```"


@pytest.mark.parametrize("mode", ["json_schema", "tool_call"])
def test_marks_follow_the_setting(bootstrap: Bootstrap, mode: str) -> None:
    llm = FakeLLM(say(ANSWER), lambda body: final(body, {"kinds": ["read-only"]}))
    with TestClient(make_app(bootstrap, llm)) as client:
        values = client.get("/api/settings").json()["values"]
        values["llm"]["structured_output"] = mode
        assert client.put("/api/settings", json=values).status_code == 200
        response = client.post("/api/chat", json={"message": "load?"})
        assert response.status_code == 200
        session_id = next(
            json.loads(line[5:])["session_id"]
            for line in response.text.splitlines()
            if line.startswith("data:") and '"session_id"' in line
        )
        stored = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]
    marks = llm.requests[1]
    assert marks["messages"][0]["content"] == MARKS_SYSTEM
    if mode == "json_schema":
        assert schema_of(marks)["properties"]["kinds"]["type"] == "array"
    else:
        tool_used(marks)
    assert "# [read-only]\nuptime" in stored["content"]


def test_rejected_marks_leave_the_command_unmarked_with_the_reason(bootstrap: Bootstrap) -> None:
    class Rejecting(FakeLLM):
        def handler(self, request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                body = json.loads(request.content)
                if body["messages"][0]["content"] == MARKS_SYSTEM:
                    assert "response_format" in body
                    return httpx.Response(400, json={"error": {"message": REJECT}})
            return super().handler(request)

    llm = Rejecting(say(ANSWER))
    with TestClient(make_app(bootstrap, llm)) as client:
        response = client.post("/api/chat", json={"message": "load?"})
        session_id = next(
            json.loads(line[5:])["session_id"]
            for line in response.text.splitlines()
            if line.startswith("data:") and '"session_id"' in line
        )
        stored = client.get(f"/api/chat/sessions/{session_id}").json()["messages"][-1]
    assert "[read-only]" not in stored["content"]
    unmarked = stored["meta"]["unmarked"]
    assert unmarked["commands"] == 1 and REJECT in unmarked["error"]


def test_the_settings_test_reports_structured_output(
    bootstrap: Bootstrap,
    echo_shell: list[str],  # noqa: F811
) -> None:
    llm = FakeLLM(
        lambda body: call("bash", {"command": f"echo {word_of(body)}"}),
        lambda body: say(word_of(body)),
        lambda body: final(body, {"word": "x"}),
    )
    with TestClient(make_app(bootstrap, llm)) as client:
        result = run_test(client)
    assert result["ok"] is True
    assert result["structured_output"] == {"ok": True, "text": "structured output OK (JSON schema)"}
    assert llm.requests[2]["response_format"]["type"] == "json_schema"


def test_the_settings_test_names_a_rejected_schema_and_the_way_back(
    bootstrap: Bootstrap,
    echo_shell: list[str],  # noqa: F811
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(404)
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": REJECT}})
        if "Answer with the word you are given" in str(body["messages"][0]["content"]):
            return completion(call("final_result", {"word": "x"}))  # tool_call: the tool
        if not any(m["role"] == "tool" for m in body["messages"]):
            return streamed(call("bash", {"command": f"echo {word_of(body)}"}))
        return streamed(say("done"))

    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(handler),
        seed_settings=Settings.model_validate({"llm": {"base_url": LLM_URL, "model": "fake"}}),
    )
    with TestClient(app) as client:
        result = run_test(client)
        tool_mode = run_test(client, structured_output="tool_call")
    assert result["ok"] is True and result["structured_output"]["ok"] is False
    text = result["structured_output"]["text"]
    assert text.startswith("structured output failed (JSON schema): ") and REJECT in text
    assert 'Set "Structured output" to tool_call' in text
    # switched back to tool_call, the same endpoint passes
    assert tool_mode["structured_output"] == {
        "ok": True,
        "text": "structured output OK (tool call)",
    }


def test_the_setting_round_trips_for_both_profiles(bootstrap: Bootstrap) -> None:
    with TestClient(create_app(bootstrap)) as client:
        doc = client.get("/api/settings").json()
        values = doc["values"]
        assert values["llm"]["structured_output"] == "json_schema"
        assert values["worker"]["structured_output"] == "json_schema"
        values["llm"]["structured_output"] = "tool_call"
        values["worker"]["structured_output"] = "tool_call"
        assert client.put("/api/settings", json=values).status_code == 200
        values["worker"]["structured_output"] = "both"
        assert client.put("/api/settings", json=values).status_code == 422
    with TestClient(create_app(bootstrap)) as client:
        again = client.get("/api/settings").json()["values"]
    assert again["llm"]["structured_output"] == "tool_call"
    assert again["worker"]["structured_output"] == "tool_call"
