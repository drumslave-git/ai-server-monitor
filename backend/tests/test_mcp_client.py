"""The MCP client (Pydantic AI's MCPToolset) against a real local MCP server, over both
transports, driven by Pydantic AI's FunctionModel through our agent runtime."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from ai_server_monitor.agent.runtime import AgentConfig, run_agent
from ai_server_monitor.mcp_client import (
    McpServers,
    McpServerSettings,
    McpSettings,
    check_mcp_server,
    kind_of,
)

from .mcp_fake import LONG_DESCRIPTION, SECRET_IN_RESULT, FakeMcp, free_port

TRANSPORTS = ["streamable_http", "sse"]
TOKEN = "tok-9f8e7d6c5b4a"


@pytest.fixture(params=TRANSPORTS)
async def fake(request: pytest.FixtureRequest) -> AsyncIterator[FakeMcp]:
    server = FakeMcp(request.param, token=TOKEN)
    await server.start()
    yield server
    await server.stop()


def cfg_for(url: str, transport: str = "auto", **overrides: Any) -> McpServerSettings:
    values: dict[str, Any] = {
        "name": "home",
        "url": url,
        "transport": transport,
        "token": TOKEN,
        "headers": [{"name": "X-Api-Key", "value": "hdr-secret-value"}],
        "timeout": "5s",
        "tool_filter": ["get_temperature", "set_heating", "leak", "boom", "slow"],
    }
    values.update(overrides)
    return McpServerSettings.model_validate(values)


class Script:
    """A FunctionModel that makes the given calls, one per step, then answers; it records the
    tools it was offered and what each call returned."""

    def __init__(self, *calls: tuple[str, dict[str, Any]]):
        self.calls = list(calls)
        self.offered: list[list[str]] = []
        self.returns: list[str] = []
        self.instructions: list[str] = []
        self.model = FunctionModel(self._step, stream_function=self._stream)

    def _step(self, messages: list[Any], info: AgentInfo) -> ModelResponse:
        self.offered.append(sorted(t.name for t in info.function_tools))
        self.instructions.append(info.instructions or "")
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                self.returns.append(str(part.content))
        if self.calls:
            name, args = self.calls.pop(0)
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    async def _stream(self, messages: list[Any], info: AgentInfo) -> AsyncIterator[Any]:
        response = self._step(messages, info)
        async for chunk in deltas(response):
            yield chunk


async def deltas(response: ModelResponse | Any) -> AsyncIterator[Any]:
    if inspect.isawaitable(response):
        response = await response
    part = response.parts[0]
    if isinstance(part, ToolCallPart):
        yield {0: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args))}
    else:
        yield part.content


async def run(servers: McpServers, script: Script, **cfg: Any) -> list[dict[str, Any]]:
    mcp = servers.run("test", **cfg.pop("run", {}))
    config = AgentConfig(
        model=script.model,
        instructions="test",
        toolsets=mcp.toolsets,
        notes=lambda: "\n".join(mcp.unavailable()),
        tool_timeout_s=30,
        **cfg,
    )
    return [event async for event in run_agent(config, "go")]


# --------------------------------------------------------------------------- discovery


async def test_check_lists_server_and_tools_with_whole_descriptions(fake: FakeMcp) -> None:
    result = await check_mcp_server(cfg_for(fake.url))
    assert result.ok, result.message
    assert result.details["server"]["name"] == "fake-home"
    expected = "HTTP+SSE" if fake.transport == "sse" else "Streamable HTTP"
    assert result.details["transport"] == expected and expected in result.message
    rows = {r["name"]: r for r in result.details["tools"]}
    assert (
        rows["get_temperature"]["read_only"] is True and rows["get_temperature"]["kind"] == "read"
    )
    assert rows["set_heating"]["kind"] == "write" and rows["set_heating"]["selected"] is True
    assert rows["image"]["selected"] is False
    sent = fake.headers[-1]
    assert sent["authorization"] == f"Bearer {TOKEN}" and sent["x-api-key"] == "hdr-secret-value"
    assert TOKEN not in json.dumps(result.details) + result.message


async def test_check_keeps_long_descriptions_whole() -> None:
    server = FakeMcp("streamable_http", extra_tools=44)
    await server.start()
    try:
        result = await check_mcp_server(cfg_for(server.url, token="", headers=[]))
        rows = {r["name"]: r for r in result.details["tools"]}
        assert len(rows) == 6 + 44
        assert rows["bulk_03"]["description"] == LONG_DESCRIPTION  # never cut
    finally:
        await server.stop()


async def test_check_says_what_the_server_answered(fake: FakeMcp) -> None:
    """The MCP SDK reduces an error status to "Server returned an error response"; the Test
    names the status and the path tried, or why no connection was made."""
    port = free_port()
    dead = await check_mcp_server(cfg_for(f"http://127.0.0.1:{port}/mcp", timeout="5s"))
    assert not dead.ok
    assert dead.message == f"The server could not be used: connection to 127.0.0.1:{port} refused"

    wrong = await check_mcp_server(cfg_for(fake.url, token="wrong-token-000"))
    method, path = ("POST", "/mcp") if fake.transport == "streamable_http" else ("GET", "/sse")
    assert not wrong.ok and "wrong-token-000" not in wrong.message
    assert wrong.message == (
        "The server could not be used: "
        f"the server answered HTTP 401 Unauthorized to {method} {path}"
    )

    lost = "/nope" if fake.transport == "streamable_http" else "/nope/sse"
    gone = await check_mcp_server(cfg_for(fake.url.replace(path, lost)))
    assert not gone.ok
    assert gone.message.endswith(f"the server answered HTTP 404 Not Found to {method} {lost}")


# --------------------------------------------------------------------------- agent runs


async def test_chosen_tools_are_given_directly_and_a_read_runs(fake: FakeMcp) -> None:
    audit: list[dict[str, Any]] = []
    servers = McpServers(McpSettings(servers=[cfg_for(fake.url)]), audit=audit.append)
    script = Script(("home_get_temperature", {"room": "kitchen"}))
    events = await run(servers, script)
    assert script.offered[0] == [
        "home_boom",
        "home_get_temperature",
        "home_leak",
        "home_set_heating",
        "home_slow",
    ]
    assert "21.5 C in kitchen" in script.returns[0] and 'trust="untrusted"' in script.returns[0]
    assert events[-1]["type"] == "end"
    assert audit[0]["tool"] == "get_temperature" and audit[0]["ok"] and audit[0]["source"] == "test"


async def test_secrets_errors_and_timeouts_come_back_as_text(fake: FakeMcp) -> None:
    audit: list[dict[str, Any]] = []
    cfg = cfg_for(fake.url, timeout="2s")
    servers = McpServers(McpSettings(servers=[cfg]), audit=audit.append)
    script = Script(("home_leak", {}), ("home_boom", {}), ("home_slow", {"seconds": 5}))
    events = await run(servers, script)
    leak, boom, slow = script.returns
    assert TOKEN not in leak and "[redacted]" in leak and SECRET_IN_RESULT in leak
    assert "boom" in boom and "failed" in boom
    # (over SSE the cut-off call sometimes ends as "Connection closed": a race in the transport)
    assert "timed out" in slow or "failed" in slow
    assert events[-1]["type"] == "end"
    assert [a["ok"] for a in audit] == [True, False, False]
    assert TOKEN not in json.dumps(audit)


async def test_write_tool_waits_for_approval_in_the_chat(fake: FakeMcp) -> None:
    servers = McpServers(McpSettings(servers=[cfg_for(fake.url)]))
    script = Script(("home_set_heating", {"room": "hall", "on": True}))
    filed: list[tuple[str, dict[str, Any]]] = []

    async def on_approval(tool: Any, args: dict[str, Any]) -> str:
        filed.append((tool.name, args))
        return "Approval #1 is waiting."

    from ai_server_monitor.agent.runtime import AgentTool

    def approval_tool(name: str) -> AgentTool:
        return AgentTool(name=name, description="", parameters={}, handler=lambda a: None)  # type: ignore[arg-type,return-value]

    await run(servers, script, on_approval=on_approval, approval_tool=approval_tool)
    assert filed == [("home_set_heating", {"room": "hall", "on": True})]
    assert not fake.calls  # nothing sent: it went to the approval service
    assert "Approval #1 is waiting." in script.returns[0]


async def test_kind_override_and_unchosen_tools(fake: FakeMcp) -> None:
    cfg = cfg_for(
        fake.url,
        tool_filter=["set_heating"],
        tool_overrides=[{"name": "set_heating", "kind": "read"}],
    )
    servers = McpServers(McpSettings(servers=[cfg]))
    script = Script(("home_set_heating", {"room": "a", "on": True}))
    await run(servers, script)  # no on_approval: a write could not run here
    assert script.offered[0] == ["home_set_heating"]
    assert ("set_heating", {"room": "a", "on": True}) in fake.calls
    assert kind_of(cfg, "set_heating", None) == "read"
    assert kind_of(cfg_for(fake.url), "set_heating", None) == "write"
    assert kind_of(cfg_for(fake.url), "x", {"readOnlyHint": True}) == "read"


async def test_job_writes_go_to_the_write_call(fake: FakeMcp) -> None:
    servers = McpServers(McpSettings(servers=[cfg_for(fake.url)]))
    seen: list[tuple[str, str, dict[str, Any]]] = []

    async def write_call(cfg: McpServerSettings, tool: str, args: dict[str, Any]) -> str:
        seen.append((cfg.name, tool, args))
        return "Requested: waits for approval #7."

    script = Script(("home_set_heating", {"room": "den", "on": True}))
    await run(servers, script, run={"write_call": write_call})
    assert seen == [("home", "set_heating", {"room": "den", "on": True})]
    assert not fake.calls and "approval #7" in script.returns[0]


async def test_a_dead_server_gives_no_tools_and_says_so() -> None:
    servers = McpServers(McpSettings(servers=[cfg_for("http://127.0.0.1:1/mcp", timeout="5s")]))
    script = Script()
    events = await run(servers, script)
    assert events[-1]["type"] == "end"
    assert script.offered[0] == []
    assert "MCP server 'home' is unavailable this run" in script.instructions[0]
    assert "connection to 127.0.0.1:1 refused" in script.instructions[0]


async def test_a_refused_token_is_named_in_the_run(fake: FakeMcp) -> None:
    servers = McpServers(McpSettings(servers=[cfg_for(fake.url, token="wrong-token-000")]))
    script = Script()
    await run(servers, script)
    to = "POST /mcp" if fake.transport == "streamable_http" else "GET /sse"
    # failed at once: no "(after 0.0 s)" behind the reason
    assert f"answered HTTP 401 Unauthorized to {to})." in script.instructions[0]
    assert "wrong-token-000" not in script.instructions[0]


async def test_a_server_dying_during_the_run_does_not_end_it() -> None:
    server = FakeMcp("streamable_http")
    await server.start()
    servers = McpServers(McpSettings(servers=[cfg_for(server.url, token="", headers=[])]))
    script = Script(
        ("home_get_temperature", {"room": "a"}), ("home_get_temperature", {"room": "b"})
    )
    original = script._step

    async def stop_after_first(messages: list[Any], info: AgentInfo) -> ModelResponse:
        if len(script.offered) == 1:
            await server.stop()
        return original(messages, info)

    async def stream(messages: list[Any], info: AgentInfo) -> AsyncIterator[Any]:
        async for chunk in deltas(stop_after_first(messages, info)):
            yield chunk

    script.model = FunctionModel(stop_after_first, stream_function=stream)
    events = await run(servers, script)
    assert events[-1]["type"] == "end"
    assert "21.5 C in a" in script.returns[0] and "failed" in script.returns[1]


async def test_many_tools_are_all_offered() -> None:
    server = FakeMcp("streamable_http", extra_tools=44)
    await server.start()
    try:
        names = [f"bulk_{i:02d}" for i in range(44)]
        servers = McpServers(
            McpSettings(servers=[cfg_for(server.url, token="", headers=[], tool_filter=names)])
        )
        script = Script()
        await run(servers, script)
        assert len(script.offered[0]) == 44 and all(
            n.startswith("home_bulk_") for n in script.offered[0]
        )
    finally:
        await server.stop()


async def test_an_approved_action_is_called_outside_a_run(fake: FakeMcp) -> None:
    servers = McpServers(McpSettings(servers=[cfg_for(fake.url)]))
    assert await servers.call("home", "set_heating", {"room": "x", "on": False}, "action") == (
        "heating in x is now off"
    )
    assert "[redacted]" in await servers.call("home", "leak", {}, "action")
    with pytest.raises(RuntimeError):
        await servers.call("home", "boom", {}, "action")
    with pytest.raises(RuntimeError, match="no MCP server"):
        await servers.call("nope", "x", {}, "action")


async def test_secrets_stay_out_of_the_logs(caplog: pytest.LogCaptureFixture) -> None:
    server = FakeMcp("streamable_http", token=TOKEN)
    await server.start()
    caplog.set_level(logging.DEBUG)
    try:
        servers = McpServers(McpSettings(servers=[cfg_for(server.url)]))
        await run(servers, Script(("home_leak", {})))
        logs = " | ".join(r.getMessage() for r in caplog.records)
        assert logs and TOKEN not in logs and "hdr-secret-value" not in logs
    finally:
        await server.stop()


# --------------------------------------------------------------------------- settings


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


def test_policy_off_from_older_settings_is_mapped() -> None:
    cfg = McpServerSettings.model_validate(
        {
            "name": "a",
            "url": "http://x",
            "default_policy": "off",
            "tool_filter": ["keep", "drop"],
            "tool_overrides": [{"name": "drop", "policy": "off"}, {"name": "keep", "kind": "read"}],
        }
    )
    assert cfg.default_policy == "suggest"
    assert cfg.tool_filter == ["keep"]
    assert cfg.override("drop") is not None and cfg.override("drop").policy == "inherit"  # type: ignore[union-attr]
