"""MCP tool domains: the built-in ones, listings, sorting by the model, the owner's edits."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ai_server_monitor.mcp_client.domains import DomainError, DomainStore, ListedTool

BUILTIN = ["docker", "journal", "resources", "storage", "systemd", "updates"]


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


async def test_builtin_domains_exist_from_the_start(db: Any) -> None:
    domains = await DomainStore(db).domains()
    assert sorted(d["key"] for d in domains) == BUILTIN
    assert all(d["set_by"] == "builtin" and d["description"] for d in domains)


async def test_a_listing_is_recorded_and_unsorted_tools_returned(db: Any) -> None:
    store = DomainStore(db)
    assert await store.record_listing("arr", listed("get_queue", "add_series")) == [
        "add_series",
        "get_queue",
    ]
    await store.record_listing("arr", listed("get_queue"))  # add_series is gone on the server
    assert [t["tool"] for t in await store.tools()] == ["get_queue"]
    await store.forget_servers([])
    assert await store.tools() == []


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
            {"name": "docker", "tools": ["arr_get_containers"]},
        ],
        seen=prompts,
    )
    assert await store.sort_tools(model) == {"sorted": 3, "error": None}
    assert "Existing domains:" in prompts[0] and "- docker: Docker containers" in prompts[0]
    assert "not placed: arr_add_series, arr_get_containers" in prompts[1]
    tools = {t["tool"]: t for t in await store.tools()}
    assert (
        tools["get_queue"]["domain"] == "sonarr" and tools["get_containers"]["domain"] == "docker"
    )
    sonarr = next(d for d in await store.domains() if d["name"] == "sonarr")
    assert sonarr["set_by"] == "model" and sonarr["key"] is None and sonarr["tools"] == 2


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


async def test_resort_keeps_the_owners_placements_unless_asked(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue", "add_series"))
    await store.sort_tools(
        sorter(
            [{"name": "sonarr", "description": "TV", "tools": ["arr_get_queue", "arr_add_series"]}]
        )
    )
    docker = next(d["id"] for d in await store.domains() if d["name"] == "docker")
    await store.move_tool("arr", "add_series", docker)  # the owner's own placement
    seen: list[str] = []
    model = sorter(
        [{"name": "queue", "description": "Queues", "tools": ["arr_get_queue"]}], seen=seen
    )
    await store.sort_tools(model, again=True)
    tools = {t["tool"]: t for t in await store.tools()}
    assert tools["add_series"]["domain"] == "docker" and tools["add_series"]["set_by"] == "owner"
    assert tools["get_queue"]["domain"] == "queue"
    assert "sonarr" not in {d["name"] for d in await store.domains()}  # left empty: removed
    assert "arr_add_series" not in seen[0].split("Tools to sort:")[1]
    both = sorter(
        [{"name": "queue", "description": "Q", "tools": ["arr_get_queue", "arr_add_series"]}]
    )
    await store.sort_tools(both, again=True, include_owner=True)
    assert {t["domain"] for t in await store.tools()} == {"queue"}
    assert "docker" in {d["name"] for d in await store.domains()}  # built-in: never removed


async def test_a_domain_a_job_uses_survives_a_resort_empty(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue"))
    await store.sort_tools(sorter([{"name": "a", "description": "A", "tools": ["arr_get_queue"]}]))
    a = next(d["id"] for d in await store.domains() if d["name"] == "a")
    await store.sort_tools(
        sorter([{"name": "b", "description": "B", "tools": ["arr_get_queue"]}]),
        again=True,
        in_use=[a],
    )
    names = {d["name"]: d["tools"] for d in await store.domains()}
    assert names["a"] == 0 and names["b"] == 1


async def test_a_failed_sorting_is_kept_on_the_tools(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue"))

    def broken(messages: list[Any], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model down")

    result = await store.sort_tools(FunctionModel(broken))
    assert result["sorted"] == 0 and "model down" in result["error"]
    [tool] = await store.tools()
    assert tool["domain_id"] is None and "model down" in tool["sort_error"]


async def test_the_owners_edits(db: Any) -> None:
    store = DomainStore(db)
    await store.record_listing("arr", listed("get_queue"))
    new = await store.new_domain("sonarr", "TV series")
    await store.move_tool("arr", "get_queue", new)
    with pytest.raises(DomainError, match="move its tools"):
        await store.delete_domain(new, [])
    with pytest.raises(DomainError, match="used by: Downloads"):
        await store.delete_domain(new, ["Downloads"])
    docker = next(d for d in await store.domains() if d["key"] == "docker")
    await store.edit_domain(docker["id"], "containers", "Containers")  # built-ins can be renamed
    renamed = next(d for d in await store.domains() if d["key"] == "docker")
    assert renamed["name"] == "containers" and renamed["set_by"] == "owner"
    with pytest.raises(DomainError, match="cannot be deleted"):
        await store.delete_domain(docker["id"], [])
    with pytest.raises(DomainError, match="already a domain"):
        await store.edit_domain(new, "containers", "x")
    with pytest.raises(DomainError, match="lowercase"):
        await store.new_domain("Bad Name", "x")
    other = await store.new_domain("empty", "")
    await store.delete_domain(other, [])
    assert "empty" not in {d["name"] for d in await store.domains()}
