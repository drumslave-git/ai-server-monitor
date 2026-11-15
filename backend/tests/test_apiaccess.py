from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.actions.service import ActionService
from ai_server_monitor.apiaccess import (
    ACTION_NAME,
    ApiAccessError,
    ApiCallActionArgs,
    ApiGateway,
    ApiRequest,
    action_check,
    action_target,
    api_tool_specs,
    index,
)
from ai_server_monitor.apiaccess import gateway as gateway_module
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.db import Database

from .apifakes import (
    BIG,
    DOCKER_ENV_SECRET,
    DOWNLOAD_CLIENT_KEY,
    PROM_TOKEN,
    FakeServices,
    api_settings,
)
from .arrfakes import API_KEY, QBIT_PASSWORD
from .test_chat import FakeLLM, ask, call, make_app, say


@pytest.fixture
def world() -> FakeServices:
    return FakeServices()


@pytest.fixture
async def gateway(world: FakeServices) -> AsyncIterator[ApiGateway]:
    gw = ApiGateway(api_settings(), transport=world.transport())
    yield gw
    await gw.close()


def get(service: str, path: str, **kw: Any) -> ApiRequest:
    return ApiRequest(service=service, path=path, **kw)


def text_of(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------- GET shaping


async def test_get_large_library_is_limited_and_trimmed(gateway: ApiGateway) -> None:
    result = await gateway.execute(get("radarr", "/api/v3/movie"))
    assert result["status"] == 200
    assert result["total"] == BIG and result["showing"] < BIG
    assert "narrow" in result["note"]
    first = result["data"][0]
    # Pictures and bulk are gone, long text is cut, empty values removed.
    assert "images" not in first and "alternateTitles" not in first and "remotePoster" not in first
    assert len(first["overview"]) <= 200
    assert "folder" not in first and "tags" not in first
    assert len(text_of(result)) < 7000


async def test_select_and_limit_on_the_whole_library(gateway: ApiGateway) -> None:
    result = await gateway.execute(
        get(
            "radarr",
            "/api/v3/movie",
            select="[?monitored && !hasFile].{title: title, year: year}",
            limit=10,
        )
    )
    assert len(result["data"]) == 10
    assert set(result["data"][0]) == {"title", "year"}
    assert result["showing"] == 10 and result["total"] > 10
    assert "10" in result["note"]


async def test_select_can_use_a_field_dropped_by_default(gateway: ApiGateway) -> None:
    result = await gateway.execute(
        get("radarr", "/api/v3/movie", select="[0].{n: length(alternateTitles)}")
    )
    assert result["data"] == {"n": 25}


async def test_select_lower_for_case_insensitive_match(gateway: ApiGateway) -> None:
    result = await gateway.execute(
        get("radarr", "/api/v3/movie", select="[?contains(lower(title), 'movie 0042')].id")
    )
    assert result["data"] == [42]


async def test_invalid_select_explains_itself(gateway: ApiGateway) -> None:
    with pytest.raises(ApiAccessError, match=r"JMESPath.*Example"):
        await gateway.execute(get("radarr", "/api/v3/movie", select="records[.title"))


async def test_select_matching_nothing_shows_the_shape(gateway: ApiGateway) -> None:
    result = await gateway.execute(get("radarr", "/api/v3/wanted/missing", select="items[].title"))
    assert result["data"] is None
    assert "keys: page, pageSize, totalRecords, records" in result["note"]


async def test_result_stays_under_the_size_cap(gateway: ApiGateway) -> None:
    result = await gateway.execute(
        get(
            "radarr",
            "/api/v3/movie",
            select="[].{id: id, overview: overview, path: path}",
            limit=500,
        ),
        max_chars=3000,
    )
    assert len(text_of(result["data"])) <= 3000
    assert result["showing"] < 500 and "total" in result


# --------------------------------------------------------------------------- paging


async def test_missing_endpoint_page_note_and_next_page(
    gateway: ApiGateway, world: FakeServices
) -> None:
    result = await gateway.execute(
        get(
            "radarr",
            "/api/v3/wanted/missing?page=1&pageSize=50",
            select="records[].{title: title, year: year}",
        )
    )
    assert len(result["data"]) == 50
    assert result["paged"]["totalRecords"] == 250
    assert "250 records" in result["note"] and "page=2" in result["note"]
    page2 = await gateway.execute(
        get(
            "radarr",
            "/api/v3/wanted/missing",
            query={"page": 2, "pageSize": 50},
            select="records[0].title",
        )
    )
    assert page2["data"] == "Missing 051"
    # The query in the path really reached the server.
    assert ("pageSize", "50") in world.calls("radarr.test")[0]["params"]


async def test_all_pages_collects_every_page(gateway: ApiGateway, world: FakeServices) -> None:
    result = await gateway.execute(
        get("radarr", "/api/v3/wanted/missing", all_pages=True, select="length(records)")
    )
    assert result["data"] == 250
    assert len(world.calls("radarr.test", "GET")) == 3  # 100 + 100 + 50


async def test_all_pages_has_a_hard_cap(
    gateway: ApiGateway, world: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "MAX_PAGES", 2)
    result = await gateway.execute(
        get("radarr", "/api/v3/wanted/missing", all_pages=True, select="length(records)")
    )
    assert result["data"] == 200
    assert "stopped after 2 pages" in result["note"]


# --------------------------------------------------------------------------- safety


@pytest.mark.parametrize(
    "path",
    [
        "http://evil.test/api/v3/health",
        "https://radarr.test:7878/api/v3/health",
        "//evil.test/x",
        "/api/v3/../../etc/passwd",
        "/api/v3/%2e%2e/system",
        "/api/v3/%252e%252e/system",
        "/api/v3/movie/..",
        "..\\secret",
        "/api/v3/health#frag",
        "/api/v3/movie/{id}",
        "/api/v3/health\n/x",
        "/api/v3/movie lookup",
        "",
    ],
)
async def test_unsafe_paths_are_rejected_before_any_request(
    gateway: ApiGateway, world: FakeServices, path: str
) -> None:
    with pytest.raises(ApiAccessError):
        await gateway.execute(get("radarr", path))
    assert world.requests == []


async def test_prefix_is_added_when_missing(gateway: ApiGateway, world: FakeServices) -> None:
    result = await gateway.execute(get("radarr", "health"))
    assert result["call"] == "GET /api/v3/health"
    result = await gateway.execute(get("prowlarr", "/indexer", select="[].name"))
    assert result["call"] == "GET /api/v1/indexer"
    assert result["data"] == ["GoodIndexer", "BadIndexer"]


async def test_credential_parameters_are_refused(gateway: ApiGateway, world: FakeServices) -> None:
    with pytest.raises(ApiAccessError, match="credentials are added automatically"):
        await gateway.execute(get("radarr", "/api/v3/health", query={"apikey": "x"}))
    with pytest.raises(ApiAccessError, match="credentials"):
        await gateway.execute(get("radarr", "/api/v3/health?api_key=x"))
    assert world.requests == []


async def test_unknown_service_lists_the_choices(gateway: ApiGateway) -> None:
    with pytest.raises(ApiAccessError, match="available: radarr, sonarr, whisparr, prowlarr"):
        await gateway.execute(get("lidarr", "/api/v1/x"))


async def test_secrets_never_reach_the_output(gateway: ApiGateway) -> None:
    dl = await gateway.execute(get("radarr", "/api/v3/downloadclient"))
    health = await gateway.execute(get("radarr", "/api/v3/health"))
    env = await gateway.execute(get("docker", "/containers/web/json"))
    selected = await gateway.execute(
        get("radarr", "/api/v3/downloadclient", select="[0].fields[].{n: name, v: value}")
    )
    blob = text_of([dl, health, env, selected])
    for secret in (
        API_KEY,
        DOWNLOAD_CLIENT_KEY,
        DOCKER_ENV_SECRET,
        "pw-in-field-value",
        QBIT_PASSWORD,
        PROM_TOKEN,
    ):
        assert secret not in blob
    assert "[redacted]" in blob
    assert "PATH=/usr/bin" in blob  # harmless env entries stay


async def test_error_text_is_short_scrubbed_and_helpful(gateway: ApiGateway) -> None:
    with pytest.raises(ApiAccessError, match=r"HTTP 404.*api_endpoints") as exc:
        await gateway.execute(get("radarr", "/api/v3/movie/999"))
    assert exc.value.status == 404
    bad = ApiGateway(api_settings(), transport=FakeServices().transport())
    bad.targets["radarr"] = type(bad.targets["radarr"])(
        "radarr", "radarr", "http://radarr.test:7878", "/api/v3", headers={"X-Api-Key": "wrong"}
    )
    with pytest.raises(ApiAccessError, match="HTTP 401: access denied"):
        await bad.execute(get("radarr", "/api/v3/health"))
    await bad.close()


async def test_read_only_services_refuse_writes(gateway: ApiGateway, world: FakeServices) -> None:
    for service, path in (("docker", "/containers/web/restart"), ("prometheus", "/api/v1/admin/x")):
        with pytest.raises(ApiAccessError, match="read-only"):
            gateway.prepare(service, "POST", path)
        with pytest.raises(ApiAccessError, match="read-only"):
            await gateway.execute(
                ApiRequest(service=service, method="DELETE", path=path), approved=True
            )
    assert world.requests == []


async def test_writes_do_not_run_without_approval(gateway: ApiGateway, world: FakeServices) -> None:
    with pytest.raises(ApiAccessError, match="needs the owner's approval"):
        await gateway.execute(ApiRequest(service="radarr", method="DELETE", path="/api/v3/movie/5"))
    assert world.requests == []


@pytest.mark.parametrize(
    ("service", "path", "write_like"),
    [
        ("prowlarr", "/api/v1/search?query=x", True),
        ("prowlarr", "/api/v1/search", True),
        ("radarr", "/api/v3/release?movieId=1", True),
        ("sonarr", "/release?episodeId=1", True),
        ("radarr", "/api/v3/movie", False),
        ("prowlarr", "/api/v1/indexerstatus", False),
        ("radarr", "/api/v3/releaseprofile", False),
        ("qbittorrent", "/torrents/info", False),
    ],
)
def test_indexer_querying_gets_are_write_like(service: str, path: str, write_like: bool) -> None:
    gw = ApiGateway(api_settings())
    assert gw.prepare(service, "GET", path).write_like is write_like


async def test_rate_limit_per_service(
    gateway: ApiGateway, world: FakeServices, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "GET_RATE_LIMIT", 3)
    for _ in range(3):
        await gateway.execute(get("radarr", "/api/v3/health"))
    with pytest.raises(ApiAccessError, match="rate limit"):
        await gateway.execute(get("radarr", "/api/v3/health"))
    await gateway.execute(get("prowlarr", "/api/v1/indexer"))  # other services are unaffected
    assert len(world.calls("radarr.test")) == 3


async def test_every_call_is_logged(world: FakeServices) -> None:
    seen: list[dict[str, Any]] = []
    gw = ApiGateway(api_settings(), transport=world.transport(), on_call=seen.append)
    await gw.execute(get("radarr", "/api/v3/health"), source="chat:s1")
    with pytest.raises(ApiAccessError):
        await gw.execute(get("radarr", "/api/v3/movie/999"))
    assert [r["status"] for r in seen] == [200, 404]
    first = seen[0]
    assert first["service"] == "radarr" and first["method"] == "GET"
    assert first["path"] == "/api/v3/health" and first["source"] == "chat:s1"
    assert first["bytes"] > 0 and first["ms"] >= 0
    assert list(gw.recent) == seen
    assert API_KEY not in text_of(seen)
    await gw.close()


# --------------------------------------------------------------------------- other services


async def test_qbittorrent_logs_in_and_trims(gateway: ApiGateway, world: FakeServices) -> None:
    result = await gateway.execute(get("qbittorrent", "/api/v2/torrents/info?filter=downloading"))
    assert world.logins == 1
    assert "magnet_uri" not in result["data"][0]
    again = await gateway.execute(get("qbittorrent", "/torrents/info", select="[].name"))
    assert again["data"] == ["Big.Movie.2024"] and world.logins == 1
    version = await gateway.execute(get("qbittorrent", "/app/version"))
    assert version["data"] == "v5.0.1"


async def test_qbittorrent_session_expiry_logs_in_again(
    gateway: ApiGateway, world: FakeServices
) -> None:
    await gateway.execute(get("qbittorrent", "/app/version"))
    world.session_valid = False
    await gateway.execute(get("qbittorrent", "/app/version"))
    assert world.logins == 2


async def test_docker_logs_are_demultiplexed_and_keep_the_tail(gateway: ApiGateway) -> None:
    result = await gateway.execute(
        get("docker", "/containers/web/logs", query={"stdout": True, "tail": 120}, limit=20)
    )
    assert result["data"][-1] == "line 120" and result["data"][0] == "line 101"
    assert result["total"] == 120 and "last 20" in result["note"]


async def test_docker_mounts_with_select(gateway: ApiGateway) -> None:
    result = await gateway.execute(
        get("docker", "/containers/web/json", select="Mounts[].{src: Source, dst: Destination}")
    )
    assert result["data"] == [{"src": "/srv/web", "dst": "/usr/share"}]


async def test_prometheus_uses_the_configured_token(
    gateway: ApiGateway, world: FakeServices
) -> None:
    result = await gateway.execute(
        get("prometheus", "/query", query={"query": "up"}, select="data.result[].value[1]")
    )
    assert result["call"] == "GET /api/v1/query" and result["data"] == ["1"]


# --------------------------------------------------------------------------- tools


class Ctx(SimpleNamespace):
    pass


def make_actions(db: Database, gateway: ApiGateway, **actions_cfg: Any) -> ActionService:
    settings = api_settings(**({"actions": actions_cfg} if actions_cfg else {}))
    return ActionService(db, settings, api=gateway)


@pytest.fixture
async def actions(tmp_path: Path, gateway: ApiGateway) -> AsyncIterator[ActionService]:
    db = Database(tmp_path / "t.db")
    await db.connect()
    service = make_actions(db, gateway)
    yield service
    await service.close()
    await db.close()


def ctx_for(gateway: ApiGateway, actions: ActionService) -> Ctx:
    return Ctx(
        app=SimpleNamespace(api_gateway=gateway, actions=actions), session_id="s1", source=None
    )


def tools(settings: Settings | None = None) -> dict[str, Any]:
    return {t.name: t for t in api_tool_specs(settings or api_settings())}


async def run_tool(name: str, args: dict[str, Any], ctx: Ctx) -> Any:
    tool = tools()[name]
    return await tool.handler(ctx, tool.args_model.model_validate(args))


async def test_tool_read_goes_straight_through(gateway: ApiGateway, actions: ActionService) -> None:
    result = await run_tool(
        "api_call",
        {
            "service": "sonarr",
            "path": "/api/v3/queue",
            "select": (
                "records[?trackedDownloadStatus!='ok'].{t: title, why: statusMessages[].messages[]}"
            ),
        },
        ctx_for(gateway, actions),
    )
    assert result["data"] == [
        {"t": "Show.S01E01.1080p", "why": ["No files found are eligible for import"]}
    ]
    assert "also" not in result  # Sonarr has no sibling
    movies = await run_tool(
        "api_call",
        {
            "service": "radarr",
            "path": "/api/v3/wanted/missing?pageSize=1",
            "select": "totalRecords",
        },
        ctx_for(gateway, actions),
    )
    assert "whisparr" in movies["also"] and "query it too" in movies["also"]


async def test_tool_write_becomes_a_pending_approval_and_runs_only_after_it(
    gateway: ApiGateway, actions: ActionService, world: FakeServices
) -> None:
    ctx = ctx_for(gateway, actions)
    result = await run_tool(
        "api_call",
        {
            "service": "radarr",
            "method": "POST",
            "path": "/api/v3/command",
            "body": {"name": "MoviesSearch", "movieIds": [7]},
        },
        ctx,
    )
    assert result["status"] == "pending" and "NOT been done" in result["note"]
    assert result["target"] == "radarr: POST /api/v3/command"
    assert world.mutations == [] and world.requests == []

    pending = await actions.pending()
    assert len(pending) == 1 and pending[0]["source"] == "chat:s1"
    assert pending[0]["args"]["body"] == {"name": "MoviesSearch", "movieIds": [7]}

    done = await actions.decide(result["action_id"], "approve", "ui")
    assert done["status"] == "executed"
    assert done["result"]["status"] == 201 and done["result"]["data"]["id"] == 77
    assert len(world.mutations) == 1
    sent = world.mutations[0]
    assert sent["method"] == "POST" and sent["path"] == "/api/v3/command"
    assert json.loads(sent["body"]) == {"name": "MoviesSearch", "movieIds": [7]}
    assert API_KEY not in text_of(done)


async def test_rejecting_an_api_call_sends_nothing(
    gateway: ApiGateway, actions: ActionService, world: FakeServices
) -> None:
    result = await run_tool(
        "api_call",
        {
            "service": "radarr",
            "method": "DELETE",
            "path": "/api/v3/movie/5",
            "query": {"deleteFiles": True},
        },
        ctx_for(gateway, actions),
    )
    await actions.decide(result["action_id"], "reject", "ui")
    assert world.requests == []


async def test_prowlarr_search_waits_for_approval_like_a_write(
    gateway: ApiGateway, actions: ActionService, world: FakeServices
) -> None:
    ctx = ctx_for(gateway, actions)
    for service, path in (
        ("prowlarr", "/api/v1/search?query=ubuntu"),
        ("radarr", "/api/v3/release?movieId=1"),
    ):
        result = await run_tool("api_call", {"service": service, "path": path}, ctx)
        assert result["status"] == "pending"
    assert world.requests == []
    done = await actions.decide((await actions.pending())[0]["id"], "approve")
    assert done["status"] == "executed" and done["result"]["call"] == "GET /api/v1/search"
    assert len(world.requests) == 1


async def test_tool_refuses_bad_calls_before_filing_anything(
    gateway: ApiGateway, actions: ActionService, world: FakeServices
) -> None:
    ctx = ctx_for(gateway, actions)
    for args in (
        {"service": "docker", "method": "POST", "path": "/containers/web/stop"},
        {"service": "radarr", "method": "POST", "path": "http://evil.test/x"},
        {"service": "radarr", "method": "POST", "path": "/api/v3/../x"},
    ):
        with pytest.raises(ApiAccessError):
            await run_tool("api_call", args, ctx)
    assert await actions.pending() == [] and world.requests == []


async def test_action_check_rejects_what_the_tool_would() -> None:
    settings = api_settings()
    ok = ApiCallActionArgs(service="radarr", method="POST", path="/api/v3/command")
    assert action_check(settings, ok) is None
    assert "read-only" in (
        action_check(settings, ApiCallActionArgs(service="docker", method="POST", path="/x")) or ""
    )
    assert "unknown service" in (
        action_check(settings, ApiCallActionArgs(service="nope", method="POST", path="/x")) or ""
    )
    assert action_check(
        settings, ApiCallActionArgs(service="radarr", method="POST", path="http://evil.test/x")
    )


async def test_api_call_is_registered_with_policy_approve_by_default(
    actions: ActionService,
) -> None:
    assert actions.settings.arr.actions.api_call.policy == "approve"
    definition = actions.registry[ACTION_NAME]
    assert definition.chat_tool is False  # the chat has its own tool; no second generic one
    assert action_target(ApiCallActionArgs(service="radarr", method="DELETE", path="/x")) == (
        "radarr: DELETE /x"
    )


async def test_policy_off_rejects_and_suggest_only_records(
    tmp_path: Path, gateway: ApiGateway, world: FakeServices
) -> None:
    db = Database(tmp_path / "p.db")
    await db.connect()
    args = {"service": "radarr", "method": "DELETE", "path": "/api/v3/movie/5"}
    for policy, status in (("off", "rejected"), ("suggest", "suggested")):
        service = make_actions(db, gateway, api_call={"policy": policy})
        record = await service.request(ACTION_NAME, args, "chat:s1")
        assert record["status"] == status
    assert world.requests == []
    await db.close()


async def test_dry_run_is_honoured_for_approved_calls(
    tmp_path: Path, gateway: ApiGateway, world: FakeServices
) -> None:
    db = Database(tmp_path / "d.db")
    await db.connect()
    settings = api_settings(dry_run=True)
    service = ActionService(db, settings, api=gateway)
    record = await service.request(
        ACTION_NAME, {"service": "radarr", "method": "POST", "path": "/api/v3/command"}, "chat:s1"
    )
    done = await service.decide(record["id"], "approve")
    assert done["status"] == "dry_run" and world.requests == []
    await db.close()


async def test_max_per_hour_caps_approved_calls(
    tmp_path: Path, gateway: ApiGateway, world: FakeServices
) -> None:
    db = Database(tmp_path / "m.db")
    await db.connect()
    service = make_actions(db, gateway, api_call={"policy": "approve", "max_per_hour": 1})
    args = {"service": "radarr", "method": "POST", "path": "/api/v3/command", "body": {"name": "X"}}
    first = await service.request(ACTION_NAME, args, "chat:s1")
    assert (await service.decide(first["id"], "approve"))["status"] == "executed"
    second = await service.request(ACTION_NAME, args, "chat:s1")
    assert (await service.decide(second["id"], "approve"))["status"] == "rejected"
    assert len(world.mutations) == 1
    await db.close()


async def test_failed_approved_call_is_recorded_with_the_message(
    gateway: ApiGateway, actions: ActionService
) -> None:
    result = await run_tool(
        "api_call",
        {"service": "radarr", "method": "DELETE", "path": "/api/v3/movie/999"},
        ctx_for(gateway, actions),
    )
    # (the fake answers 201 to any DELETE; make the service unknown to the executor instead)
    gateway.targets.pop("radarr")
    done = await actions.decide(result["action_id"], "approve")
    assert done["status"] == "failed" and "unknown service" in done["result"]["error"]


async def test_tool_errors_use_the_given_error_class(
    gateway: ApiGateway, actions: ActionService
) -> None:
    class MyError(Exception):
        pass

    spec = {t.name: t for t in api_tool_specs(api_settings(), error_cls=MyError)}["api_call"]
    args = spec.args_model.model_validate({"service": "radarr", "path": "/api/v3/movie/999"})
    with pytest.raises(MyError, match="HTTP 404"):
        await spec.handler(ctx_for(gateway, actions), args)


# --------------------------------------------------------------------------- tool definitions


def test_docker_is_always_offered_and_the_rest_follows_the_settings() -> None:
    # Docker counts as configured by default (socket); the others need their settings.
    assert list(ApiGateway(Settings()).services) == ["docker"]
    off = Settings.model_validate({"collectors": {"docker": {"enabled": False}}})
    assert ApiGateway(off).services == {}
    assert not any(t.enabled(off) for t in tools(off).values())
    assert all(t.enabled(api_settings()) for t in tools().values())


def test_tool_schemas_are_small_and_list_the_services() -> None:
    specs = tools()
    schema = specs["api_call"].args_model.model_json_schema()
    assert schema["required"] == ["service", "path"]
    assert schema["properties"]["service"]["enum"] == [
        "radarr",
        "sonarr",
        "whisparr",
        "prowlarr",
        "qbittorrent",
        "docker",
        "prometheus",
    ]
    definitions = [
        {
            "name": t.name,
            "description": t.description,
            "parameters": t.args_model.model_json_schema(),
        }
        for t in specs.values()
    ]
    assert len(json.dumps(definitions)) < 3500  # ~900 tokens
    with pytest.raises(ValueError):
        specs["api_call"].args_model.model_validate({"service": "lidarr", "path": "/x"})
    with pytest.raises(ValueError):
        specs["api_call"].args_model.model_validate({"service": "radarr", "path": "/x", "bogus": 1})


async def test_api_endpoints_tool(gateway: ApiGateway, actions: ActionService) -> None:
    ctx = ctx_for(gateway, actions)
    result = await run_tool("api_endpoints", {"service": "radarr", "search": "missing"}, ctx)
    assert result["recipes"][0]["call"].startswith("GET /api/v3/wanted/missing")
    assert result["recipes"][0]["select"].startswith("records[]")
    assert any("/api/v3/wanted/missing" in e for e in result["endpoints"])
    assert "whisparr" in result["also"]  # holds movies too: the model should look there as well
    assert "also" not in await run_tool("api_endpoints", {"service": "sonarr"}, ctx)
    no_search = await run_tool("api_endpoints", {"service": "docker"}, ctx)
    assert no_search["recipes"] and "endpoints" not in no_search and "groups" in no_search


# --------------------------------------------------------------------------- the index


@pytest.mark.parametrize(
    ("kind", "words", "expected"),
    [
        ("radarr", "missing movies", "GET /api/v3/wanted/missing"),
        ("whisparr", "what exact movies are we missing", "GET /api/v3/wanted/missing"),
        ("sonarr", "missing episodes", "GET /api/v3/wanted/missing?"),
        ("sonarr", "what is stuck in the queue", "GET /api/v3/queue?"),
        ("prowlarr", "which indexers failed", "GET /api/v1/indexerstatus"),
        ("qbittorrent", "what is downloading", "GET /api/v2/torrents/info?filter=downloading"),
        ("docker", "container mounts", "GET /containers/NAME/json"),
        ("prometheus", "targets down", "GET /api/v1/targets"),
    ],
)
def test_endpoint_search_finds_the_right_recipe_first(kind: str, words: str, expected: str) -> None:
    result = index.search(kind, words)
    assert result["recipes"][0]["call"].startswith(expected)


def test_index_covers_every_service_and_is_compact() -> None:
    data = index.load()
    assert set(data["services"]) == {
        "radarr",
        "sonarr",
        "whisparr",
        "prowlarr",
        "qbittorrent",
        "docker",
        "prometheus",
    }
    for kind, service in data["services"].items():
        assert service["entries"] and service["recipes"], kind
        for method, path, _tag, _summary, _params in service["entries"]:
            assert method in ("GET", "POST", "PUT", "DELETE", "PATCH")
            assert path.startswith("/")
    # Docker is GET-only in the index (the socket proxy enforces it anyway).
    assert {e[0] for e in data["services"]["docker"]["entries"]} == {"GET"}
    assert (index.DATA_FILE.stat().st_size) < 150_000


def test_search_limits_results_and_marks_writes() -> None:
    result = index.search("radarr", "movie", max_results=5)
    assert len(result["endpoints"]) == 5 and "more" in result
    writes = index.search("radarr", "command")
    assert "approve" in writes["note"]


def test_every_recipe_call_is_a_valid_request() -> None:
    gw = ApiGateway(api_settings())
    for kind, service in index.load()["services"].items():
        name = {"radarr": "radarr", "sonarr": "sonarr", "whisparr": "whisparr"}.get(kind, kind)
        for recipe in service["recipes"]:
            method, _, rest = recipe["call"].partition(" ")
            prepared = gw.prepare(
                name, method, rest.replace("NAME", "web").replace("HASH", "a" * 40)
            )
            assert prepared.path.startswith("/")
            if "select" in recipe:
                from ai_server_monitor.apiaccess.shaping import run_select

                run_select({}, recipe["select"])  # compiles


# --------------------------------------------------------------------------- the whole chat


def test_chat_reads_through_api_call_and_audits_every_call(bootstrap: Bootstrap) -> None:
    world = FakeServices()
    llm = FakeLLM(
        call("api_endpoints", {"service": "radarr", "search": "missing"}, "e"),
        call(
            "api_call",
            {
                "service": "radarr",
                "path": "/api/v3/wanted/missing?page=1&pageSize=3",
                "select": "records[].title",
            },
            "c",
        ),
        say("Missing: Missing 001, Missing 002, Missing 003."),
    )
    with TestClient(make_app(bootstrap, llm, world=world, dry_run=False)) as client:
        events = ask(client, "what movies are we missing?")
        offered = {t["function"]["name"] for t in llm.requests[0]["tools"]}
        assert {"api_call", "api_endpoints"} <= offered
        calls = [e for e in events if e["type"] == "tool_call"]
        assert [(c["name"], c["kind"]) for c in calls] == [
            ("api_endpoints", "read"),
            ("api_call", "read"),  # a GET is labelled read although the tool can write
        ]
        results = {e["name"]: e for e in events if e["type"] == "tool_result"}
        assert "Missing 001" in results["api_call"]["preview"]
        assert all(e["ok"] for e in results.values())
        audit = client.portal.call(
            client.app.state.ctx.db.kv_get,  # type: ignore[attr-defined]
            "tool_audit",
            [],
        )
        entry = next(a for a in audit if a["kind"] == "api")
        assert (entry["service"], entry["method"], entry["status"]) == ("radarr", "GET", 200)
        assert entry["path"] == "/api/v3/wanted/missing" and entry["source"].startswith("chat:")
        assert API_KEY not in text_of(audit)


def test_chat_write_ends_as_a_pending_approval_and_runs_after_it(bootstrap: Bootstrap) -> None:
    world = FakeServices()
    body = {"name": "MoviesSearch", "movieIds": [7]}
    llm = FakeLLM(
        call(
            "api_call",
            {"service": "radarr", "method": "POST", "path": "/api/v3/command", "body": body},
            "w",
        ),
        say("Asked for approval; nothing was searched yet."),
    )
    with TestClient(make_app(bootstrap, llm, world=world, dry_run=False)) as client:
        events = ask(client, "search for movie 7 now")
        write = next(e for e in events if e["type"] == "tool_call")
        assert write["name"] == "api_call" and write["kind"] == "write"
        result = next(e for e in events if e["type"] == "tool_result")
        assert json.loads(result["preview"])["status"] == "pending"
        assert world.mutations == []
        approvals = client.get("/api/approvals").json()["items"]
        assert [a["action"] for a in approvals] == ["api_call"]
        assert approvals[0]["target"] == "radarr: POST /api/v3/command"
        done = client.post(f"/api/approvals/{approvals[0]['id']}", json={"decision": "approve"})
        assert done.json()["status"] == "executed"
        assert len(world.mutations) == 1
        assert json.loads(world.mutations[0]["body"]) == body
        # the approved call is in the same audit trail
        audit = client.portal.call(
            client.app.state.ctx.db.kv_get,  # type: ignore[attr-defined]
            "tool_audit",
            [],
        )
        assert [(a["method"], a["source"]) for a in audit if a["kind"] == "api"] == [
            ("POST", "action:api_call")
        ]
