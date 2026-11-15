"""MCP tools as chat tools (`chat.tools.Tool`), built from the chosen tool list of each server.

Selection and exposure:
- only tools the user ticked (`tool_filter`) and whose policy is not `off` are offered;
- if their schemas together stay within `max_inline_tool_tokens`, each one becomes a chat
  tool named `<server>__<tool>`; otherwise the chat gets just two generic tools,
  `mcp_list_tools` and `mcp_call`, so a 4B model is not drowned in schemas.

Routing (the same in both modes):
- read-only tools (the server says `readOnlyHint: true` and the user did not override it)
  are called directly through the manager;
- everything else is a write and is only ever *requested* from the action service
  (`mcp_call` action, source `chat:<session>`), so the policy decides: suggestion, approval
  card in the UI/Telegram, or run.

Results are data from a third party: text is capped, and the engine scrubs secrets and
wraps it as untrusted before the model sees it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field

from .actions import ACTION_NAME
from .connection import McpCallError, McpUnavailable, ToolInfo, schema_tokens
from .manager import McpManager
from .settings import McpServerSettings

if TYPE_CHECKING:
    from ..chat.tools import Tool, ToolContext

NAME_LIMIT = 64
DESCRIPTION_LIMIT = 400
LIST_DESCRIPTION_LIMIT = 160
SEPARATOR = "__"

Kind = Literal["read", "write"]

_NOTES = {
    "pending": "Waiting for the user's approval (approval id {id}). Tell the user to approve "
    "or reject it in the approvals list; it has NOT been done yet.",
    "suggested": "Recorded as a suggestion only; the policy does not let it run. "
    "It has NOT been done.",
    "executed": "Done.",
    "dry_run": "Dry-run mode is on: nothing was sent. It has NOT been done.",
    "failed": "The request failed (see result.error). It has NOT been done.",
    "rejected": "Rejected (see result.detail). It has NOT been done.",
}


def display_name(name: str) -> str:
    """`server__tool` → `server · tool` (what the chat shows as the used tool)."""
    server, sep, tool = name.partition(SEPARATOR)
    return f"{server} · {tool}" if sep else name


@dataclass(frozen=True)
class McpPick:
    """One chosen tool of one server, with the routing decision made from the settings."""

    server: str
    info: ToolInfo
    kind: Kind
    name: str
    """The chat tool name (`server__tool`, made safe and unique)."""

    @property
    def tool(self) -> str:
        return self.info.name


def kind_of(cfg: McpServerSettings, info: ToolInfo) -> Kind:
    """read only if the user says so, or (without an override) the server marks it read-only."""
    override = cfg.override(info.name)
    if override is not None and override.kind != "auto":
        return override.kind
    return "read" if info.read_only is True else "write"


def pick_tools(manager: McpManager) -> list[McpPick]:
    """The tools of connected servers that the user chose and that are not switched off."""
    picks: list[McpPick] = []
    taken: set[str] = set()
    for cfg in manager.settings.servers:
        tools = manager.tools_of(cfg.name)
        if not cfg.enabled or not tools:
            continue
        by_name = {t.name: t for t in tools}
        for chosen in cfg.tool_filter:
            info = by_name.get(chosen)
            if info is None or cfg.policy_of(chosen) == "off":
                continue
            picks.append(
                McpPick(cfg.name, info, kind_of(cfg, info), _chat_name(cfg.name, chosen, taken))
            )
    return picks


def _chat_name(server: str, tool: str, taken: set[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    name = f"{server}{SEPARATOR}{safe}"
    if len(name) > NAME_LIMIT or name in taken:
        digest = hashlib.sha1(f"{server}/{tool}".encode()).hexdigest()[:8]
        name = f"{name[: NAME_LIMIT - 9]}_{digest}"
    taken.add(name)
    return name


# --------------------------------------------------------------------------- schemas


def _clean(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: _clean(v)
            for k, v in node.items()
            if k not in ("title", "$schema") or not isinstance(v, str)
        }
    if isinstance(node, list):
        return [_clean(v) for v in node]
    return node


def input_schema(info: ToolInfo) -> dict[str, Any]:
    schema = _clean(info.input_schema) or {}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def _passthrough(schema: dict[str, Any]) -> type[BaseModel]:
    """An args model that accepts whatever the model sent and reports `schema` as its JSON
    schema; the real validation against the schema is done in `_check_arguments`."""

    class Args(BaseModel):
        model_config = ConfigDict(extra="allow")

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
            return schema

    return Args


def _check_arguments(info: ToolInfo, arguments: dict[str, Any], *, hint: str = "") -> None:
    schema = input_schema(info)
    try:
        validator_cls = jsonschema.validators.validator_for(
            schema, default=jsonschema.Draft202012Validator
        )
        validator_cls.check_schema(schema)
        errors = sorted(validator_cls(schema).iter_errors(arguments), key=lambda e: list(e.path))
    except Exception:
        return  # a schema we cannot check is the server's problem, not the model's
    if errors:
        problems = "; ".join(
            f"{'.'.join(map(str, e.absolute_path)) or 'args'}: {e.message}" for e in errors[:4]
        )
        raise _tool_error(f"invalid arguments for {info.name}: {problems}{hint}")


def _tool_error(message: str) -> Exception:
    from ..chat.tools import ToolError  # lazy: chat.tools imports this package

    return ToolError(message)


# --------------------------------------------------------------------------- routing


async def invoke(
    ctx: ToolContext, manager: McpManager, pick: McpPick, arguments: dict[str, Any], hint: str = ""
) -> Any:
    """Run one MCP tool for the chat: read → call now; write → file an action request."""
    _check_arguments(pick.info, arguments, hint=hint)
    source = ctx.source or f"chat:{ctx.session_id}"
    if pick.kind == "write":
        from ..actions import ActionError

        try:
            record = await ctx.app.actions.request(
                ACTION_NAME,
                {"server": pick.server, "tool": pick.tool, "arguments": arguments},
                source,
                reason="requested in chat",
            )
        except ActionError as exc:
            raise _tool_error(str(exc)) from exc
        return {
            "action_id": record["id"],
            "status": record["status"],
            "policy": record["policy"],
            "target": record["target"],
            "result": record["result"],
            "note": _NOTES[record["status"]].format(id=record["id"]),
        }
    limit = ctx.settings.chat.max_tool_result_chars
    try:
        outcome = await manager.call(pick.server, pick.tool, arguments, source=source)
    except (McpUnavailable, McpCallError) as exc:
        raise _tool_error(str(exc)) from None
    if outcome.is_error:
        raise _tool_error(outcome.text or f"{pick.server} · {pick.tool} reported an error")
    return outcome.payload(limit)


# --------------------------------------------------------------------------- tool specs


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _description(pick: McpPick) -> str:
    base = _short(pick.info.description or pick.info.title or pick.tool, DESCRIPTION_LIMIT)
    if pick.kind == "read":
        return f"[{pick.server}] {base}"
    return (
        f"WRITE [{pick.server}] {base} Only call this when the user clearly asked for it and "
        "confirmed the details. It goes through the owner's policy and may need their "
        "approval; report exactly what the result says."
    )


def build_mcp_tools(manager: McpManager) -> list[Tool]:
    """Chat tools for the chosen MCP tools of the connected servers (empty if none)."""
    from ..chat.tools import Tool

    picks = pick_tools(manager)
    if not picks:
        return []
    budget = manager.settings.max_inline_tool_tokens
    singles = [_single_tool(manager, pick, Tool) for pick in picks]
    total = sum(schema_tokens(t.definition()) for t in singles)
    if total <= budget:
        return singles
    return _meta_tools(manager, picks, Tool)


def inline_tokens(manager: McpManager) -> int:
    """Estimated tokens the chosen tools would cost as individual definitions."""
    from ..chat.tools import Tool

    return sum(
        schema_tokens(_single_tool(manager, p, Tool).definition()) for p in pick_tools(manager)
    )


def _single_tool(manager: McpManager, pick: McpPick, tool_cls: type[Tool]) -> Tool:
    async def handler(ctx: ToolContext, args: Any) -> Any:
        return await invoke(ctx, manager, pick, args.model_dump())

    return tool_cls(
        name=pick.name,
        description=_description(pick),
        args_model=_passthrough(input_schema(pick.info)),
        kind=pick.kind,
        handler=handler,
    )


class ListToolsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str | None = Field(
        default=None, description="MCP server name. Omit to list the servers."
    )
    tool: str | None = Field(
        default=None, description="Tool name: returns its description and arguments."
    )


class CallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server: str = Field(description="MCP server name.")
    tool: str = Field(description="Tool name, as listed by mcp_list_tools.")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="The tool's arguments (see mcp_list_tools)."
    )


def _meta_tools(manager: McpManager, picks: list[McpPick], tool_cls: type[Tool]) -> list[Tool]:
    index = {(p.server, p.tool): p for p in picks}

    def listing(server: str) -> list[McpPick]:
        return [p for p in picks if p.server == server]

    async def list_tools(ctx: ToolContext, args: ListToolsArgs) -> Any:
        notes = manager.unavailable()
        if args.server is None:
            servers = sorted({p.server for p in picks})
            return {
                "servers": [{"name": s, "tools": [p.tool for p in listing(s)]} for s in servers],
                **({"unavailable": notes} if notes else {}),
            }
        mine = listing(args.server)
        if not mine:
            known = ", ".join(sorted({p.server for p in picks})) or "none"
            raise _tool_error(f"no usable MCP server {args.server!r}; available: {known}")
        if args.tool is None:
            return {
                "server": args.server,
                "tools": [
                    {
                        "name": p.tool,
                        "kind": p.kind,
                        "description": _short(p.info.description or "", LIST_DESCRIPTION_LIMIT),
                    }
                    for p in mine
                ],
            }
        pick = index.get((args.server, args.tool))
        if pick is None:
            raise _tool_error(
                f"{args.server} has no usable tool {args.tool!r}; "
                f"tools: {', '.join(p.tool for p in mine)}"
            )
        return {
            "server": args.server,
            "name": pick.tool,
            "kind": pick.kind,
            "description": _short(pick.info.description or "", DESCRIPTION_LIMIT),
            "arguments_schema": input_schema(pick.info),
        }

    async def call(ctx: ToolContext, args: CallArgs) -> Any:
        pick = index.get((args.server, args.tool))
        if pick is None:
            raise _tool_error(
                f"unknown MCP tool {args.server} · {args.tool}; use mcp_list_tools to see them"
            )
        return await invoke(
            ctx,
            manager,
            pick,
            args.arguments,
            hint=f" (see mcp_list_tools server={args.server} tool={args.tool})",
        )

    any_write = any(p.kind == "write" for p in picks)
    return [
        tool_cls(
            name="mcp_list_tools",
            description=(
                "List the external MCP servers you can use and their tools; with server and "
                "tool, the arguments of one tool. Use this before mcp_call."
            ),
            args_model=ListToolsArgs,
            kind="read",
            handler=list_tools,
        ),
        tool_cls(
            name="mcp_call",
            description=(
                "Call a tool of an external MCP server (see mcp_list_tools). Tools marked "
                "write change things: only call them when the user clearly asked; they go "
                "through the owner's policy and may need approval, report exactly what the "
                "result says."
            ),
            args_model=CallArgs,
            kind="write" if any_write else "read",
            handler=call,
        ),
    ]


def overview_lines(manager: McpManager) -> list[str]:
    """One line per server naming its chosen tools. Only needed with the generic pair
    (the model cannot see the tools then, and a small model does not think of asking)."""
    by_server: dict[str, list[str]] = {}
    for pick in pick_tools(manager):
        by_server.setdefault(pick.server, []).append(
            pick.tool if pick.kind == "read" else f"{pick.tool} (write)"
        )
    return [
        f"MCP server {server!r} offers: {', '.join(names)}. Use mcp_list_tools / mcp_call for it."
        for server, names in by_server.items()
    ]


def schema_report(info: ToolInfo) -> int:
    """Tokens this tool's definition would cost as an individual chat tool."""
    return schema_tokens(
        {
            "name": "x" * 20,
            "description": _short(info.description or info.title or info.name, DESCRIPTION_LIMIT),
            "parameters": input_schema(info),
        }
    )


__all__ = [
    "build_mcp_tools",
    "display_name",
    "inline_tokens",
    "invoke",
    "kind_of",
    "overview_lines",
    "pick_tools",
    "schema_report",
]
