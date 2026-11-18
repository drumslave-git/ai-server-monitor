"""MCP tools in the real chat engine and action layer (fake LLM, real local MCP server)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from ai_server_monitor.actions import ActionDef, ActionError, ActionService
from ai_server_monitor.chat import audit
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.context import AppContext

from .conftest import LLM_URL
from .mcp_fake import FakeMcp
from .test_chat import FakeLLM, call, completion, say, sse_body


class McpLLM(FakeLLM):
    """FakeLLM that answers the MCP side calls itself, outside the script: the sorting of
    tools into domains (`sort_into(tool)` names the domain; default: all into 'home') and the
    classification of a request (`picks`; None = every domain offered)."""

    def __init__(self, *script: Any):
        super().__init__(*script)
        self.sort_into = lambda tool: "home"
        self.picks: list[str] | None = None
        self.sort_requests: list[dict[str, Any]] = []
        self.classify_requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            system = str(body["messages"][0]["content"])
            if system.startswith("You sort the tools"):
                self.sort_requests.append(body)
                return self._answer(body, self._sorting(body))
            if system.startswith("Pick the domains"):
                self.classify_requests.append(body)
                text = body["messages"][-1]["content"]
                offered = re.findall(r"^- ([a-z0-9-]+):", text.split("Request:")[0], re.M)
                picks = offered if self.picks is None else self.picks
                return self._answer(body, call("final_result", {"response": picks}))
        return super().handler(request)

    def _sorting(self, body: dict[str, Any]) -> dict[str, Any]:
        text = body["messages"][-1]["content"]
        names = re.findall(r"^- (\S+?):", text.split("Tools to sort:")[1], re.M)
        groups: dict[str, list[str]] = {}
        for name in names:
            groups.setdefault(self.sort_into(name.partition("_")[2]), []).append(name)
        out = [{"name": d, "description": f"All about {d}", "tools": t} for d, t in groups.items()]
        return call("final_result", {"response": out})

    @staticmethod
    def _answer(body: dict[str, Any], message: dict[str, Any]) -> httpx.Response:
        if body.get("stream"):
            return httpx.Response(
                200, content=sse_body(message), headers={"content-type": "text/event-stream"}
            )
        return completion(message)


def settings_for(url: str | None, **mcp: Any) -> Settings:
    server = {
        "name": "home",
        "url": url or "http://127.0.0.1:1/mcp",
        "transport": "streamable_http",
        "timeout": "5s",
        "tool_filter": ["get_temperature", "set_heating"],
    }
    return Settings.model_validate(
        {
            "llm": {"base_url": LLM_URL, "model": "fake", "max_retries": 0},
            "mcp": {"servers": [server], **mcp},
        }
    )


@pytest.fixture
async def fake() -> AsyncIterator[FakeMcp]:
    server = FakeMcp("streamable_http")
    await server.start()
    yield server
    await server.stop()


async def start_app(tmp_path: Path, llm: FakeLLM, settings: Settings) -> AsyncIterator[AppContext]:
    app = AppContext(
        Bootstrap(data_dir=tmp_path / "data", host_root=tmp_path / "no-host"),
        llm_transport=httpx.MockTransport(llm.handler),
        seed_settings=settings,
    )
    await app.start()
    try:
        yield app
    finally:
        await app.stop()


async def ask(app: AppContext, message: str) -> list[dict[str, Any]]:
    return [event async for event in app.chat.run(message)]


def used_tools(events: list[dict[str, Any]]) -> list[str]:
    return [e["name"] for e in events if e["type"] == "tool_call"]


async def test_read_tool_is_answered_directly_and_audited(tmp_path: Path, fake: FakeMcp) -> None:
    llm = McpLLM(call("home_get_temperature", {"room": "kitchen"}), say("It is 21.5 C."))
    async for app in start_app(tmp_path, llm, settings_for(fake.url)):
        events = await ask(app, "how warm is the kitchen?")
        assert used_tools(events) == ["home_get_temperature"]
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["ok"] and "21.5 C in kitchen" in result["preview"]
        assert events[-1]["type"] == "done" and "21.5" in events[-1]["text"]
        # the model saw the tools in the first request, the result wrapped as untrusted data
        first, second = llm.requests
        names = {t["function"]["name"] for t in first["tools"]}
        assert {"home_get_temperature", "home_set_heating"} <= names
        assert "MCP servers" in first["messages"][0]["content"]
        tool_message = next(m for m in second["messages"] if m["role"] == "tool")
        assert 'trust="untrusted"' in tool_message["content"]
        assert (await app.actions.list_actions())["total"] == 0  # reads never touch the log
        rows = await audit.recent(app.db, kind="mcp")
        assert len(rows) == 1
        assert rows[0]["server"] == "home" and rows[0]["tool"] == "get_temperature"
        assert rows[0]["ok"] is True and rows[0]["source"].startswith("chat:")


async def test_write_tool_becomes_a_pending_approval_then_runs_on_approve(
    tmp_path: Path, fake: FakeMcp
) -> None:
    llm = McpLLM(
        call("home_set_heating", {"room": "bedroom", "on": False}),
        say("I asked for approval."),
    )
    async for app in start_app(tmp_path, llm, settings_for(fake.url)):
        events = await ask(app, "turn the bedroom heating off")
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["ok"] and "Waiting for approval #" in result["preview"]
        assert events[-1]["text"].startswith("Waiting for approval #")  # the run stopped here
        session_id = events[-1]["session_id"]
        assert not any(name == "set_heating" for name, _ in fake.calls)  # nothing sent yet
        pending = await app.actions.pending()
        assert len(pending) == 1
        record = pending[0]
        assert record["action"] == "mcp_call" and record["source"].startswith("chat:")
        assert record["target"] == "home · set_heating"
        assert record["args"]["arguments"] == {"room": "bedroom", "on": False}
        # approve → executed on the server (mcp.dry_run is off by default)
        done = await app.actions.decide(record["id"], "approve")
        assert done["status"] == "executed", done
        assert done["result"]["result"] == "heating in bedroom is now off"
        assert ("set_heating", {"room": "bedroom", "on": False}) in fake.calls
        # ... and the chat continues by itself with the result
        history: list[dict[str, str]] = []
        for _ in range(200):
            await asyncio.gather(*list(app.chat._background))
            history = await app.chat.store.history(session_id, 10)
            if history[-1]["content"] == "I asked for approval.":
                break
            await asyncio.sleep(0.02)
        assert history[-1]["content"] == "I asked for approval."
        assert "heating in bedroom is now off" in json.dumps(llm.requests[-1]["messages"])


async def test_mcp_dry_run_is_its_own_switch(tmp_path: Path, fake: FakeMcp) -> None:
    llm = McpLLM()
    settings = settings_for(fake.url, dry_run=True)
    settings.mcp.servers[0].default_policy = "auto"
    async for app in start_app(tmp_path, llm, settings):
        record = await app.actions.request(
            "mcp_call",
            {"server": "home", "tool": "set_heating", "arguments": {"room": "a", "on": True}},
            "test",
        )
        assert record["status"] == "dry_run" and not fake.calls


async def test_policies_and_checks_of_the_mcp_action(tmp_path: Path, fake: FakeMcp) -> None:
    llm = McpLLM()
    settings = settings_for(fake.url)
    raw = settings.model_dump(mode="json", context={"reveal_secrets": True})
    raw["mcp"]["servers"][0]["tool_overrides"] = [{"name": "set_heating", "policy": "suggest"}]
    settings = Settings.model_validate(raw)
    async for app in start_app(tmp_path, llm, settings):
        args = {"server": "home", "tool": "set_heating", "arguments": {"room": "a", "on": True}}
        assert (await app.actions.request("mcp_call", args, "t"))["status"] == "suggested"
        other = {"server": "home", "tool": "slow", "arguments": {}}
        rejected = await app.actions.request("mcp_call", other, "t")
        assert (
            rejected["status"] == "rejected"
            and "not one of the tools" in rejected["result"]["detail"]
        )
        gone = await app.actions.request("mcp_call", {"server": "x", "tool": "y"}, "t")
        assert gone["status"] == "rejected"  # unknown server
        assert not fake.calls


async def test_dead_server_does_not_break_the_chat(tmp_path: Path) -> None:
    llm = McpLLM(say("Nothing wrong with the server."))
    async for app in start_app(tmp_path, llm, settings_for(None)):
        events = await ask(app, "hello")
        assert events[-1]["type"] == "done"
        names = {t["function"]["name"] for t in llm.requests[0]["tools"]}
        assert not any(n.startswith("home_") for n in names)
        assert "MCP server 'home' is unavailable" in json.dumps(llm.requests[0]["messages"])


async def test_settings_change_rebuilds_the_manager(tmp_path: Path, fake: FakeMcp) -> None:
    llm = McpLLM()
    async for app in start_app(tmp_path, llm, settings_for(fake.url)):
        old = app.mcp
        assert await app.actions.mcp.call("home", "get_temperature", {"room": "a"}, "t") == (
            "21.5 C in a"
        )
        await app.apply_settings(settings_for(fake.url, dry_run=True))
        assert app.mcp is not old and app.actions.mcp is app.mcp
        assert app.mcp.settings.dry_run is True


# --------------------------------------------------------------------------- the hooks


class Echo(BaseModel):
    text: str = ""
    title: str = ""


def custom_def(calls: list[str], **hooks: Any) -> ActionDef:
    async def run(service: ActionService, args: Echo) -> dict[str, Any]:
        calls.append(args.text)
        return {"echo": args.text, "has_service": service is not None}

    return ActionDef(
        name="echo",
        description="test",
        args_model=Echo,  # type: ignore[arg-type]
        policy_key=lambda a: "echo",
        target=lambda a: f"echo: {a.text}",
        run=run,
        chat_tool=False,
        **hooks,
    )


async def test_action_hooks_policy_cap_dry_run_and_run(db: Any) -> None:
    calls: list[str] = []
    policy = {"value": "auto"}
    switches = {"dry": False}
    cap = {"n": 2}
    definition = custom_def(
        calls,
        policy_of=lambda settings, args: policy["value"],
        max_per_hour_of=lambda settings, args: cap["n"],
        uses_dry_run=lambda settings: switches["dry"],
    )
    service = ActionService(db, Settings(), registry={"echo": definition})
    first = await service.request("echo", {"text": "a"}, "t")
    assert first["status"] == "executed" and first["result"]["echo"] == "a"
    await service.request("echo", {"text": "b"}, "t")
    capped = await service.request("echo", {"text": "c"}, "t")
    assert capped["status"] == "queued" and "2 per hour" in capped["result"]["detail"]
    assert calls == ["a", "b"]
    cap["n"] = 0
    policy["value"] = "approve"
    pending = await service.request("echo", {"text": "d"}, "t")
    assert pending["status"] == "pending"
    switches["dry"] = True
    dry = await service.decide(pending["id"], "approve")
    assert dry["status"] == "dry_run" and calls == ["a", "b"]
    policy["value"] = "off"
    assert (await service.request("echo", {"text": "e"}, "t"))["status"] == "rejected"


async def test_unknown_action_still_raises(db: Any) -> None:
    with pytest.raises(ActionError):
        await ActionService(db, Settings(), registry={}).request("nope", {}, "t")


async def test_settings_test_endpoint_unmasks_the_stored_token() -> None:
    from ai_server_monitor.conntest import run_test

    server = FakeMcp("streamable_http", token="tok-stored-123456")
    await server.start()
    try:
        stored = Settings.model_validate(
            {
                "mcp": {
                    "servers": [{"name": "home", "url": server.url, "token": "tok-stored-123456"}]
                }
            }
        )
        body = stored.public_dict()["mcp"]["servers"][0]  # the token is masked, as in the form
        assert "tok-stored" not in json.dumps(body)
        result = await run_test("mcp", body, stored, index=0)
        assert result.ok, result.message
        assert result.details["server"]["name"] == "fake-home"
        assert {t["name"] for t in result.details["tools"]} >= {"get_temperature", "set_heating"}
        assert "tok-stored" not in json.dumps(result.details) + result.message
        wrong = {**body, "token": "wrong-token-000"}
        bad = await run_test("mcp", wrong, stored)
        assert not bad.ok and "could not be used" in bad.message
        invalid = await run_test("mcp", {"name": "x y", "url": "ftp://z"}, stored)
        assert not invalid.ok and invalid.message.startswith("Fix this first")
    finally:
        await server.stop()
