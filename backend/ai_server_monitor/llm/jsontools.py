"""JSON-in-text tool protocol for models without native tool calling.

The model is told to answer with ONLY `{"tool": "<name>", "args": {...}}` when it wants a
tool, and with plain text otherwise.
"""

from __future__ import annotations

import json
from typing import Any

from .text import extract_json_object


def describe_tools(tools: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for tool in tools:
        fn = tool.get("function", tool)
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        lines.append(
            f"- {fn['name']}: {fn.get('description', '').strip()}\n"
            f"  parameters (JSON schema): {json.dumps(params, separators=(',', ':'))}"
        )
    return "\n".join(lines)


def json_tool_system_prompt(tools: list[dict[str, Any]], preamble: str = "") -> str:
    head = f"{preamble.strip()}\n\n" if preamble.strip() else ""
    return (
        f"{head}You can use these tools:\n{describe_tools(tools)}\n\n"
        "To use a tool, reply with ONLY a single JSON object and nothing else, exactly:\n"
        '{"tool": "<tool name>", "args": {<arguments>}}\n'
        "You will then receive the tool result and can continue. "
        "When you do not need a tool, reply to the user normally in plain text without JSON."
    )


def parse_tool_request(text: str, tool_names: set[str]) -> tuple[str, dict[str, Any]] | None:
    """Return (tool, args) if the reply is a tool request for a known tool."""
    obj = extract_json_object(text)
    if not obj:
        return None
    name = obj.get("tool") or obj.get("name") or obj.get("function")
    if not isinstance(name, str) or name not in tool_names:
        return None
    args = obj.get("args", obj.get("arguments", obj.get("parameters", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    return name, args
