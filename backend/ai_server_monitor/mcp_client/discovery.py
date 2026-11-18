"""Discovery for the Settings page ("Test" button): connect, list the tools.

`check_mcp_server` takes the *unsaved* settings of one server and returns what a person can act
on: server name and version, the transport, and every tool with its whole description, whether
the server calls it read-only, its effective kind and whether it is chosen. It never raises and
never includes secrets. The result has the shape of `conntest.ConnResult` (ok, message, details).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .settings import McpServerSettings
from .toolsets import failure_text, kind_of, server_toolset, transport_label


@dataclass
class McpCheck:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


async def check_mcp_server(cfg: McpServerSettings) -> McpCheck:
    toolset = server_toolset(cfg)
    try:
        async with toolset:
            tools = await toolset.list_tools()
            info = toolset.server_info
    except Exception as exc:
        return McpCheck(False, f"The server could not be used: {failure_text(exc, cfg)}")
    rows = []
    for tool in tools:
        annotations = tool.annotations.model_dump(by_alias=True) if tool.annotations else {}
        rows.append(
            {
                "name": tool.name,
                "title": tool.title,
                "description": (tool.description or "").strip(),
                "read_only": annotations.get("readOnlyHint"),
                "destructive": annotations.get("destructiveHint"),
                "kind": kind_of(cfg, tool.name, annotations),
                "selected": tool.name in cfg.tool_filter,
            }
        )
    transport = transport_label(cfg)
    name = info.name if info else "the server"
    version = info.version if info else ""
    reads = sum(1 for r in rows if r["read_only"] is True)
    message = (
        f"Connected to {name} {version} over {transport}: "
        f"{len(rows)} tool{'s' if len(rows) != 1 else ''} ({reads} read-only)."
    ).replace("  ", " ")
    return McpCheck(
        True,
        message,
        {
            "server": {"name": name, "version": version},
            "transport": transport,
            "tools": rows,
        },
    )
