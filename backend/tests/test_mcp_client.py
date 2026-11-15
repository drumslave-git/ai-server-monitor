"""The MCP client against a real local MCP server, over both transports."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from ai_server_monitor.chat.tools import Tool, ToolContext, ToolError
from ai_server_monitor.config import Settings
from ai_server_monitor.mcp_client import (
    McpCallError,
    McpManager,
    McpServerSettings,
    McpSettings,
    McpToolOverride,
    McpUnavailable,
    build_mcp_tools,
    check_mcp_server,
    display_name,
)
from ai_server_monitor.mcp_client import actions as mcp_actions
from ai_server_monitor.mcp_client.tools import inline_tokens, kind_of, pick_tools

from .mcp_fake import SECRET_IN_RESULT, FakeMcp

TRANSPORTS = ["streamable_http", "sse"]
TOKEN = "tok-9f8e7d6c5b4a"


@pytest.fixture(params=TRANSPORTS)
async def fake(request: pytest.FixtureRequest) -> AsyncIterator[FakeMcp]:
    server = FakeMcp(request.param)
    await server.start()
    yield server
    await server.stop()


def cfg_for(fake: FakeMcp, **overrides: Any) -> McpServerSettings:
    values: dict[str, Any] = {
        "name": "home",
        "url": fake.url,
        "transport": fake.transport,
        "timeout": "10s",
        "tool_filter": ["get_temperature", "set_heating"],
    }
    values.update(overrides)
    return McpServerSettings.model_validate(values)


@pytest.fixture
async def manager_for(fake: FakeMcp) -> AsyncIterator[Callable[..., McpManager]]:
    made: list[McpManager] = []

    def make(**overrides: Any) -> McpManager:
        mcp_settings = overrides.pop("mcp_settings", {})
        manager = McpManager(
            McpSettings(servers=[cfg_for(fake, **overrides)], **mcp_settings), backoff_base=0.05
        )
        made.append(manager)
        return manager

    yield make
    for manager in made:
        await manager.close()


class FakeActions:
    """Stands in for ActionService: records requests, answers like a `pending` approval."""

    def __init__(self, status: str = "pending"):
        self.status = status
        self.requests: list[dict[str, Any]] = []

    async def request(
        self,
        action: str,
        args: dict[str, Any],
        source: str,
        *,
        reason: str | None = None,
        run_key: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        self.requests.append({"action": action, "args": args, "source": source})
        return {
            "id": len(self.requests),
            "batch_id": len(self.requests),
            "status": self.status,
            "policy": "approve",
            "target": f"{args['server']} · {args['tool']}",
            "result": None,
        }


def ctx_for(manager: McpManager, actions: FakeActions | None = None) -> ToolContext:
    app = SimpleNamespace(settings=Settings(), actions=actions or FakeActions(), mcp=manager)
    return ToolContext(app=app, session_id="s1")  # type: ignore[arg-type]


async def run_tool(tools: list[Tool], name: str, ctx: ToolContext, **args: Any) -> Any:
    tool = next(t for t in tools if t.name == name)
    return await tool.handler(ctx, tool.args_model.model_validate(args))


# --------------------------------------------------------------------------- discovery


async def test_check_lists_server_and_tools(fake: FakeMcp) -> None:
    result = await check_mcp_server(cfg_for(fake))
    assert result.ok, result.message
    assert "fake-home" in result.message
    assert result.details["server"]["name"] == "fake-home"
    assert result.details["transport"] == fake.transport
    rows = {r["name"]: r for r in result.details["tools"]}
    assert set(rows) >= {"get_temperature", "set_heating", "slow", "boom", "leak", "add_tool"}
    assert rows["get_temperature"]["read_only"] is True
    assert rows["get_temperature"]["kind"] == "read"
    assert rows["get_temperature"]["selected"] is True
    assert rows["set_heating"]["read_only"] is None
    assert rows["set_heating"]["kind"] == "write"
    assert rows["set_heating"]["selected"] is True
    assert rows["slow"]["selected"] is False
    assert rows["get_temperature"]["description"] == "Room temperature."
    assert all(r["schema_tokens"] > 0 for r in rows.values())
    assert result.details["schema_tokens_total"] == sum(r["schema_tokens"] for r in rows.values())


async def test_auto_transport_finds_the_right_one(fake: FakeMcp) -> None:
    result = await check_mcp_server(cfg_for(fake, transport="auto"))
    assert result.ok, result.message
    assert result.details["transport"] == fake.transport  # SSE servers answer 405 to POST /sse


async def test_check_reports_a_dead_server_in_words() -> None:
    cfg = McpServerSettings(name="x", url="http://127.0.0.1:1/mcp", timeout="3s")
    result = await check_mcp_server(cfg)
    assert not result.ok
    assert "127.0.0.1:1" in result.message
    assert "Traceback" not in result.message


async def test_check_rejected_credentials() -> None:
    server = FakeMcp("streamable_http", token=TOKEN)
    await server.start()
    try:
        bad = McpServerSettings(name="x", url=server.url, token="wrong-token-123", timeout="5s")
        result = await check_mcp_server(bad)
        assert not result.ok
        assert "credentials" in result.message and "401" in result.message
        assert "wrong-token-123" not in json.dumps(result.details) + result.message
        good = McpServerSettings(name="x", url=server.url, token=TOKEN, timeout="5s")
        assert (await check_mcp_server(good)).ok
    finally:
        await server.stop()


# --------------------------------------------------------------------------- calls


async def test_read_call_returns_text(manager_for: Callable[..., McpManager]) -> None:
    manager = manager_for()
    outcome = await manager.call("home", "get_temperature", {"room": "kitchen"})
    assert not outcome.is_error
    assert outcome.text == "21.5 C in kitchen"
    assert outcome.payload() == {"text": "21.5 C in kitchen"}


async def test_tool_error_is_an_outcome_not_an_exception(
    manager_for: Callable[..., McpManager],
) -> None:
    manager = manager_for(tool_filter=["boom"])
    outcome = await manager.call("home", "boom", {})
    assert outcome.is_error
    assert "exploded" in outcome.text


async def test_images_are_not_passed_on(manager_for: Callable[..., McpManager]) -> None:
    manager = manager_for(tool_filter=["image"])
    payload = (await manager.call("home", "image", {})).payload()
    assert payload["text"] == "caption"
    assert payload["not_shown"][0].startswith("[image image/png")


async def test_call_timeout_then_reconnects(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for(tool_filter=["slow", "get_temperature"], timeout="1s")
    with pytest.raises(McpCallError, match="timed out after 1s"):
        await manager.call("home", "slow", {"seconds": 5})
    # the connection was thrown away; the next call connects again and works
    outcome = await manager.call("home", "get_temperature", {"room": "hall"})
    assert outcome.text == "21.5 C in hall"


async def test_result_text_is_capped(manager_for: Callable[..., McpManager]) -> None:
    manager = manager_for()
    outcome = await manager.call("home", "get_temperature", {"room": "x" * 500})
    payload = outcome.payload(limit=100)
    assert len(payload["text"]) < 200
    assert "truncated" in payload["text"]


async def test_allow_list_is_enforced_by_the_manager(
    manager_for: Callable[..., McpManager],
) -> None:
    manager = manager_for(tool_filter=["get_temperature"])
    with pytest.raises(McpCallError, match="not one of the tools chosen"):
        await manager.call("home", "set_heating", {"room": "a", "on": True})
    with pytest.raises(McpCallError, match="no enabled MCP server"):
        await manager.call("nope", "get_temperature", {})
    manager = manager_for(tool_overrides=[{"name": "get_temperature", "policy": "off"}])
    with pytest.raises(McpCallError, match="policy"):
        await manager.call("home", "get_temperature", {"room": "a"})


async def test_audit_hook_sees_every_call_without_secrets(
    manager_for: Callable[..., McpManager],
) -> None:
    entries: list[dict[str, Any]] = []
    manager = manager_for(token=TOKEN)  # the fake does not check it; only redaction matters
    manager.audit = entries.append
    await manager.call("home", "get_temperature", {"room": f"x {TOKEN}"}, source="chat:s1")
    with pytest.raises(McpCallError):
        await manager.call("home", "slow", {"seconds": 1})
    assert [e["ok"] for e in entries] == [True, False]
    assert entries[0]["source"] == "chat:s1"
    assert entries[0]["server"] == "home" and entries[0]["tool"] == "get_temperature"
    assert TOKEN not in json.dumps(entries)
    assert entries[1]["error"]


# --------------------------------------------------------------------------- lifecycle


async def test_dead_server_is_unavailable_not_fatal(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for()
    await manager.prepare()
    assert manager.unavailable() == []
    assert {t.name for t in build_mcp_tools(manager)} == {
        "home__get_temperature",
        "home__set_heating",
    }
    await fake.stop()
    with pytest.raises(McpCallError):
        await manager.call("home", "get_temperature", {"room": "a"})
    assert manager.tools_of("home") is None
    assert build_mcp_tools(manager) == []
    with pytest.raises(McpUnavailable, match="unreachable"):
        await manager.call("home", "get_temperature", {"room": "a"})
    await manager.prepare()  # must not raise or hang
    assert "unavailable" in manager.unavailable()[0]
    status = {row["name"]: row for row in manager.status()}["home"]
    assert status["state"] == "unreachable" and status["error"]


async def test_server_coming_back_is_picked_up_after_backoff(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for()
    await manager.call("home", "get_temperature", {"room": "a"})
    port = fake.port
    await fake.stop()
    with pytest.raises(McpCallError):
        await manager.call("home", "get_temperature", {"room": "a"})
    with pytest.raises(McpUnavailable):
        await manager.call("home", "get_temperature", {"room": "a"})  # connect refused, backoff
    again = FakeMcp(fake.transport, port=port)
    await again.start()
    try:
        outcome = None
        for _ in range(60):
            try:
                outcome = await manager.call("home", "get_temperature", {"room": "b"})
                break
            except McpUnavailable:
                await asyncio.sleep(0.1)
        assert outcome is not None and outcome.text == "21.5 C in b"
        assert manager.unavailable() == []
    finally:
        await again.stop()


async def test_prepare_never_blocks_on_a_dead_server() -> None:
    manager = McpManager(
        McpSettings(
            servers=[
                McpServerSettings(name="dead", url="http://127.0.0.1:1/mcp", tool_filter=["a"])
            ]
        )
    )
    await asyncio.wait_for(manager.prepare(), 15)
    assert build_mcp_tools(manager) == []
    assert "dead" in manager.unavailable()[0]
    await manager.close()


async def test_tools_list_changed_refreshes_the_cache(
    manager_for: Callable[..., McpManager],
) -> None:
    manager = manager_for(tool_filter=["add_tool", "extra"])
    await manager.prepare()
    before = {t.name for t in manager.tools_of("home") or []}
    assert "extra" not in before
    await manager.call("home", "add_tool", {})
    for _ in range(50):
        names = {t.name for t in manager.tools_of("home") or []}
        if "extra" in names:
            break
        await asyncio.sleep(0.1)
    assert "extra" in names
    assert "home__extra" in {t.name for t in build_mcp_tools(manager)}


async def test_settings_change_closes_the_old_manager(
    manager_for: Callable[..., McpManager],
) -> None:
    manager = manager_for()
    await manager.call("home", "get_temperature", {"room": "a"})
    await manager.close()
    with pytest.raises(McpUnavailable, match="settings changed"):
        await manager.call("home", "get_temperature", {"room": "a"})


# --------------------------------------------------------------------------- chat tools


async def test_tools_are_built_from_the_chosen_list(
    manager_for: Callable[..., McpManager],
) -> None:
    manager = manager_for()
    await manager.prepare()
    tools = {t.name: t for t in build_mcp_tools(manager)}
    assert set(tools) == {"home__get_temperature", "home__set_heating"}
    assert tools["home__get_temperature"].kind == "read"
    assert tools["home__set_heating"].kind == "write"
    definition = tools["home__set_heating"].definition()["function"]
    assert definition["description"].startswith("WRITE [home]")
    assert definition["parameters"]["required"] == ["room", "on"]
    assert '"title"' not in json.dumps(definition["parameters"])
    assert display_name("home__set_heating") == "home · set_heating"


async def test_empty_filter_offers_nothing(manager_for: Callable[..., McpManager]) -> None:
    manager = manager_for(tool_filter=[])
    await manager.prepare()
    assert build_mcp_tools(manager) == []


async def test_read_tool_runs_directly_and_never_touches_the_action_layer(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for()
    await manager.prepare()
    actions = FakeActions()
    result = await run_tool(
        build_mcp_tools(manager),
        "home__get_temperature",
        ctx_for(manager, actions),
        room="kitchen",
    )
    assert result == {"text": "21.5 C in kitchen"}
    assert actions.requests == []
    assert ("get_temperature", {"room": "kitchen"}) in fake.calls


async def test_write_tool_only_files_an_action_request(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for()
    await manager.prepare()
    actions = FakeActions()
    result = await run_tool(
        build_mcp_tools(manager),
        "home__set_heating",
        ctx_for(manager, actions),
        room="bedroom",
        on=False,
    )
    assert actions.requests == [
        {
            "action": "mcp_call",
            "args": {
                "server": "home",
                "tool": "set_heating",
                "arguments": {"room": "bedroom", "on": False},
            },
            "source": "chat:s1",
        }
    ]
    assert result["status"] == "pending"
    assert "NOT been done" in result["note"]
    assert not any(name == "set_heating" for name, _ in fake.calls)  # nothing was sent


async def test_approved_write_is_executed_by_the_action_executor(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for()
    args = mcp_actions.McpCallArgs(
        server="home", tool="set_heating", arguments={"room": "a", "on": True}
    )
    assert mcp_actions.check(manager.settings, args) is None
    assert mcp_actions.policy_of(manager.settings, args) == "approve"
    assert mcp_actions.target(args) == "home · set_heating"
    result = await mcp_actions.execute(manager, args)
    assert result["result"] == {"text": "heating in a is now on"}
    assert ("set_heating", {"room": "a", "on": True}) in fake.calls


async def test_action_check_follows_the_current_settings(
    manager_for: Callable[..., McpManager],
) -> None:
    manager = manager_for(tool_filter=["get_temperature"])
    args = mcp_actions.McpCallArgs(server="home", tool="set_heating")
    assert "not one of the tools" in (mcp_actions.check(manager.settings, args) or "")
    gone = mcp_actions.McpCallArgs(server="nope", tool="x")
    assert mcp_actions.policy_of(manager.settings, gone) == "off"
    assert "no enabled" in (mcp_actions.check(manager.settings, gone) or "")


async def test_failed_write_is_an_action_error(manager_for: Callable[..., McpManager]) -> None:
    from ai_server_monitor.actions import ActionError

    manager = manager_for(tool_filter=["boom"])
    with pytest.raises(ActionError, match="exploded"):
        await mcp_actions.execute(manager, mcp_actions.McpCallArgs(server="home", tool="boom"))


async def test_policy_and_kind_overrides(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for(
        default_policy="suggest",
        tool_overrides=[
            {"name": "set_heating", "kind": "read"},  # the user says: this one is harmless
            {"name": "get_temperature", "policy": "off"},
        ],
    )
    await manager.prepare()
    cfg = manager.settings.server("home")
    assert cfg is not None and cfg.policy_of("get_temperature") == "off"
    tools = {t.name: t for t in build_mcp_tools(manager)}
    assert set(tools) == {"home__set_heating"}  # policy off hides the tool
    assert tools["home__set_heating"].kind == "read"
    actions = FakeActions()
    result = await run_tool(
        list(tools.values()), "home__set_heating", ctx_for(manager, actions), room="a", on=True
    )
    assert actions.requests == [] and result == {"text": "heating in a is now on"}
    # ... and a server that says read-only can be forced to write
    manager = manager_for(tool_overrides=[{"name": "get_temperature", "kind": "write"}])
    await manager.prepare()
    assert {t.name: t.kind for t in build_mcp_tools(manager)}["home__get_temperature"] == "write"


async def test_arguments_are_checked_against_the_schema(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for()
    await manager.prepare()
    actions = FakeActions()
    ctx = ctx_for(manager, actions)
    tools = build_mcp_tools(manager)
    with pytest.raises(ToolError, match="invalid arguments for set_heating"):
        await run_tool(tools, "home__set_heating", ctx, room="a")  # `on` missing
    with pytest.raises(ToolError, match="on: "):
        await run_tool(tools, "home__set_heating", ctx, room="a", on="maybe")
    assert actions.requests == [] and not fake.calls


async def test_failed_read_becomes_a_tool_error(manager_for: Callable[..., McpManager]) -> None:
    manager = manager_for(tool_filter=["boom", "leak"])
    await manager.prepare()
    tools = build_mcp_tools(manager)
    with pytest.raises(ToolError, match="exploded"):
        await run_tool(tools, "home__boom", ctx_for(manager))


async def test_many_tools_switch_to_the_two_meta_tools(
    fake: FakeMcp, manager_for: Callable[..., McpManager]
) -> None:
    manager = manager_for(mcp_settings={"max_inline_tool_tokens": 10})
    await manager.prepare()
    assert inline_tokens(manager) > 10
    tools = build_mcp_tools(manager)
    assert [t.name for t in tools] == ["mcp_list_tools", "mcp_call"]
    assert tools[1].kind == "write"  # one of the chosen tools changes things
    # ...but the chat labels each call by the tool it resolves to
    assert tools[1].call_kind({"server": "home", "tool": "get_temperature"}) == "read"
    assert tools[1].call_kind({"server": "home", "tool": "set_heating"}) == "write"
    assert tools[1].call_kind({"server": "home", "tool": "nope"}) == "write"
    ctx = ctx_for(manager)
    overview = await run_tool(tools, "mcp_list_tools", ctx)
    assert overview == {"servers": [{"name": "home", "tools": ["get_temperature", "set_heating"]}]}
    listing = await run_tool(tools, "mcp_list_tools", ctx, server="home")
    assert {(t["name"], t["kind"]) for t in listing["tools"]} == {
        ("get_temperature", "read"),
        ("set_heating", "write"),
    }
    detail = await run_tool(tools, "mcp_list_tools", ctx, server="home", tool="set_heating")
    assert detail["arguments_schema"]["required"] == ["room", "on"]
    result = await run_tool(
        tools, "mcp_call", ctx, server="home", tool="get_temperature", arguments={"room": "k"}
    )
    assert result == {"text": "21.5 C in k"}
    with pytest.raises(ToolError, match="unknown MCP tool"):
        await run_tool(tools, "mcp_call", ctx, server="home", tool="slow", arguments={})
    with pytest.raises(ToolError, match="mcp_list_tools server=home tool=set_heating"):
        await run_tool(tools, "mcp_call", ctx, server="home", tool="set_heating", arguments={})
    # writes through the generic tool still go to the action layer
    actions = FakeActions()
    done = await run_tool(
        tools,
        "mcp_call",
        ctx_for(manager, actions),
        server="home",
        tool="set_heating",
        arguments={"room": "a", "on": True},
    )
    assert done["status"] == "pending" and len(actions.requests) == 1


async def test_threshold_zero_always_uses_meta_tools_and_big_one_never(
    manager_for: Callable[..., McpManager],
) -> None:
    manager = manager_for(mcp_settings={"max_inline_tool_tokens": 0})
    await manager.prepare()
    assert [t.name for t in build_mcp_tools(manager)] == ["mcp_list_tools", "mcp_call"]
    manager = manager_for(mcp_settings={"max_inline_tool_tokens": 100000})
    await manager.prepare()
    assert len(build_mcp_tools(manager)) == 2


def test_kind_detection_needs_the_read_only_hint() -> None:
    from ai_server_monitor.mcp_client import ToolInfo

    cfg = McpServerSettings(
        name="a", url="http://x", tool_overrides=[McpToolOverride(name="t", kind="read")]
    )
    assert kind_of(cfg, ToolInfo("t", "", {}, read_only=None)) == "read"  # user override
    assert kind_of(cfg, ToolInfo("u", "", {}, read_only=None)) == "write"  # not stated: write
    assert kind_of(cfg, ToolInfo("u", "", {}, read_only=False)) == "write"
    assert kind_of(cfg, ToolInfo("u", "", {}, read_only=True)) == "read"


def test_long_or_odd_tool_names_get_safe_unique_chat_names() -> None:
    from ai_server_monitor.mcp_client.tools import _chat_name

    taken: set[str] = set()
    a = _chat_name("home", "x" * 100, taken)
    b = _chat_name("home", "x" * 100 + "y", taken)
    c = _chat_name("home", "light.turn on!", taken)
    assert len(a) <= 64 and len(b) <= 64 and a != b
    assert c == "home__light_turn_on_"


# --------------------------------------------------------------------------- secrets


async def test_token_is_sent_but_never_leaves(caplog: pytest.LogCaptureFixture) -> None:
    server = FakeMcp("streamable_http", token=TOKEN)
    await server.start()
    caplog.set_level(logging.DEBUG)
    manager = McpManager(
        McpSettings(
            servers=[
                McpServerSettings(
                    name="home",
                    url=server.url,
                    token=TOKEN,
                    headers=[{"name": "X-Api-Key", "value": "hdr-secret-value"}],
                    tool_filter=["leak"],
                    timeout="10s",
                )
            ]
        )
    )
    try:
        outcome = await manager.call("home", "leak", {})
        assert not outcome.is_error
        sent = server.headers[-1]
        assert sent["authorization"] == f"Bearer {TOKEN}"
        assert sent["x-api-key"] == "hdr-secret-value"
        # the server put the token (and an unrelated secret) in its answer: the token is gone
        assert TOKEN not in outcome.text and "[redacted]" in outcome.text
        assert SECRET_IN_RESULT in outcome.text  # (not ours to know; the chat scrubber's job)
        # not in status, settings JSON, logs
        dumped = json.dumps(manager.status()) + json.dumps(manager.settings.model_dump(mode="json"))
        assert TOKEN not in dumped and "hdr-secret-value" not in dumped
        client_logs = " | ".join(
            r.getMessage()
            for r in caplog.records
            if r.name.split(".")[0] in ("ai_server_monitor", "httpx", "httpcore", "anyio")
            or r.name.startswith("mcp.client")
        )
        assert client_logs  # (the filter is not hiding everything)
        assert TOKEN not in client_logs and "hdr-secret-value" not in client_logs
    finally:
        await manager.close()
        await server.stop()


def test_settings_mask_secrets_and_validate() -> None:
    cfg = McpServerSettings(
        name="home",
        url="https://ha.example/mcp",
        token=TOKEN,
        headers=[{"name": "X-Key", "value": "abcdefgh"}],
    )
    public = cfg.model_dump(mode="json")
    assert TOKEN not in json.dumps(public) and "abcdefgh" not in json.dumps(public)
    revealed = cfg.model_dump(mode="json", context={"reveal_secrets": True})
    assert revealed["token"] == TOKEN
    assert cfg.request_headers()["Authorization"] == f"Bearer {TOKEN}"
    assert cfg.tool_filter == [] and cfg.default_policy == "approve" and cfg.transport == "auto"
    with pytest.raises(ValidationError):
        McpServerSettings(name="has space", url="http://x")
    with pytest.raises(ValidationError):
        McpServerSettings(name="under_score", url="http://x")
    with pytest.raises(ValidationError):
        McpServerSettings(name="a", url="stdio:///usr/bin/thing")
    with pytest.raises(ValidationError, match="duplicate server names"):
        McpSettings(servers=[{"name": "a", "url": "http://x"}, {"name": "A", "url": "http://y"}])
    with pytest.raises(ValidationError, match="duplicate tool overrides"):
        McpServerSettings(
            name="a", url="http://x", tool_overrides=[{"name": "t"}, {"name": "t", "kind": "read"}]
        )
    assert McpServerSettings(
        name="a", url="http://x", tool_filter=["b", "b", " c "]
    ).tool_filter == [
        "b",
        "c",
    ]


async def test_pick_tools_skips_servers_that_are_off(
    manager_for: Callable[..., McpManager],
) -> None:
    manager = manager_for(enabled=False)
    await manager.prepare()
    assert pick_tools(manager) == [] and manager.unavailable() == []
