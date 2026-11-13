from __future__ import annotations

import json

import httpx
import pytest
import respx

from servermon.db import Database
from servermon.llm import LLMClient, LLMConnectionError, ToolModeResolver, probe_tool_mode

from .conftest import LLM_URL, chat_response

NATIVE_CALL = [
    {"id": "c1", "type": "function", "function": {"name": "get_magic_number", "arguments": "{}"}}
]
JSON_CALL = 'Sure.\n```json\n{"tool": "get_magic_number", "args": {}}\n```'


@respx.mock
async def test_probe_native(llm: LLMClient) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=chat_response(None, tool_calls=NATIVE_CALL)
    )
    assert await probe_tool_mode(llm) == "native"
    assert json.loads(route.calls.last.request.content)["tools"][0]["function"]["name"] == (
        "get_magic_number"
    )


@respx.mock
async def test_probe_json_when_tools_rejected(llm: LLMClient) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(
        side_effect=[httpx.Response(400, text="tools not supported"), chat_response(JSON_CALL)]
    )
    assert await probe_tool_mode(llm) == "json"


@respx.mock
async def test_probe_json_when_model_ignores_native_tools(llm: LLMClient) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(
        side_effect=[chat_response("I don't know the number."), chat_response(JSON_CALL)]
    )
    assert await probe_tool_mode(llm) == "json"


@respx.mock
async def test_probe_none(llm: LLMClient) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(return_value=chat_response("It's 42."))
    assert await probe_tool_mode(llm) == "none"


@respx.mock
async def test_probe_auth_error_propagates(llm: LLMClient) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(return_value=httpx.Response(401, text="no"))
    with pytest.raises(Exception, match="401"):
        await probe_tool_mode(llm)


@respx.mock
async def test_resolver_caches_probe_result(llm: LLMClient, db: Database) -> None:
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=chat_response(None, tool_calls=NATIVE_CALL)
    )
    first = ToolModeResolver(llm, db, "auto")
    assert await first.resolve() == "native"
    assert first.source == "probe"
    assert await first.resolve() == "native"  # no-op once resolved
    assert route.call_count == 1

    second = ToolModeResolver(llm, db, "auto")  # e.g. after restart
    assert await second.resolve() == "native"
    assert second.source == "cache"
    assert route.call_count == 1


async def test_resolver_uses_config_without_calls(llm: LLMClient, db: Database) -> None:
    resolver = ToolModeResolver(llm, db, "json")
    assert await resolver.resolve() == "json"
    assert resolver.status()["source"] == "config"


@respx.mock
async def test_resolver_stays_unresolved_when_unreachable(llm: LLMClient, db: Database) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(side_effect=httpx.ConnectError("down"))
    resolver = ToolModeResolver(llm, db, "auto")
    with pytest.raises(LLMConnectionError):
        await resolver.resolve()
    assert resolver.mode is None
