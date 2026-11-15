"""Discovery for the Settings page ("Test" button): connect, `initialize`, `tools/list`.

`check_mcp_server` takes the *unsaved* settings of one server and returns what a person can
act on: server name and version, the transport that worked, and every tool with its
description, whether the server calls it read-only, and what its schema would cost the chat
model (tokens), so the tool picker can show it. It never raises and never includes secrets.

The result has the shape of `conntest.ConnResult` (ok, message, details): the Settings test
endpoint wraps it as is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .connection import McpConnection, McpUnavailable, ToolInfo
from .settings import McpServerSettings
from .tools import kind_of, schema_report

DESCRIPTION_LIMIT = 300


@dataclass
class McpCheck:
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def tool_row(cfg: McpServerSettings, info: ToolInfo) -> dict[str, Any]:
    description = " ".join(info.description.split())
    if len(description) > DESCRIPTION_LIMIT:
        description = description[: DESCRIPTION_LIMIT - 1] + "…"
    return {
        "name": info.name,
        "title": info.title,
        "description": description,
        "read_only": info.read_only,
        "destructive": info.destructive,
        "kind": kind_of(cfg, info),
        "schema_tokens": schema_report(info),
        "selected": info.name in cfg.tool_filter,
    }


async def check_mcp_server(cfg: McpServerSettings) -> McpCheck:
    conn = McpConnection(cfg)
    secrets = cfg.secret_values()
    try:
        try:
            await conn.ensure(force=True)
        except McpUnavailable:
            return McpCheck(
                False, _scrub(conn.last_error or "The server could not be reached.", secrets)
            )
        assert conn.info is not None and conn.tools is not None
        rows = [tool_row(cfg, t) for t in conn.tools]
        total = sum(r["schema_tokens"] for r in rows)
        transport = {"streamable_http": "Streamable HTTP", "sse": "HTTP+SSE"}.get(
            conn.transport or "", conn.transport or "?"
        )
        info = conn.info
        message = (
            f"Connected to {info.name} {info.version} over {transport}: "
            f"{len(rows)} tool{'s' if len(rows) != 1 else ''} "
            f"({sum(1 for r in rows if r['read_only'])} read-only)."
        )
        return McpCheck(
            True,
            message,
            {
                "server": {"name": info.name, "version": info.version, "protocol": info.protocol},
                "transport": conn.transport,
                "tools": rows,
                "schema_tokens_total": total,
            },
        )
    except Exception as exc:  # a bug must not become a 500 in the form
        return McpCheck(False, f"The test failed unexpectedly ({type(exc).__name__}).")
    finally:
        await conn.close()


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text
