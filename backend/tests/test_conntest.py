"""Settings → Test buttons: every connection kind against mocked HTTP.

Nothing here touches a real service: respx answers every request.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from ai_server_monitor.config import SECRET_MASK, Bootstrap, Settings
from ai_server_monitor.conntest import describe_error, scrub
from ai_server_monitor.main import create_app

KEY = "SECRET-api-key-0123456789"
PROM = "http://prom.test:9090"
DOCKER = "http://docker-proxy.test:2375"
TG_TOKEN = "123456:TG-secret-token-abcdefghijklmnop"
TG = f"https://api.telegram.org/bot{TG_TOKEN}"


@pytest.fixture
def client(bootstrap: Bootstrap) -> Iterator[TestClient]:
    settings = Settings.model_validate(
        {
            "collectors": {"prometheus": {"url": PROM, "token": "stored-bearer-token"}},
            "outputs": {"telegram": {"token": TG_TOKEN, "allowed_chat_ids": [111, 222]}},
        }
    )
    with TestClient(create_app(bootstrap, collector_registry={}, seed_settings=settings)) as c:
        yield c


def post(client: TestClient, kind: str, body: dict[str, Any], **params: Any) -> dict[str, Any]:
    response = client.post(f"/api/settings/test/{kind}", json=body, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def dns_failure() -> httpx.ConnectError:
    exc = httpx.ConnectError("[Errno -2] Name or service not known")
    exc.__cause__ = socket.gaierror(-2, "Name or service not known")
    return exc


def refused() -> httpx.ConnectError:
    exc = httpx.ConnectError("[Errno 111] Connection refused")
    exc.__cause__ = ConnectionRefusedError(111, "Connection refused")
    return exc


def tls_failure() -> httpx.ConnectError:
    inner = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")
    inner.verify_message = "self-signed certificate"
    exc = httpx.ConnectError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate "
        "(_ssl.c:1006)"
    )
    exc.__cause__ = inner
    return exc


# --------------------------------------------------------------------------- Prometheus


@respx.mock
def test_prometheus_connected_with_version_and_query(client: TestClient) -> None:
    info = respx.get(f"{PROM}/api/v1/status/buildinfo").respond(
        json={"status": "success", "data": {"version": "2.53.0"}}
    )
    respx.get(f"{PROM}/api/v1/query").respond(
        json={"status": "success", "data": {"resultType": "vector", "result": []}}
    )
    body = {"url": PROM, "token": SECRET_MASK, "username": "", "password": ""}
    r = post(client, "prometheus", body)
    assert r["ok"] is True and r["message"] == "Connected: Prometheus 2.53.0, a test query worked."
    assert info.calls.last.request.headers["authorization"] == "Bearer stored-bearer-token"


@respx.mock
def test_prometheus_without_buildinfo_still_works_and_basic_auth(client: TestClient) -> None:
    respx.get(f"{PROM}/api/v1/status/buildinfo").respond(404)
    q = respx.get(f"{PROM}/api/v1/query").respond(
        json={"status": "success", "data": {"resultType": "vector", "result": []}}
    )
    body = {"url": PROM, "token": "", "username": "u", "password": "p"}
    r = post(client, "prometheus", body)
    assert r["ok"] is True and r["message"] == "Connected: Prometheus, a test query worked."
    assert q.calls.last.request.headers["authorization"].startswith("Basic ")


@respx.mock
def test_prometheus_failures(client: TestClient) -> None:
    body = {"url": PROM, "token": "t", "username": "", "password": ""}
    info = respx.get(f"{PROM}/api/v1/status/buildinfo")
    info.respond(401)
    assert "rejected the credentials (HTTP 401)" in post(client, "prometheus", body)["message"]
    info.mock(side_effect=refused())
    assert "Connection refused by prom.test:9090" in post(client, "prometheus", body)["message"]
    info.respond(200, text="<html>not prometheus</html>")
    assert "does not look like Prometheus" in post(client, "prometheus", body)["message"]
    r = post(client, "prometheus", {**body, "url": ""})
    assert r["ok"] is False and "Enter the Prometheus URL" in r["message"]


# --------------------------------------------------------------------------- Docker


@respx.mock
def test_docker_connected_and_failures(client: TestClient) -> None:
    route = respx.get(f"{DOCKER}/version")
    route.respond(json={"Version": "27.3.1", "ApiVersion": "1.47", "Os": "linux"})
    r = post(client, "docker", {"url": "tcp://docker-proxy.test:2375"})
    assert r["ok"] is True and r["message"] == "Connected: Docker 27.3.1 (API 1.47)."
    route.respond(403, text="Forbidden")
    assert "socket proxy does not allow" in post(client, "docker", {"url": DOCKER})["message"]
    route.respond(200, json={"hello": "world"})
    assert "not a Docker API" in post(client, "docker", {"url": DOCKER})["message"]
    route.mock(side_effect=dns_failure())
    assert (
        "DNS name not found: docker-proxy.test"
        in post(client, "docker", {"url": DOCKER})["message"]
    )
    assert "Unsupported URL" in post(client, "docker", {"url": "ftp://x"})["message"]


# --------------------------------------------------------------------------- Telegram


def tg_ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


@respx.mock
def test_telegram_get_me(client: TestClient) -> None:
    respx.post(f"{TG}/getMe").mock(return_value=tg_ok({"id": 7, "username": "my_asm_bot"}))
    send = respx.post(f"{TG}/sendMessage")
    r = post(client, "telegram", {"token": SECRET_MASK, "allowed_chat_ids": [111], "api_url": ""})
    assert r["ok"] is True and "bot @my_asm_bot is valid" in r["message"]
    assert "1 allowed chat(s)" in r["message"]
    assert not send.called  # a plain test sends nothing
    r = post(client, "telegram", {"token": SECRET_MASK, "allowed_chat_ids": []})
    assert "send /id to the bot" in r["message"]


@respx.mock
def test_telegram_bad_token_and_unreachable(client: TestClient) -> None:
    route = respx.post(f"{TG}/getMe")
    route.respond(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
    r = post(client, "telegram", {"token": SECRET_MASK})
    assert r["ok"] is False and "rejected the bot token" in r["message"]
    assert TG_TOKEN not in r["message"]
    route.mock(side_effect=dns_failure())
    r = post(client, "telegram", {"token": SECRET_MASK})
    assert "DNS name not found: api.telegram.org" in r["message"] and TG_TOKEN not in r["message"]
    r = post(client, "telegram", {"token": ""})
    assert "Enter the bot token" in r["message"]


@respx.mock
def test_telegram_send_reports_each_chat(client: TestClient) -> None:
    respx.post(f"{TG}/getMe").mock(return_value=tg_ok({"id": 7, "username": "my_asm_bot"}))

    def answer(request: httpx.Request) -> httpx.Response:
        import json

        chat = json.loads(request.content)["chat_id"]
        if chat == 222:
            return httpx.Response(
                400,
                json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
            )
        return tg_ok({"message_id": 1})

    send = respx.post(f"{TG}/sendMessage").mock(side_effect=answer)
    r = post(
        client, "telegram", {"token": SECRET_MASK, "allowed_chat_ids": [111, 222]}, mode="send"
    )
    assert r["ok"] is False
    assert r["details"]["chats"] == {
        "111": "sent",
        "222": "chat not found: the user must send /start to the bot first "
        "(for a group: add the bot to it)",
    }
    assert "Sent to 1 of 2 chat(s). chat 222: chat not found" in r["message"]
    assert send.call_count == 2

    r = post(client, "telegram", {"token": SECRET_MASK, "allowed_chat_ids": [111]}, mode="send")
    assert r["ok"] is True and r["message"] == "Test message sent to 1 chat(s) as @my_asm_bot."


@respx.mock
def test_telegram_send_without_chats_is_not_ok(client: TestClient) -> None:
    respx.post(f"{TG}/getMe").mock(return_value=tg_ok({"id": 7, "username": "b"}))
    r = post(client, "telegram", {"token": SECRET_MASK, "allowed_chat_ids": []}, mode="send")
    assert r["ok"] is False and "no allowed chat ids" in r["message"]


# --------------------------------------------------------------------------- misc


def test_unknown_kind_is_404(client: TestClient) -> None:
    assert client.post("/api/settings/test/nope", json={}).status_code == 404


def test_secrets_are_scrubbed_from_text() -> None:
    assert scrub(f"bad key {KEY} here", [KEY]) == "bad key *** here"
    assert scrub("a wrong guess", ["wrong"]) == "a wrong guess"  # too short to mangle


def test_describe_error_never_includes_the_url() -> None:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getMe"
    for exc in (httpx.ConnectError(f"boom {url}"), httpx.ReadTimeout(url), dns_failure()):
        assert TG_TOKEN not in describe_error(exc, url)
