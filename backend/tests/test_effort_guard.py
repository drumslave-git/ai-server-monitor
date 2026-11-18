"""EffortGuard: an endpoint that rejects `reasoning_effort` is asked again without it."""

from __future__ import annotations

import json
from typing import Any

import httpx

from ai_server_monitor.agent.models import EffortGuard

URL = "http://llm.test/v1/chat/completions"


def _request() -> httpx.Request:
    body = {"model": "m", "messages": [], "reasoning_effort": "low"}
    return httpx.Request("POST", URL, json=body)


def _rig(reply: httpx.Response) -> tuple[EffortGuard, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return reply if "reasoning_effort" in body else httpx.Response(200, json={"ok": True})

    return EffortGuard(httpx.MockTransport(handler)), sent


async def test_a_400_naming_the_field_is_retried_once_without_it() -> None:
    rejection = httpx.Response(400, json={"error": "Unsupported parameter: reasoning_effort"})
    guard, sent = _rig(rejection)

    first = await guard.handle_async_request(_request())
    assert first.status_code == 200 and guard.rejected
    assert ["reasoning_effort" in body for body in sent] == [True, False]

    again = await guard.handle_async_request(_request())  # later requests go without it
    assert again.status_code == 200
    assert ["reasoning_effort" in body for body in sent] == [True, False, False]


async def test_a_400_about_something_else_is_returned_as_it_is() -> None:
    guard, sent = _rig(httpx.Response(400, json={"error": "max_tokens is too large"}))

    response = await guard.handle_async_request(_request())
    assert response.status_code == 400 and not guard.rejected
    assert len(sent) == 1 and sent[0]["reasoning_effort"] == "low"

    await guard.handle_async_request(_request())  # still sent: nothing was learned
    assert len(sent) == 2 and sent[1]["reasoning_effort"] == "low"


async def test_a_request_without_the_field_is_not_retried() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"error": "reasoning_effort is bad"})

    guard = EffortGuard(httpx.MockTransport(handler))
    plain = httpx.Request("POST", URL, json={"model": "m", "messages": []})
    response = await guard.handle_async_request(plain)
    assert response.status_code == 400 and not guard.rejected and len(calls) == 1
