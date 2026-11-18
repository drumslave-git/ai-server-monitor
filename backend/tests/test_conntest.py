"""Settings → Test buttons: every connection kind against mocked HTTP.

Nothing here touches a real service: respx answers every request.
"""

from __future__ import annotations

import socket
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
TG_TOKEN = "123456:TG-secret-token-abcdefghijklmnop"
TG = f"https://api.telegram.org/bot{TG_TOKEN}"


@pytest.fixture
def client(bootstrap: Bootstrap) -> Iterator[TestClient]:
    settings = Settings.model_validate(
        {
            "outputs": {"telegram": {"token": TG_TOKEN, "allowed_chat_ids": [111, 222]}},
        }
    )
    with TestClient(create_app(bootstrap, seed_settings=settings)) as c:
        yield c


def post(client: TestClient, kind: str, body: dict[str, Any], **params: Any) -> dict[str, Any]:
    response = client.post(f"/api/settings/test/{kind}", json=body, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def dns_failure() -> httpx.ConnectError:
    exc = httpx.ConnectError("[Errno -2] Name or service not known")
    exc.__cause__ = socket.gaierror(-2, "Name or service not known")
    return exc


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
    assert client.post("/api/settings/test/docker", json={}).status_code == 404
    assert client.post("/api/settings/test/prometheus", json={}).status_code == 404


def test_secrets_are_scrubbed_from_text() -> None:
    assert scrub(f"bad key {KEY} here", [KEY]) == "bad key *** here"
    assert scrub("a wrong guess", ["wrong"]) == "a wrong guess"  # too short to mangle


def test_describe_error_never_includes_the_url() -> None:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getMe"
    for exc in (httpx.ConnectError(f"boom {url}"), httpx.ReadTimeout(url), dns_failure()):
        assert TG_TOKEN not in describe_error(exc, url)
