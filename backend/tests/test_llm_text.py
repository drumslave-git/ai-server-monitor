from __future__ import annotations

from servermon.llm.jsontools import json_tool_system_prompt, parse_tool_request
from servermon.llm.probe import PROBE_TOOL
from servermon.llm.text import extract_json_object, strip_reasoning


def test_strip_reasoning() -> None:
    assert strip_reasoning("<think>hmm</think>\nAnswer") == "Answer"
    assert strip_reasoning("<THINKING>a\nb</THINKING>Hi") == "Hi"
    assert strip_reasoning("<think>cut off mid thought") == ""
    assert strip_reasoning("no opening tag</think> result") == "result"
    assert strip_reasoning("plain") == "plain"


def test_extract_json_object_variants() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}
    assert extract_json_object('Sure!\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json_object('prefix {"a": {"b": 3}} suffix {"c": 4}') == {"a": {"b": 3}}
    assert extract_json_object('<think>{"no": 1}</think>{"yes": 1}') == {"yes": 1}
    assert extract_json_object("[1, 2]") is None
    assert extract_json_object("no json {here") is None


def test_parse_tool_request() -> None:
    names = {"get_magic_number"}
    assert parse_tool_request('{"tool": "get_magic_number", "args": {"seed": 1}}', names) == (
        "get_magic_number",
        {"seed": 1},
    )
    # Alternate key names and string-encoded args some models produce
    assert parse_tool_request(
        '{"name": "get_magic_number", "arguments": "{\\"seed\\": 2}"}', names
    ) == ("get_magic_number", {"seed": 2})
    assert parse_tool_request('{"tool": "unknown", "args": {}}', names) is None
    assert parse_tool_request("The magic number is 7.", names) is None


def test_json_tool_prompt_lists_tools() -> None:
    prompt = json_tool_system_prompt([PROBE_TOOL], preamble="Be brief.")
    assert prompt.startswith("Be brief.")
    assert "get_magic_number" in prompt and '"tool"' in prompt
