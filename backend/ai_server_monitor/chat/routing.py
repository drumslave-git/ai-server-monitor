"""Tool routing: send the model only the tool groups a question needs.

Every enabled tool used to go to the model on every question (19 tools, ~2,900 tokens of
definitions for an 11-token question), and a small model then picks the wrong one. With routing
on, one short model call (`chat_route` in the Logs) names the relevant groups, the model gets
those tools plus `get_status` and `more_tools(group)`, and a wrong classification is repaired in
the same answer by calling `more_tools`.

Groups: host, media, metrics, containers, actions and `mcp:<server>`. A tool may belong to
several groups; a tool without a group is an error that `tests/test_chat_routing.py` catches.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ..llm.text import extract_json_object, strip_reasoning
from ..tracing import trace_scope
from .tools import Tool, ToolContext, ToolError, _Args

if TYPE_CHECKING:
    from ..llm import LLMClient

log = logging.getLogger(__name__)

MORE_TOOLS = "more_tools"
ALWAYS = ("get_status", MORE_TOOLS)
"""Sent with every question, whatever the classifier says."""

ROUTE_TIMEOUT_S = 60.0
ROUTE_MAX_TOKENS = 400
"""The answer is a few words; the rest is room for a model that thinks despite
`reasoning_effort: none` (a thinking model that spends the whole limit gives no answer)."""
MCP_PREFIX = "mcp:"
STICKY_TOOL_CALLS = 60
"""How many of the newest tool results of a conversation tell which groups are in use."""

GROUP_HELP: dict[str, str] = {
    "host": "the server itself: disks and what fills them, files, logs and services "
    "(journalctl, systemctl), processes, ports, CPU/RAM/GPU, collected data, history and trends, "
    "status reports",
    "media": "Radarr, Sonarr, Whisparr, Prowlarr, qBittorrent: movies, series, queues, "
    "downloads, torrents, indexers, adding or removing media; why one of these services fails",
    "metrics": "Prometheus: PromQL queries and metric trends",
    "containers": "Docker containers: state, restarts, logs, mounts, ports, config, Docker API",
    "actions": "the owner's approvals and the audit log of actions (what is waiting, what was "
    "approved or done)",
}

TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "run_command": ("host",),
    "disk_usage": ("host",),
    "metric_history": ("host",),
    "listening_ports": ("host", "containers"),
    "get_collector_data": ("host",),
    "list_reports": ("host",),
    "get_report": ("host",),
    "container_inspect": ("containers",),
    "container_logs": ("containers",),
    "promql_query": ("metrics",),
    "arr_lookup": ("media",),
    "arr_quality_profiles": ("media",),
    "arr_root_folders": ("media",),
    "get_arr_overview": ("media",),
    "diagnose_service": ("media",),
    "api_call": ("media", "containers", "metrics"),
    "api_endpoints": ("media", "containers", "metrics"),
    "list_pending_approvals": ("actions",),
    "list_recent_actions": ("actions",),
    # the WRITE tools of chat/tools.py
    "qbit_delete_torrent": ("media",),
    "arr_remove_queue_item": ("media",),
    "arr_search": ("media",),
    "arr_add_media": ("media",),
    "prowlarr_test_indexer": ("media",),
}
MCP_META_TOOLS = ("mcp_list_tools", "mcp_call")
"""The generic pair that stands in for many MCP tools: it belongs to every MCP server group."""


def groups_of(name: str, mcp_servers: Collection[str] = ()) -> tuple[str, ...]:
    """The groups of tool `name` (empty for the always-sent tools and for unknown ones)."""
    if name in ALWAYS:
        return ()
    if name in TOOL_GROUPS:
        return TOOL_GROUPS[name]
    if name in MCP_META_TOOLS:
        return tuple(f"{MCP_PREFIX}{s}" for s in sorted(mcp_servers))
    server, sep, _ = name.partition("__")
    if sep and server:
        return (f"{MCP_PREFIX}{server}",)
    return ()


def catalog(tools: Mapping[str, Tool], mcp_servers: Collection[str] = ()) -> dict[str, list[str]]:
    """group -> its tool names, for the tools that exist right now (empty groups left out)."""
    out: dict[str, list[str]] = {}
    for name in tools:
        for group in groups_of(name, mcp_servers):
            out.setdefault(group, []).append(name)
    return out


def ungrouped(tools: Iterable[str], mcp_servers: Collection[str] = ()) -> list[str]:
    """Tools that belong to no group and are not always sent (the guard test wants none)."""
    return [t for t in tools if t not in ALWAYS and not groups_of(t, mcp_servers)]


def estimate_tokens(definitions: list[dict[str, Any]]) -> int:
    """Rough token count (4 characters each) of tool definitions."""
    return round(len(json.dumps(definitions, separators=(",", ":"), default=str)) / 4)


# --------------------------------------------------------------------------- state of a turn


@dataclass
class Routing:
    """Which groups are loaded in one question; `more_tools` adds to it."""

    groups: dict[str, list[str]]
    active: set[str] = field(default_factory=set)
    chosen: list[str] = field(default_factory=list)
    """What the classifier (and the conversation so far) picked, before `more_tools`."""
    loaded: list[str] = field(default_factory=list)
    """Groups added later by `more_tools`."""
    classifier_s: float = 0.0
    fallback: str | None = None
    """Why every group is loaded, when the classifier could not be used."""

    def names(self) -> set[str]:
        out = set(ALWAYS)
        for group in self.active:
            out.update(self.groups.get(group, ()))
        return out

    def activate(self, group: str) -> list[str]:
        self.active.add(group)
        if group not in self.chosen and group not in self.loaded:
            self.loaded.append(group)
        return list(self.groups.get(group, ()))

    def summary(self) -> dict[str, Any]:
        return {
            "groups": sorted(self.chosen),
            "loaded_later": list(self.loaded),
            "tools": len(self.names()),
            "classifier_s": round(self.classifier_s, 2),
            **({"fallback": self.fallback} if self.fallback else {}),
        }


def wanted(mode: str, definitions: list[dict[str, Any]], budget: int) -> bool:
    """Is routing applied to a question with these tool definitions?"""
    if mode == "always":
        return True
    if mode == "auto":
        return estimate_tokens(definitions) > budget
    return False


# --------------------------------------------------------------------------- more_tools


class MoreToolsArgs(_Args):
    group: str = Field(description="Tool group to load, as listed in this tool's description.")


def _more_tools_handler(ctx: ToolContext, args: MoreToolsArgs) -> Any:
    state: Routing | None = ctx.routing
    if state is None:
        raise ToolError("all tools are already available")
    if args.group not in state.groups:
        raise ToolError(f"no tool group {args.group!r}; choose one of: {', '.join(state.groups)}")
    if args.group in state.active:
        return {"group": args.group, "note": "already loaded", "tools": state.groups[args.group]}
    added = state.activate(args.group)
    return {
        "group": args.group,
        "loaded": added,
        "note": "These tools can be called now.",
    }


def more_tools(groups: Mapping[str, list[str]]) -> Tool:
    """The escape hatch: load another tool group when the first choice was wrong."""
    lines = "; ".join(f"{g} ({', '.join(names)})" for g, names in groups.items())

    async def handler(ctx: ToolContext, args: MoreToolsArgs) -> Any:
        return _more_tools_handler(ctx, args)

    return Tool(
        MORE_TOOLS,
        "Load more tools when the ones you have cannot answer the question. Call it with the "
        f"group name, then call the loaded tool. Groups: {lines}.",
        MoreToolsArgs,
        "read",
        handler,
    )


# --------------------------------------------------------------------------- the classifier

ROUTE_SYSTEM = (
    "You route questions for a server-monitor assistant. Pick the tool groups needed to "
    "answer the user's latest message. Pick every group that may be needed, usually one or "
    "two. If the message needs no tool (greeting, thanks, general knowledge), pick none.\n"
    "Groups:\n{groups}\n"
    '{used}Reply with ONLY JSON like {{"groups": ["host"]}}. Use only the group names above.'
)


def route_system(
    groups: Mapping[str, list[str]], tools: Mapping[str, Tool], used: list[str]
) -> str:
    lines = []
    for group in groups:
        if group.startswith(MCP_PREFIX):
            server = group[len(MCP_PREFIX) :]
            text = f"tools of the external MCP server {server}: " + ", ".join(
                n.partition("__")[2] or n for n in groups[group][:8]
            )
        else:
            text = GROUP_HELP.get(group, group)
        lines.append(f"- {group}: {text}")
    note = f"Groups already used in this conversation: {', '.join(used)}.\n" if used else ""
    return ROUTE_SYSTEM.format(groups="\n".join(lines), used=note)


def parse_groups(text: str, known: Collection[str]) -> list[str] | None:
    """The group names in a classifier reply (None = unusable reply)."""
    text = strip_reasoning(text)
    obj = extract_json_object(text)
    raw: Any = None
    if isinstance(obj, dict):
        raw = obj.get("groups", obj.get("group"))
    else:
        start, end = text.find("["), text.rfind("]")
        if start != -1 and end > start:
            try:
                raw = json.loads(text[start : end + 1])
            except ValueError:
                raw = None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    by_lower = {k.lower(): k for k in known}
    out: list[str] = []
    for item in raw:
        key = by_lower.get(str(item).strip().lower())
        if key and key not in out:
            out.append(key)
    return out


async def classify(
    llm: LLMClient,
    question: str,
    groups: Mapping[str, list[str]],
    tools: Mapping[str, Tool],
    used: list[str],
    *,
    session_id: str,
    turn: str,
) -> list[str] | None:
    """Ask the model which groups the question needs (None = failed; the caller loads all)."""
    messages = [
        {"role": "system", "content": route_system(groups, tools, used)},
        {"role": "user", "content": question[:1500]},
    ]
    try:
        async with asyncio.timeout(ROUTE_TIMEOUT_S):
            with trace_scope("chat_route", link_kind="chat", link_id=session_id, turn=turn):
                completion = await llm.complete(
                    messages,
                    temperature=0.0,
                    max_tokens=ROUTE_MAX_TOKENS,
                    reasoning_effort="none",
                )
    except Exception as exc:
        log.info("chat: routing call failed (%s: %s)", type(exc).__name__, exc)
        return None
    found = parse_groups(completion.content, groups)
    if found is None:
        log.info("chat: unusable routing reply %r", completion.content[:120])
    return found
