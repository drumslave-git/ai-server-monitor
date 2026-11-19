"""The one write action for MCP tools: `mcp_call`.

A single action (not one per tool) because tools are discovered at runtime while approvals
must survive restarts: the audit row holds `{server, tool, arguments}` and everything else
(policy, whether the tool is still chosen) is read from the current settings when the request
is made and again when it is approved.

The functions here are the pieces of an `ActionDef`; `actions/registry.py` assembles them.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .settings import McpSettings
from .toolsets import McpServers

ACTION_NAME = "mcp_call"
DESCRIPTION = "Call a tool of an MCP server that may change something outside this monitor."
RESULT_CHARS = 2000


class McpCallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    """Human-readable name of what this touches; only used in the audit log."""
    server: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


def policy_of(settings: McpSettings, args: McpCallArgs) -> str:
    """The policy for this call (suggest | approve | auto), from the settings."""
    cfg = settings.server(args.server)
    return cfg.policy_of(args.tool) if cfg is not None else "approve"


def policy_key(args: McpCallArgs) -> str:
    return f"mcp:{args.server}"


def target(args: McpCallArgs) -> str:
    return f"{args.server} · {args.title or args.tool}"


def kind(args: McpCallArgs) -> str:
    """The calls one "approve the rest of this answer" covers: this tool of this server,
    whatever its arguments."""
    return f"{args.server} · {args.tool}"


def summary(items: list[McpCallArgs]) -> str:
    """ "arr · remove_queue_item × 5" / "arr · pause × 2, arr · remove_queue_item × 3"."""
    if len(items) == 1:
        return target(items[0])
    counts: dict[str, int] = {}
    for item in items:
        counts[kind(item)] = counts.get(kind(item), 0) + 1
    return ", ".join(f"{name} × {n}" if n > 1 else name for name, n in counts.items())


def check(settings: McpSettings, args: McpCallArgs) -> str | None:
    """Why the call cannot be carried out now (None = fine). Checked on request and again
    on approval, so a tool removed from the list in between is not run."""
    cfg = settings.server(args.server)
    if cfg is None or not cfg.enabled:
        return f"no enabled MCP server named {args.server!r}"
    if args.tool not in cfg.tool_filter:
        return f"{args.tool!r} is not one of the tools chosen for {args.server!r}"
    return None


async def execute(servers: McpServers, args: McpCallArgs) -> dict[str, Any]:
    """Run the approved call. Failures raise ActionError (recorded as `failed`)."""
    from ..actions import ActionError  # lazy: the action package imports settings types

    try:
        result = await servers.call(args.server, args.tool, args.arguments, source="action")
    except RuntimeError as exc:
        raise ActionError(str(exc)) from None
    text = result if isinstance(result, str) else str(result)
    if len(text) > RESULT_CHARS:
        text = text[: RESULT_CHARS - 1] + "…"
    return {"server": args.server, "tool": args.tool, "result": text}
