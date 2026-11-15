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

from ai_server_monitor.arr import qbit_auth
from ai_server_monitor.config import SECRET_MASK, Bootstrap, Settings
from ai_server_monitor.conntest import describe_error, scrub
from ai_server_monitor.main import create_app

RADARR = "http://radarr.test:7878"
KEY = "SECRET-api-key-0123456789"
PROM = "http://prom.test:9090"
QBIT = "http://qbit.test:8080"
DOCKER = "http://docker-proxy.test:2375"
TG_TOKEN = "123456:TG-secret-token-abcdefghijklmnop"
TG = f"https://api.telegram.org/bot{TG_TOKEN}"


@pytest.fixture
def client(bootstrap: Bootstrap) -> Iterator[TestClient]:
    settings = Settings.model_validate(
        {
            "arr": {
                "radarr": [{"name": "main", "url": RADARR, "api_key": KEY}],
                "qbittorrent": {"url": QBIT, "username": "admin", "password": "stored-pass-123"},
            },
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


# --------------------------------------------------------------------------- *arr

ARR_BODY = {"name": "main", "url": RADARR, "api_key": KEY}


@respx.mock
def test_arr_connected_reports_app_and_version(client: TestClient) -> None:
    route = respx.get(f"{RADARR}/api/v3/system/status").respond(
        json={"appName": "Radarr", "version": "5.14.0.9383"}
    )
    r = post(client, "radarr", ARR_BODY)
    assert r["ok"] is True and r["message"] == "Connected: Radarr 5.14.0.9383."
    assert r["details"]["version"] == "5.14.0.9383" and r["details"]["app"] == "Radarr"
    assert route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
def test_arr_masked_key_uses_the_stored_one(client: TestClient) -> None:
    route = respx.get(f"{RADARR}/api/v3/system/status").respond(
        json={"appName": "Radarr", "version": "5"}
    )
    r = post(client, "radarr", {**ARR_BODY, "api_key": SECRET_MASK})
    assert r["ok"] is True
    assert route.calls.last.request.headers["x-api-key"] == KEY
    # a renamed item is still matched by its position in the list
    r = post(client, "radarr", {**ARR_BODY, "name": "renamed", "api_key": SECRET_MASK}, index=0)
    assert r["ok"] is True and route.calls.last.request.headers["x-api-key"] == KEY


@respx.mock
def test_arr_prowlarr_uses_api_v1(client: TestClient) -> None:
    respx.get("http://prowlarr.test:9696/api/v1/system/status").respond(
        json={"appName": "Prowlarr", "version": "1.28.0"}
    )
    body = {"name": "prowlarr", "url": "http://prowlarr.test:9696", "api_key": KEY}
    assert post(client, "prowlarr", body)["message"] == "Connected: Prowlarr 1.28.0."


@respx.mock
@pytest.mark.parametrize("app", ["Radarr", "Sonarr", "Whisparr"])
def test_arr_wrong_application_is_reported(client: TestClient, app: str) -> None:
    kind = "sonarr" if app == "Radarr" else "radarr"
    respx.get(f"{RADARR}/api/v3/system/status").respond(json={"appName": app, "version": "1.2"})
    r = post(client, kind, ARR_BODY)
    assert r["ok"] is False
    assert f"this URL is {app} 1.2, not {kind.capitalize()}" in r["message"]


@respx.mock
def test_arr_wrong_key(client: TestClient) -> None:
    respx.get(f"{RADARR}/api/v3/system/status").respond(401, json={"error": "Unauthorized"})
    r = post(client, "radarr", ARR_BODY)
    assert r["ok"] is False and "Radarr itself rejected the API key" in r["message"]
    assert "HTTP 401" in r["message"] and "Unauthorized" in r["message"]
    assert KEY not in r["message"]


@respx.mock
def test_arr_not_json_and_redirect_and_404(client: TestClient) -> None:
    route = respx.get(f"{RADARR}/api/v3/system/status")
    route.respond(200, text="<html>login</html>")
    assert "not JSON" in post(client, "radarr", ARR_BODY)["message"]
    route.respond(302, headers={"location": "https://radarr.test/login?next=%2F&apikey=zzz"})
    message = post(client, "radarr", ARR_BODY)["message"]
    assert "redirected to https://radarr.test/login" in message and "apikey" not in message
    route.respond(404)
    assert "not Radarr" in post(client, "radarr", ARR_BODY)["message"]


@respx.mock
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (dns_failure(), "DNS name not found: radarr.test"),
        (refused(), "Connection refused by radarr.test:7878"),
        (httpx.ReadTimeout("slow"), "Timed out after 10 s"),
        (tls_failure(), "TLS certificate of radarr.test is not trusted (self-signed"),
    ],
)
def test_arr_unreachable_messages(client: TestClient, error: Exception, expected: str) -> None:
    respx.get(f"{RADARR}/api/v3/system/status").mock(side_effect=error)
    r = post(client, "radarr", ARR_BODY)
    assert r["ok"] is False and expected in r["message"]
    assert KEY not in r["message"]


def test_arr_bad_input_is_a_readable_message_not_an_error(client: TestClient) -> None:
    r = post(client, "sonarr", {"name": "s", "url": "radarr:7878", "api_key": ""})
    assert r["ok"] is False and r["message"].startswith("Fix this first:")
    assert "http://" in r["message"]


# --------------------------------------------------------------------------- qBittorrent


@respx.mock
def test_qbit_connected(client: TestClient) -> None:
    login = respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200, text="Ok.", headers={"set-cookie": "SID=abc; path=/"}
    )
    respx.get(f"{QBIT}/api/v2/app/version").respond(200, text="v5.0.2")
    body = {"url": QBIT, "username": "admin", "password": SECRET_MASK}
    r = post(client, "qbittorrent", body)
    assert r["ok"] is True and r["message"] == "Connected: qBittorrent v5.0.2, login OK."
    assert b"stored-pass-123" in login.calls.last.request.content  # the stored password


@respx.mock
def test_qbit_wrong_credentials_ban_and_unreachable(client: TestClient) -> None:
    body = {"url": QBIT, "username": "admin", "password": "nope"}
    login = respx.post(f"{QBIT}/api/v2/auth/login")
    login.respond(200, text="Fails.")
    r = post(client, "qbittorrent", body)
    assert r["ok"] is False and "answered 'Fails.'" in r["message"]
    assert 'HTTP 200, body "Fails."' in r["message"] and "Referer: " in r["message"]
    assert "paused until" in r["message"]
    assert "nope" not in r["message"]
    # retried with the same credentials: not sent again (that would only add a failed attempt)
    calls = login.call_count
    again = post(client, "qbittorrent", body)
    assert "Not tried again" in again["message"] and login.call_count == calls
    # other credentials = new key = a fresh attempt
    qbit_auth.reset()
    login.respond(403, text="Your IP address has been banned")
    r = post(client, "qbittorrent", body)
    assert "banned" in r["message"] and "extends the ban" in r["message"]
    assert r["details"]["http_status"] == 403 and r["details"]["kind"] == "banned"
    qbit_auth.reset()
    login.mock(side_effect=dns_failure())
    assert "DNS name not found: qbit.test" in post(client, "qbittorrent", body)["message"]
    login.respond(404)
    assert "does not look like qBittorrent" in post(client, "qbittorrent", body)["message"]


@respx.mock
def test_qbit_52_login_answers_204_with_a_cookie(client: TestClient) -> None:
    body = {"url": QBIT, "username": "admin", "password": "good-password"}
    respx.post(f"{QBIT}/api/v2/auth/login").respond(204, headers={"set-cookie": "SID=abc; path=/"})
    respx.get(f"{QBIT}/api/v2/app/version").respond(200, text="v5.2.0")
    r = post(client, "qbittorrent", body)
    assert r["ok"] is True and "qBittorrent v5.2.0" in r["message"]
    assert r["details"]["version"] == "v5.2.0"


@respx.mock
def test_qbit_204_without_cookie_needs_a_working_version_call(client: TestClient) -> None:
    body = {"url": QBIT, "username": "admin", "password": "good-password"}
    respx.post(f"{QBIT}/api/v2/auth/login").respond(204)
    version = respx.get(f"{QBIT}/api/v2/app/version").respond(403, text="Forbidden")
    r = post(client, "qbittorrent", body)
    assert r["ok"] is False and "does not look like qBittorrent" in r["message"]
    assert "HTTP 204" in r["message"] and "HTTP 403" in r["message"]
    version.respond(200, text="v5.2.0")
    assert post(client, "qbittorrent", body)["ok"] is True  # (e.g. authentication bypass)


@respx.mock
def test_qbit_401_is_host_validation_not_wrong_password(client: TestClient) -> None:
    body = {"url": QBIT, "username": "admin", "password": "good-password"}
    login = respx.post(f"{QBIT}/api/v2/auth/login").respond(401, text="Unauthorized")
    r = post(client, "qbittorrent", body)
    assert r["ok"] is False
    assert "Host-header validation" in r["message"] and "Server domains" in r["message"]
    assert "http://<qbittorrent container>:8080" in r["message"]
    assert 'HTTP 401, body "Unauthorized"' in r["message"]
    sent = login.calls.last.request.headers
    assert sent["origin"] == QBIT
    assert sent["referer"] == QBIT
    assert r["details"]["headers_sent"]["Host"] == "qbit.test:8080"
    assert "good-password" not in str(r)


@respx.mock
def test_qbit_html_page_in_front(client: TestClient) -> None:
    body = {"url": QBIT, "username": "admin", "password": "pw-123456"}
    respx.post(f"{QBIT}/api/v2/auth/login").respond(
        200,
        text="<html><body>Sign in to continue</body></html>",
        headers={"content-type": "text/html"},
    )
    r = post(client, "qbittorrent", body)
    assert "web page, not qBittorrent's API" in r["message"]
    assert "Sign in to continue" in r["message"] and "paused" not in r["message"]


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
