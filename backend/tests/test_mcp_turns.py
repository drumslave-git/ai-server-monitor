"""MCP domains in the chat (fake MCP server with 46 tools): new tools are sorted before the
turn, the worker model picks the request's domains, the turn gets exactly their tools, the
others are loadable with `load_capability`, loaded domains stay for the session, writes still
wait for approval, and without enabled MCP tools nothing of this happens."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from ai_server_monitor.context import AppContext

from .mcp_fake import FakeMcp
from .test_chat import call, say
from .test_mcp_chat import McpLLM, ask, settings_for, start_app

CHOSEN = ["get_temperature", "set_heating", "bulk_01", "bulk_02", "boom"]


def domain_of(tool: str) -> str:
    if tool in ("get_temperature", "set_heating"):
        return "climate"
    return "bulk" if tool.startswith("bulk_") else "misc"


def mcp_llm(*script: Any, picks: list[str] | None) -> McpLLM:
    llm = McpLLM(*script)
    llm.sort_into = domain_of
    llm.picks = picks
    return llm


def offered(request: dict[str, Any]) -> set[str]:
    return {t["function"]["name"] for t in request.get("tools", [])}


def mcp_tools(request: dict[str, Any]) -> set[str]:
    return {n for n in offered(request) if n.startswith("home_")}


@pytest.fixture
async def fake46() -> AsyncIterator[FakeMcp]:
    server = FakeMcp("streamable_http", extra_tools=40)
    await server.start()
    yield server
    await server.stop()


def settings(url: str, chosen: list[str] = CHOSEN) -> Any:
    s = settings_for(url)
    s.mcp.servers[0].tool_filter = list(chosen)
    return s


async def turn_meta(app: AppContext, session_id: str) -> dict[str, Any]:
    row = await app.db.fetchone(
        "SELECT meta FROM chat_messages WHERE session_id = ? AND role = 'assistant' "
        "AND content IS NOT NULL ORDER BY id DESC LIMIT 1",
        (session_id,),
    )
    assert row is not None
    return json.loads(row["meta"])


async def test_new_tools_are_sorted_then_the_picked_domain_is_offered(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = mcp_llm(say("It is warm."), picks=["climate"])
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        events = await ask(app, "how warm is the kitchen?")
        assert events[-1]["type"] == "done"
        # all 46 listed tools were sorted (one call of the main model) before the turn went on
        assert len(llm.sort_requests) == 1 and len(await app.domains.tools()) == 46
        assert len(llm.classify_requests) == 1
        classify = llm.classify_requests[0]["messages"][-1]["content"]
        assert "- bulk:" in classify and "- climate:" in classify and "- misc:" in classify
        first = llm.requests[0]
        assert mcp_tools(first) == {"home_get_temperature", "home_set_heating"}
        assert "load_capability" in offered(first) and "bash" in offered(first)
        system = json.dumps([m for m in first["messages"] if m["role"] == "system"])
        assert "domain-bulk" in system and "domain-misc" in system and "(2 tools)" in system
        assert "domain-climate" not in system  # given directly, not in the catalog
        meta = await turn_meta(app, events[-1]["session_id"])
        assert meta["mcp_domains"] == {
            "picked": ["climate"],
            "offered": ["climate"],
            "loaded": [],
        }


async def test_a_miss_is_loaded_in_the_turn_and_stays_for_the_session(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = mcp_llm(
        call("load_capability", {"id": "domain-climate"}),
        call("home_get_temperature", {"room": "kitchen"}, "c2"),
        say("21.5 C."),
        say("Still 21.5 C."),
        picks=[],
    )
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        events = await ask(app, "how warm is the kitchen?")
        assert mcp_tools(llm.requests[0]) == set()  # nothing picked
        assert "home_get_temperature" in mcp_tools(llm.requests[1])  # after the load
        assert ("get_temperature", {"room": "kitchen"}) in fake46.calls
        session_id = events[-1]["session_id"]
        meta = await turn_meta(app, session_id)
        assert meta["mcp_domains"]["picked"] == [] and meta["mcp_domains"]["loaded"] == ["climate"]
        # next turn of the same session: climate is given directly from the first request
        events = [e async for e in app.chat.run("and now?", session_id)]
        assert events[-1]["text"] == "Still 21.5 C."
        assert mcp_tools(llm.requests[-1]) == {"home_get_temperature", "home_set_heating"}
        assert (await turn_meta(app, session_id))["mcp_domains"]["offered"] == ["climate"]


async def test_a_write_tool_of_a_loaded_domain_still_waits_for_approval(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = mcp_llm(
        call("load_capability", {"id": "domain-climate"}),
        call("home_set_heating", {"room": "bedroom", "on": False}, "c2"),
        say("Done."),
        picks=[],
    )
    async for app in start_app(tmp_path, llm, settings(fake46.url)):
        events = await ask(app, "turn the bedroom heating off")
        assert events[-1]["text"].startswith("Waiting for approval #")
        assert not any(name == "set_heating" for name, _ in fake46.calls)
        row = await app.db.fetchone("SELECT state FROM chat_pending_runs")
        assert json.loads(row["state"])["domains"] == ["climate"]
        [record] = await app.actions.pending()
        done = await app.actions.decide(record["id"], "approve")
        assert done["status"] == "executed", done
        session_id = events[-1]["session_id"]
        for _ in range(200):
            await asyncio.gather(*list(app.chat._background))
            history = await app.chat.store.history(session_id, 10)
            if history[-1]["content"] == "Done.":
                break
            await asyncio.sleep(0.02)
        assert history[-1]["content"] == "Done."
        # the resumed run had the same domain without classifying again
        assert len(llm.classify_requests) == 1
        assert "home_set_heating" in mcp_tools(llm.requests[-1])


async def test_without_enabled_mcp_tools_there_is_no_extra_call(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = mcp_llm(say("Hello."), picks=None)
    async for app in start_app(tmp_path, llm, settings(fake46.url, chosen=[])):
        events = await ask(app, "hi")
        assert events[-1]["text"] == "Hello."
        assert llm.sort_requests == [] and llm.classify_requests == []
        assert await app.domains.listings() == {}  # not even connected
        assert "mcp_domains" not in await turn_meta(app, events[-1]["session_id"])


async def test_a_dead_server_is_named_in_the_turn_log(tmp_path: Path) -> None:
    llm = mcp_llm(say("No sensors."), picks=None)
    async for app in start_app(tmp_path, llm, settings("http://127.0.0.1:1/mcp")):
        events = await ask(app, "how warm is it?")
        assert llm.classify_requests == []  # no domain has tools: nothing to pick
        meta = await turn_meta(app, events[-1]["session_id"])
        assert "home" in meta["mcp_domains"]["unreachable"]
        assert "unavailable this run" in json.dumps(llm.requests[0]["messages"])
