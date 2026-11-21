"""MCP tool domains: listings, sorting by the model, the owner's edits, the page's API (with
the fake MCP server's 46 tools)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_server_monitor.config import Bootstrap
from ai_server_monitor.context import AppContext
from ai_server_monitor.mcp_client.api import router, sort_tested
from ai_server_monitor.mcp_client.discovery import check_mcp_server
from ai_server_monitor.mcp_client.domains import DomainError, DomainStore, ListedTool

from .mcp_fake import FakeMcp
from .test_chat import FakeLLM, final
from .test_mcp_chat import settings_for, start_app


def sorter(*answers: list[dict[str, Any]], seen: list[str] | None = None) -> FunctionModel:
    """Answers with the given domain lists, one per model request."""
    queue = list(answers)

    def step(messages: list[Any], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(str(messages[-1].parts[-1].content))
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"response": queue.pop(0)})]
        )

    return FunctionModel(step)


def listed(*names: str) -> list[ListedTool]:
    return [ListedTool(n, f"does {n}", n.startswith("get")) for n in names]


async def domain_id(store: DomainStore, name: str) -> int:
    return next(d["id"] for d in await store.domains() if d["name"] == name)


async def test_no_domain_exists_before_tools_do(db: Any) -> None:
    assert await DomainStore(db).domains() == []


async def test_a_listing_is_recorded_and_unsorted_tools_returned(db: Any) -> None:
    store = DomainStore(db)
    assert await store.record_listing("arr", listed("get_queue", "add_series")) == [
        "add_series",
        "get_queue",
    ]
    await store.record_listing("arr", listed("get_queue"))  # add_series is gone on the server
    assert [t["tool"] for t in await store.tools()] == ["get_queue"]
    await store.record_failure("arr", "connection refused")
    assert (await store.listings())["arr"]["error"] == "connection refused"
    assert [t["tool"] for t in await store.tools()] == ["get_queue"]  # kept while down
    await store.forget_servers([])
    assert await store.tools() == [] and await store.listings() == {}


async def test_sorting_places_every_tool_and_retries_until_it_does(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue", "add_series", "get_containers"))
    prompts: list[str] = []
    model = sorter(
        [{"name": "sonarr", "description": "TV series", "tools": ["arr_get_queue"]}],
        [
            {
                "name": "sonarr",
                "description": "TV series",
                "tools": ["arr_get_queue", "arr_add_series"],
            },
            {"name": "docker", "description": "Containers", "tools": ["arr_get_containers"]},
        ],
        seen=prompts,
    )
    assert await store.sort_tools(model) == {"sorted": 3, "error": None}
    assert "Existing domains:\n(none yet)" in prompts[0]
    assert "not placed: arr_add_series, arr_get_containers" in prompts[1]
    tools = {t["tool"]: t for t in await store.tools()}
    assert tools["get_queue"]["domain"] == "sonarr"
    assert tools["get_containers"]["domain"] == "docker"
    sonarr = next(d for d in await store.domains() if d["name"] == "sonarr")
    assert sonarr["set_by"] == "model" and sonarr["tools"] == 2 and sonarr["used_by"] == []


async def test_bad_names_and_new_domains_without_description_are_sent_back(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue"))
    prompts: list[str] = []
    model = sorter(
        [{"name": "Sonarr App", "tools": ["arr_get_queue"]}],
        [{"name": "sonarr", "description": "TV", "tools": ["arr_get_queue"]}],
        seen=prompts,
    )
    await store.sort_tools(model)
    assert "bad domain name 'Sonarr App'" in prompts[1] and "needs a description" in prompts[1]


async def test_resort_keeps_the_owners_placements_and_names_unless_asked(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue", "add_series"))
    await store.sort_tools(
        sorter(
            [{"name": "sonarr", "description": "TV", "tools": ["arr_get_queue", "arr_add_series"]}]
        )
    )
    mine = await store.new_domain("library", "My shows")
    await store.move_tool("arr", "add_series", mine)  # the owner's own placement
    seen: list[str] = []
    model = sorter(
        [{"name": "queue", "description": "Queues", "tools": ["arr_get_queue"]}], seen=seen
    )
    await store.sort_tools(model, again=True)
    tools = {t["tool"]: t for t in await store.tools()}
    assert tools["add_series"]["domain"] == "library" and tools["add_series"]["set_by"] == "owner"
    assert tools["get_queue"]["domain"] == "queue"
    assert "sonarr" not in {d["name"] for d in await store.domains()}  # model's, left empty
    assert "arr_add_series" not in seen[0].split("Tools to sort:")[1]
    both = sorter(
        [{"name": "queue", "description": "Q", "tools": ["arr_get_queue", "arr_add_series"]}]
    )
    await store.sort_tools(both, again=True, include_owner=True)
    assert {t["domain"] for t in await store.tools()} == {"queue"}
    library = next(d for d in await store.domains() if d["name"] == "library")
    assert library["description"] == "My shows" and library["tools"] == 0  # owner's: stays


async def test_a_domain_a_job_uses_survives_a_resort_and_cannot_be_deleted(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue"))
    await store.sort_tools(sorter([{"name": "a", "description": "A", "tools": ["arr_get_queue"]}]))
    a = await domain_id(store, "a")
    job = await db.execute(
        "INSERT INTO jobs (slug, name, kind, prompt, interval_s, model, created_by, created, "
        "updated) VALUES ('q', 'Sonarr queue', 'topic', 'p', 60, 'worker', 'user', 0, 0)"
    )
    await db.execute("INSERT INTO job_domains (job_id, domain_id) VALUES (?, ?)", (job, a))
    await store.sort_tools(
        sorter([{"name": "b", "description": "B", "tools": ["arr_get_queue"]}]), again=True
    )
    names = {d["name"]: d for d in await store.domains()}
    assert names["a"]["tools"] == 0
    assert names["a"]["used_by"] == [{"slug": "q", "name": "Sonarr queue"}]
    with pytest.raises(DomainError, match="used by: Sonarr queue"):
        await store.delete_domain(a)


async def test_a_failed_sorting_is_kept_on_the_tools(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue"))

    def broken(messages: list[Any], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model down")

    result = await store.sort_tools(FunctionModel(broken))
    assert result["sorted"] == 0 and "model down" in result["error"]
    [tool] = await store.tools()
    assert tool["domain_id"] is None and "model down" in tool["sort_error"]
    assert "not configured" in (await store.sort_tools(None))["error"]


async def test_the_owners_edits(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue"))
    new = await store.new_domain("sonarr", "TV series")
    await store.move_tool("arr", "get_queue", new)
    await store.edit_domain(new, "tv", "Shows")
    [renamed] = await store.domains()
    assert renamed["name"] == "tv" and renamed["set_by"] == "owner"
    other = await store.new_domain("films", "")
    with pytest.raises(DomainError, match="already a domain"):
        await store.edit_domain(other, "tv", "x")
    with pytest.raises(DomainError, match="lowercase"):
        await store.new_domain("Bad Name", "x")
    with pytest.raises(DomainError, match="no tool"):
        await store.move_tool("arr", "nope", new)
    await store.delete_domain(new)  # its tool loses the domain and is sorted at the next listing
    [tool] = await store.tools()
    assert tool["domain_id"] is None
    assert [d["name"] for d in await store.domains()] == ["films"]


# ---------------------------------------------------------------------- the API, 46 tools


def sort_reply(body: dict[str, Any]) -> dict[str, Any]:
    """The main model's sorting: climate tools to 'climate', everything else to 'misc'."""
    text = body["messages"][-1]["content"]
    names = re.findall(r"^- (\S+?):", text.split("Tools to sort:")[1], re.M)
    climate = [n for n in names if "temperature" in n or "heating" in n]
    domains = [{"name": "climate", "description": "Rooms and heating", "tools": climate}]
    rest = [n for n in names if n not in climate]
    if rest:
        domains.append({"name": "misc", "description": "Everything else", "tools": rest})
    return final(body, {"response": domains})


@pytest.fixture
async def fake46() -> AsyncIterator[FakeMcp]:
    server = FakeMcp("streamable_http", extra_tools=40)
    await server.start()
    yield server
    await server.stop()


def client(app: AppContext) -> httpx.AsyncClient:
    api = FastAPI()
    api.include_router(router)
    api.state.ctx = app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")


async def test_a_save_lists_and_sorts_and_the_page_shows_it(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = FakeLLM(sort_reply, sort_reply)  # the save's sorting, the manual re-sort
    async for app in start_app(tmp_path, llm, settings_for(fake46.url)):
        await app.mcp_settings_saved()
        await asyncio.gather(*app._listing)
        async with client(app) as http:
            page = (await http.get("/api/mcp/tools")).json()
            assert len(page["tools"]) == 46 and len(llm.requests) == 1
            assert [d["name"] for d in page["domains"]] == ["climate", "misc"]
            [server] = page["servers"]
            assert server["name"] == "home" and server["error"] is None
            heating = next(t for t in page["tools"] if t["tool"] == "set_heating")
            assert heating["domain"] == "climate" and heating["enabled"]
            assert heating["kind"] == "write" and heating["name"] == "home_set_heating"
            bulk = next(t for t in page["tools"] if t["tool"] == "bulk_01")
            assert bulk["kind"] == "read" and not bulk["enabled"]

            # several at once, one settings write
            r = await http.post(
                "/api/mcp/tools/switch",
                json={
                    "tools": [{"server": "home", "tool": f"bulk_0{i}"} for i in range(3, 6)],
                    "enabled": True,
                },
            )
            assert r.status_code == 200
            assert {"bulk_03", "bulk_04", "bulk_05"} <= set(app.settings.mcp.servers[0].tool_filter)
            r = await http.post(
                "/api/mcp/tools/switch",
                json={
                    "tools": [
                        {"server": "home", "tool": "bulk_04"},
                        {"server": "home", "tool": "get_temperature"},
                    ],
                    "enabled": False,
                },
            )
            chosen = app.settings.mcp.servers[0].tool_filter
            assert (
                "bulk_04" not in chosen and "get_temperature" not in chosen and "bulk_03" in chosen
            )
            assert sum(t["enabled"] for t in r.json()["tools"]) == len(chosen)
            r = await http.post(
                "/api/mcp/tools/switch",
                json={"tools": [{"server": "x", "tool": "y"}], "enabled": True},
            )
            assert r.status_code == 404
            # on/off writes the server's tool_filter (the Settings table's value)
            r = await http.patch("/api/mcp/tools/home/bulk_01", json={"enabled": True})
            assert r.status_code == 200 and r.json()["enabled"]
            assert "bulk_01" in app.settings.mcp.servers[0].tool_filter
            # move, rename, delete
            climate = next(d["id"] for d in page["domains"] if d["name"] == "climate")
            r = await http.patch("/api/mcp/tools/home/bulk_01", json={"domain_id": climate})
            assert r.json()["domain"] == "climate" and r.json()["set_by"] == "owner"
            r = await http.put(
                f"/api/mcp/domains/{climate}", json={"name": "home", "description": "House"}
            )
            assert r.json()["name"] == "home" and r.json()["tools"] == 3
            r = await http.post("/api/mcp/domains", json={"name": "Bad Name"})
            assert r.status_code == 409 and "lowercase" in r.json()["detail"]

            # a manual re-sort keeps the owner's placement and name
            r = await http.post("/api/mcp/sort", json={})
            assert r.json() == {"sorted": 45, "error": None, "unreachable": {}}
            tools = {t["tool"]: t for t in (await http.get("/api/mcp/tools")).json()["tools"]}
            assert tools["bulk_01"]["domain"] == "home"
            assert tools["set_heating"]["domain"] == "climate"  # the model's again


async def test_the_settings_test_sorts_and_a_dead_server_is_shown(
    tmp_path: Path, fake46: FakeMcp
) -> None:
    llm = FakeLLM(sort_reply)
    async for app in start_app(tmp_path, llm, settings_for(fake46.url)):
        cfg = app.settings.mcp.servers[0]
        checked = await check_mcp_server(cfg)
        text = await sort_tested(app, {"name": "home"}, checked.details)
        assert text == "46 new tools sorted into domains: climate, misc."
        assert await sort_tested(app, {"name": "home"}, checked.details) == (
            "All its tools have a domain."
        )
        await fake46.stop()
        async with client(app) as http:
            r = await http.post("/api/mcp/sort", json={"server": "home"})
            assert r.json()["sorted"] == 0 and "home" in r.json()["unreachable"]
            page = (await http.get("/api/mcp/tools")).json()
            assert len(page["tools"]) == 46 and page["servers"][0]["error"]
            r = await http.post("/api/mcp/sort", json={"server": "nope"})
            assert r.status_code == 404
        await fake46.start()


async def test_the_page_says_sorting_while_a_sort_runs(tmp_path: Path, fake46: FakeMcp) -> None:
    """`sorting` is true from the moment a sort asks the model until it has the answer."""
    llm = FakeLLM(sort_reply)
    asked, release = asyncio.Event(), asyncio.Event()

    async def held(request: httpx.Request) -> httpx.Response:
        asked.set()
        await release.wait()
        return llm.handler(request)

    app = AppContext(
        Bootstrap(data_dir=tmp_path / "data", host_root=tmp_path / "no-host"),
        llm_transport=httpx.MockTransport(held),
        seed_settings=settings_for(fake46.url),
    )
    await app.start()
    try:
        async with client(app) as http:
            assert (await http.get("/api/mcp/tools")).json()["sorting"] is False
            sort = asyncio.create_task(http.post("/api/mcp/sort", json={}))
            await asyncio.wait_for(asked.wait(), 10)
            assert (await http.get("/api/mcp/tools")).json()["sorting"] is True
            release.set()
            assert (await sort).json()["sorted"] == 46
            assert (await http.get("/api/mcp/tools")).json()["sorting"] is False
    finally:
        release.set()
        await app.stop()
