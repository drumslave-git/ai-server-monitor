from __future__ import annotations

import json
from typing import Any

import httpx

from ai_server_monitor.config import LLMSettings
from ai_server_monitor.llm import LLMClient, ToolCall

TOOLS = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]


def sse(*chunks: dict[str, Any]) -> bytes:
    blank = "\n\n"
    return (
        "".join(f"data: {json.dumps(c)}{blank}" for c in chunks) + f"data: [DONE]{blank}"
    ).encode()


def delta(**d: Any) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": d}]}


async def run(*chunks: dict[str, Any], tools: bool = True) -> tuple[str, list[ToolCall], dict]:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, content=sse(*chunks), headers={"content-type": "text/event-stream"}
        )

    client = LLMClient(
        LLMSettings(base_url="http://llm.test/v1", model="m", max_retries=0),
        transport=httpx.MockTransport(handler),
    )
    text, calls = "", []
    async for chunk in client.stream(
        [{"role": "user", "content": "hi"}], tools=TOOLS if tools else None
    ):
        if chunk.done:
            calls = chunk.tool_calls
        else:
            text += chunk.text
    await client.aclose()
    return text, calls, seen


async def test_fragmented_arguments_and_parallel_calls_by_index() -> None:
    text, calls, sent = await run(
        delta(content="Let me "),
        delta(content="look."),
        delta(
            tool_calls=[
                {
                    "index": 0,
                    "id": "a",
                    "type": "function",
                    "function": {"name": "t", "arguments": ""},
                }
            ]
        ),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '{"collec'}}]),
        delta(
            tool_calls=[{"index": 1, "id": "b", "function": {"name": "u", "arguments": '{"x":'}}]
        ),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'tor": "system"}'}}]),
        delta(tool_calls=[{"index": 1, "function": {"arguments": " 1}"}}]),
    )
    assert text == "Let me look."
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("a", "t", '{"collector": "system"}'),
        ("b", "u", '{"x": 1}'),
    ]
    assert calls[0].parsed_arguments() == {"collector": "system"}
    assert sent["stream"] is True and sent["tools"] == TOOLS


async def test_whole_call_in_one_chunk_and_arguments_as_an_object() -> None:
    _, calls, _ = await run(
        delta(
            tool_calls=[{"index": 0, "id": "w", "function": {"name": "t", "arguments": '{"a": 1}'}}]
        ),
        delta(
            tool_calls=[{"index": 1, "id": "o", "function": {"name": "u", "arguments": {"b": 2}}}]
        ),
    )
    assert [c.parsed_arguments() for c in calls] == [{"a": 1}, {"b": 2}]


async def test_servers_without_index_and_servers_that_send_a_message_instead_of_deltas() -> None:
    _, calls, _ = await run(
        delta(tool_calls=[{"id": "x", "function": {"name": "t", "arguments": '{"q":'}}]),
        delta(tool_calls=[{"function": {"arguments": ' "a"}'}}]),
        delta(tool_calls=[{"id": "y", "function": {"name": "u", "arguments": "{}"}}]),
    )
    assert [(c.id, c.name, c.arguments) for c in calls] == [
        ("x", "t", '{"q": "a"}'),
        ("y", "u", "{}"),
    ]

    _, whole, _ = await run(
        {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "m",
                                "type": "function",
                                "function": {"name": "t", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ]
        }
    )
    assert [(c.id, c.name) for c in whole] == [("m", "t")]


async def test_calls_without_a_name_are_dropped_and_missing_ids_are_made_up() -> None:
    _, calls, _ = await run(
        delta(tool_calls=[{"index": 0, "function": {"arguments": "{}"}}]),
        delta(tool_calls=[{"index": 1, "function": {"name": "t", "arguments": "{}"}}]),
    )
    assert [(c.id, c.name) for c in calls] == [("call_1", "t")]


async def test_stream_without_tools_is_unchanged() -> None:
    text, calls, sent = await run(delta(content="plain "), delta(content="answer"), tools=False)
    assert text == "plain answer" and calls == []
    assert "tools" not in sent and "tool_choice" not in sent
