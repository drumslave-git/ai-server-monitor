"""MCP OAuth against a fake authorization server: discovery, registration (dynamic and by the
app's client document), the authorize redirect, the callback, the stored token, refresh on
expiry (also after a restart), and disconnect; plus every failure the owner can meet."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from ai_server_monitor.config import Settings
from ai_server_monitor.context import AppContext
from ai_server_monitor.mcp_client import McpServerSettings, check_mcp_server
from ai_server_monitor.mcp_client import oauth as oauth_module
from ai_server_monitor.mcp_client.oauth import McpOAuth
from ai_server_monitor.mcp_client.oauth_api import router

from .oauth_fake import TOKEN, FakeOAuthMcp, authorize, path_of, query_of
from .test_chat import FakeLLM
from .test_mcp_chat import LLM_URL, start_app

SECURE = {"x-forwarded-proto": "https", "x-forwarded-host": "asm.example.test"}
"""What a TLS-terminating reverse proxy in front of the app adds."""


def oauth_settings(url: str, **server: Any) -> Settings:
    return Settings.model_validate(
        {
            "llm": {"base_url": LLM_URL, "model": "fake", "max_retries": 0},
            "mcp": {
                "servers": [
                    {
                        "name": "home",
                        "url": url,
                        "transport": "streamable_http",
                        "timeout": "5s",
                        "auth": "oauth",
                        "tool_filter": ["get_temperature"],
                        **server,
                    }
                ]
            },
        }
    )


@pytest.fixture
async def fake() -> AsyncIterator[FakeOAuthMcp]:
    server = FakeOAuthMcp()
    await server.start()
    yield server
    await server.stop()


async def run_app(tmp_path: Path, fake: FakeOAuthMcp, **server: Any) -> AsyncIterator[AppContext]:
    async for app in start_app(tmp_path, FakeLLM(), oauth_settings(fake.url, **server)):
        yield app


def client(app: AppContext) -> httpx.AsyncClient:
    api = FastAPI()
    api.include_router(router)
    api.state.ctx = app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t")


async def sign_in(
    http: httpx.AsyncClient, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Connect, consent at the server, come back to the callback: the callback's answer."""
    started = await http.post("/api/mcp/oauth/home/connect", headers=headers)
    assert started.status_code == 200, started.text
    back = await authorize(started.json()["authorize_url"])
    return await http.get(path_of(back))


async def states(http: httpx.AsyncClient) -> dict[str, Any]:
    [state] = (await http.get("/api/mcp/oauth")).json()
    assert state["name"] == "home"
    return state


def cfg_of(app: AppContext) -> McpServerSettings:
    cfg = app.settings.mcp.server("home")
    assert cfg is not None
    return cfg


async def test_the_whole_login_with_dynamic_registration(
    tmp_path: Path, fake: FakeOAuthMcp
) -> None:
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            assert await states(http) == {"name": "home", "connected": False, "error": None}

            answer = await sign_in(http)
            assert answer.status_code == 303
            assert answer.headers["location"] == "/#/settings/mcp/home"
            assert await states(http) == {"name": "home", "connected": True, "error": None}

        # the app registered itself, with its own callback, and went through PKCE
        [registration] = fake.registered
        assert registration["redirect_uris"] == ["http://t/oauth/mcp/callback"]
        assert registration["token_endpoint_auth_method"] == "none"
        [asked] = fake.authorized
        assert asked["client_id"] == "client-1" and asked["code_challenge_method"] == "S256"
        assert asked["redirect_uri"] == "http://t/oauth/mcp/callback"
        assert asked["resource"] == fake.url
        assert [p["grant_type"] for p in fake.token_posts] == ["authorization_code"]

        # from now on the app's own calls carry the token (Test button, chat tools, jobs)
        result = await check_mcp_server(cfg_of(app), app.oauth)
        assert result.ok, result.message
        answer = await app.mcp.call("home", "get_temperature", {"room": "hall"}, "test")
        assert "21.5 C in hall" in str(answer)
        assert fake.mcp_bearers[-1] in fake.access


async def test_the_login_is_in_the_database_and_nowhere_else(
    tmp_path: Path, fake: FakeOAuthMcp
) -> None:
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            await sign_in(http)
        record = await app.db.kv_get("mcp_oauth:home")
        assert record["server_url"] == fake.url
        assert record["tokens"]["access_token"] in fake.access
        assert record["client_info"]["client_id"] == "client-1"
        assert record["metadata"]["token_endpoint"] == f"{fake.base}/oauth2/token"


async def test_the_apps_client_document_is_the_client_id_where_the_server_asks_for_it(
    tmp_path: Path,
) -> None:
    fake = FakeOAuthMcp(cimd=True)
    await fake.start()
    try:
        async for app in run_app(tmp_path, fake):
            async with client(app) as http:
                answer = await sign_in(http, headers=SECURE)
                assert answer.status_code == 303
                assert await states(http) == {"name": "home", "connected": True, "error": None}

                # no registration: the client ID is the address of the document
                url = "https://asm.example.test/oauth/mcp/client.json"
                assert fake.registered == []
                [asked] = fake.authorized
                assert asked["client_id"] == url
                assert asked["redirect_uri"] == "https://asm.example.test/oauth/mcp/callback"
                assert fake.token_posts[0]["client_id"] == url

                # the document the server would fetch says the same
                doc = (await http.get("/oauth/mcp/client.json", headers=SECURE)).json()
                assert doc["client_id"] == url
                assert doc["redirect_uris"] == [asked["redirect_uri"]]
                assert doc["token_endpoint_auth_method"] == "none"
                assert "authorization_code" in doc["grant_types"]
    finally:
        await fake.stop()


async def test_without_https_the_server_gets_a_registration_instead(
    tmp_path: Path,
) -> None:
    fake = FakeOAuthMcp(cimd=True)
    await fake.start()
    try:
        async for app in run_app(tmp_path, fake):
            async with client(app) as http:
                await sign_in(http)  # plain http: a client document cannot be the client ID
                assert len(fake.registered) == 1
    finally:
        await fake.stop()


async def test_the_token_is_refreshed_when_it_expires(tmp_path: Path) -> None:
    fake = FakeOAuthMcp(lifetime=1)
    await fake.start()
    try:
        async for app in run_app(tmp_path, fake):
            async with client(app) as http:
                await sign_in(http)
            first = (await app.db.kv_get("mcp_oauth:home"))["tokens"]["access_token"]
            await asyncio.sleep(1.2)
            assert (await check_mcp_server(cfg_of(app), app.oauth)).ok

            grants = [p["grant_type"] for p in fake.token_posts]
            assert grants == ["authorization_code", "refresh_token"]
            second = (await app.db.kv_get("mcp_oauth:home"))["tokens"]["access_token"]
            assert second != first and fake.mcp_bearers[-1] == second
    finally:
        await fake.stop()


async def test_a_restarted_app_refreshes_at_the_saved_token_endpoint(tmp_path: Path) -> None:
    fake = FakeOAuthMcp(lifetime=1)
    await fake.start()
    try:
        async for app in run_app(tmp_path, fake):
            async with client(app) as http:
                await sign_in(http)
            await asyncio.sleep(1.2)
            again = McpOAuth(app.db)  # a new process: nothing in memory but the database
            assert (await check_mcp_server(cfg_of(app), again)).ok
            assert fake.token_posts[-1]["grant_type"] == "refresh_token"
    finally:
        await fake.stop()


async def test_a_login_the_server_withdrew_says_so_and_asks_for_a_new_one(
    tmp_path: Path, fake: FakeOAuthMcp
) -> None:
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            await sign_in(http)
            fake.revoke_all()
            result = await check_mcp_server(cfg_of(app), app.oauth)
            assert not result.ok
            assert "no longer valid" in result.message or "expired" in result.message
            assert "press Connect again" in result.message
            state = await states(http)
            assert state["connected"] is True  # (still saved: the owner decides)
            assert "Connect again" in state["error"]

            await sign_in(http)  # a new login clears it
            assert await states(http) == {"name": "home", "connected": True, "error": None}
            assert (await check_mcp_server(cfg_of(app), app.oauth)).ok


async def test_a_server_that_was_never_connected_says_to_press_connect(
    tmp_path: Path, fake: FakeOAuthMcp
) -> None:
    async for app in run_app(tmp_path, fake):
        result = await check_mcp_server(cfg_of(app), app.oauth)
        assert not result.ok
        assert "press Connect" in result.message
        assert fake.registered == [] and fake.mcp_bearers == []  # nothing was sent


async def test_disconnect_forgets_the_login(tmp_path: Path, fake: FakeOAuthMcp) -> None:
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            await sign_in(http)
            assert (await http.delete("/api/mcp/oauth/home")).status_code == 204
            assert await states(http) == {"name": "home", "connected": False, "error": None}
        assert await app.db.kv_get("mcp_oauth:home") is None
        assert "press Connect" in (await check_mcp_server(cfg_of(app), app.oauth)).message


async def test_a_login_is_not_offered_to_another_address(
    tmp_path: Path, fake: FakeOAuthMcp
) -> None:
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            await sign_in(http)
        other = cfg_of(app).model_copy(update={"url": "http://127.0.0.1:1/other"})
        assert (await app.oauth.states([other]))["home"]["connected"] is False


async def test_a_save_forgets_the_login_of_a_server_that_is_gone_or_uses_a_token(
    tmp_path: Path, fake: FakeOAuthMcp
) -> None:
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            await sign_in(http)
        await app.oauth.prune(app.settings.mcp.servers)
        assert await app.db.kv_get("mcp_oauth:home") is not None  # still an OAuth server

        token_server = cfg_of(app).model_copy(update={"auth": "token"})
        await app.oauth.prune([token_server])
        assert await app.db.kv_get("mcp_oauth:home") is None

        async with client(app) as http:
            await sign_in(http)
        await app.oauth.prune([])  # deleted
        assert await app.db.kv_get("mcp_oauth:home") is None


async def test_denied_consent_is_told_in_words(tmp_path: Path, fake: FakeOAuthMcp) -> None:
    fake.deny = True
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            answer = await sign_in(http)
            assert answer.status_code == 303  # back to Settings, where the error stands
            state = await states(http)
            assert state["connected"] is False
            assert state["error"] == "The login at the server was denied."


async def test_a_server_that_publishes_no_oauth_information(tmp_path: Path) -> None:
    fake = FakeOAuthMcp(metadata=False)
    await fake.start()
    try:
        async for app in run_app(tmp_path, fake):
            async with client(app) as http:
                answer = await http.post("/api/mcp/oauth/home/connect")
                assert answer.status_code == 400
                assert "does not publish OAuth sign-in information" in answer.json()["detail"]
                assert "set Auth to token" in (await states(http))["error"]
    finally:
        await fake.stop()


async def test_a_server_that_needs_no_login(tmp_path: Path) -> None:
    fake = FakeOAuthMcp(login=False)
    await fake.start()
    try:
        async for app in run_app(tmp_path, fake):
            async with client(app) as http:
                answer = await http.post("/api/mcp/oauth/home/connect")
                assert answer.status_code == 400
                assert "without a login" in answer.json()["detail"]
    finally:
        await fake.stop()


async def test_a_login_nobody_finishes_times_out(
    tmp_path: Path, fake: FakeOAuthMcp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth_module, "SIGN_IN_WAIT", 0.2)
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            assert (await http.post("/api/mcp/oauth/home/connect")).status_code == 200
            await asyncio.sleep(0.5)
            assert "Nobody finished the login" in (await states(http))["error"]


async def test_a_second_connect_replaces_the_first(tmp_path: Path, fake: FakeOAuthMcp) -> None:
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            first = (await http.post("/api/mcp/oauth/home/connect")).json()["authorize_url"]
            back = await authorize(first)
            await sign_in(http)
            stale = await http.get(path_of(back))  # the first login's code arrives too late
            assert stale.status_code == 400 and "not started here" in stale.text
            assert (await states(http))["connected"] is True


async def test_the_callback_refuses_what_it_did_not_start(
    tmp_path: Path, fake: FakeOAuthMcp
) -> None:
    async for app in run_app(tmp_path, fake):
        async with client(app) as http:
            answer = await http.get("/oauth/mcp/callback?state=guess&code=x")
            assert answer.status_code == 400
            assert "Press Connect" in answer.text and "<script" not in answer.text


async def test_connect_needs_a_saved_oauth_server(tmp_path: Path, fake: FakeOAuthMcp) -> None:
    async for app in run_app(tmp_path, fake, auth="token"):
        async with client(app) as http:
            assert (await http.post("/api/mcp/oauth/nope/connect")).status_code == 404
            answer = await http.post("/api/mcp/oauth/home/connect")
            assert answer.status_code == 400 and "set Auth to oauth" in answer.json()["detail"]
            assert (await http.get("/api/mcp/oauth")).json() == []


async def test_a_token_server_is_untouched_by_oauth(tmp_path: Path) -> None:
    fake = FakeOAuthMcp(login=False)
    fake.token = TOKEN  # (FakeMcp's own bearer check)
    await fake.start()
    try:
        async for app in run_app(tmp_path, fake, auth="token", token=TOKEN):
            assert app.oauth.auth_for(cfg_of(app)) is None
            assert (await check_mcp_server(cfg_of(app), app.oauth)).ok
    finally:
        await fake.stop()


def test_an_origin_comes_from_the_proxy_headers_when_there_are_some() -> None:
    from ai_server_monitor.mcp_client.oauth_api import public_origin

    def origin(headers: dict[str, str]) -> str:
        scope = {
            "type": "http",
            "scheme": "http",
            "server": ("127.0.0.1", 8080),
            "path": "/",
            "query_string": b"",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
        from starlette.requests import Request

        return public_origin(Request(scope))

    assert origin({"host": "nas.lan:8080"}) == "http://nas.lan:8080"
    assert origin({"host": "app:8080", **SECURE}) == "https://asm.example.test"
    assert (
        origin({"host": "app", "x-forwarded-proto": "https, http", "x-forwarded-host": "a.b, c.d"})
        == "https://a.b"
    )


def test_the_client_document_path_is_the_one_the_docs_name() -> None:
    assert query_of("http://x/y?a=1&b=2") == {"a": "1", "b": "2"}
    assert oauth_module.CLIENT_PATH == "/oauth/mcp/client.json"
