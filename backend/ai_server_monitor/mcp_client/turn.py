"""The MCP part of one chat turn: connect the servers, record and sort what they list, pick the
request's domains (one call of the worker model) and give the agent one Pydantic AI `Toolset`
capability per domain: picked and already loaded domains directly, the others deferred (the
library adds `load_capability` and their catalog to the instructions).

A tool is offered only through its domain: an enabled tool whose sorting failed is not offered
and the turn's notes say so. A server that cannot be reached costs its timeout once per turn and
is named in the notes and the turn log; nothing is remembered between turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any, Literal

from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability, Toolset
from pydantic_ai.models import Model
from pydantic_ai.toolsets import AbstractToolset, CombinedToolset

from ..agent.models import helper_output
from ..agent.prompts import now_line
from ..tracing import trace_scope
from .domains import DomainStore, full_name, listed
from .toolsets import McpRun, failure_text

log = logging.getLogger(__name__)

CAPABILITY = "domain-{}"
LOAD_NOTE = (
    "Tools of external MCP servers are grouped into domains. If a tool you need is missing, "
    "load its domain with `load_capability` (the deferred domains are listed below)."
)
LOOKUP_NOTE = (
    "Facts that change or that a connected tool can look up (the state of this server, and "
    "whatever the MCP domains listed here cover) are looked up with tools, loading a domain "
    "when its tools are not offered, and never answered from memory; if no tool can look it "
    "up, say that the answer is from memory and may be out of date."
)
CLASSIFY_PROMPT = """\
Pick the domains whose tools are needed to answer the request. Answer with the names of the \
needed domains, or an empty list if the request needs none of them. Pick every domain that \
may be needed. Do not explain."""


@dataclass
class McpTurn:
    capabilities: list[AbstractCapability[Any]] = field(default_factory=list)
    picked: list[str] = field(default_factory=list)
    offered: list[str] = field(default_factory=list)
    """Domains given directly (picked, loaded earlier in the session, or kept by a resume)."""
    deferred: list[str] = field(default_factory=list)
    unsorted: list[str] = field(default_factory=list)
    """Enabled tools without a domain (`server_tool`): not offered."""
    unreachable: dict[str, str] = field(default_factory=dict)
    classify_error: str | None = None
    """Why the classification failed (then nothing is picked: every domain is loadable)."""

    @property
    def used(self) -> bool:
        """MCP servers took part in this turn (the turn log has an entry)."""
        return bool(self.capabilities or self.unsorted or self.unreachable)

    def notes(self) -> list[str]:
        """For the instructions (unreachable servers: `McpRun.unavailable()`)."""
        if not self.unsorted:
            return []
        return [
            "These MCP tools are not offered (they could not be sorted into a domain): "
            + ", ".join(self.unsorted)
        ]

    def log(self, loaded: list[str]) -> dict[str, Any]:
        """The turn log (assistant message meta `mcp_domains`)."""
        prefix = CAPABILITY.format("")
        return {
            "picked": self.picked,
            "offered": self.offered,
            "loaded": sorted(
                c.removeprefix(prefix)
                for c in loaded
                if c.startswith(prefix) and c.removeprefix(prefix) in self.deferred
            ),
            **({"unsorted": self.unsorted} if self.unsorted else {}),
            **({"unreachable": self.unreachable} if self.unreachable else {}),
            **({"classify_error": self.classify_error} if self.classify_error else {}),
        }


async def connect(
    mcp: McpRun, stack: contextlib.AsyncExitStack, store: DomainStore, sort_model: Model | None
) -> None:
    """Enter every server (in parallel; `stack` closes them after the run), record what each
    lists, and sort its tools that have no domain yet (the turn waits for it)."""
    await asyncio.gather(*(stack.enter_async_context(r) for r in mcp.servers))
    up = []
    for reach in mcp.servers:
        assert reach.cfg is not None
        if reach.down is None:
            try:
                tools = listed(await reach.wrapped.list_tools())  # type: ignore[attr-defined]
            except Exception as exc:
                reach.down = failure_text(exc, reach.cfg, reach.seen)
        if reach.down is not None:
            await store.record_failure(reach.cfg.name, reach.down)
            continue
        await store.record_listing(reach.cfg.name, tools)
        up.append(reach.cfg.name)
    if up:
        await store.sort_tools(sort_model, up)


async def classify(
    model: Model,
    domains: list[dict[str, Any]],
    request: str,
    tools: dict[str, list[str]] | None = None,
    zone: tzinfo | None = None,
) -> list[str]:
    """The domains `request` needs, picked by `model` (structured: names of existing domains).
    `tools`: domain name -> names of its enabled tools (shown next to the description);
    `zone`: the owner's time zone (the instructions get the current moment)."""
    names = tuple(d["name"] for d in domains)
    agent: Agent[None, list[Any]] = Agent(
        model,
        instructions=[CLASSIFY_PROMPT, lambda: now_line(zone) if zone is not None else ""],
        output_type=helper_output(model, list[Literal[names]]),  # type: ignore[valid-type]
    )
    tools = tools or {}
    prompt = "Domains:\n" + "\n".join(
        f"- {d['name']}: {d['description']}"
        + (f" (tools: {', '.join(tools[d['name']])})" if tools.get(d["name"]) else "")
        for d in domains
    )
    prompt += f"\n\nRequest:\n{request}"
    with trace_scope("mcp_classify"):
        result = await agent.run(prompt)
    return sorted(set(result.output))


async def prepare(
    mcp: McpRun,
    stack: contextlib.AsyncExitStack,
    store: DomainStore,
    *,
    sort_model: Model | None,
    classify_model: Model | None,
    request: str | None,
    keep: list[str],
    only: list[str] | None = None,
    zone: tzinfo | None = None,
) -> McpTurn:
    """Connect, sort, classify `request` (None: a resumed run or a job, nothing to classify)
    and build the domain capabilities; `keep` = domains given directly besides the picked ones;
    `only` (jobs) = no other domain is offered, not even deferred."""
    turn = McpTurn()
    if not mcp.servers:
        return turn
    await connect(mcp, stack, store, sort_model)
    views = {r.cfg.name: r.view for r in mcp.servers if r.cfg is not None and r.down is None}
    turn.unreachable = {
        r.cfg.name: r.down for r in mcp.servers if r.cfg is not None and r.down is not None
    }
    chosen = {r.cfg.name: set(r.cfg.tool_filter) for r in mcp.servers if r.cfg is not None}
    placed: dict[int, dict[str, set[str]]] = {}
    for t in await store.tools(views):
        if t["tool"] not in chosen[t["server"]]:
            continue
        if t["domain_id"] is None:
            turn.unsorted.append(full_name(t["server"], t["tool"]))
            continue
        placed.setdefault(t["domain_id"], {}).setdefault(t["server"], set()).add(
            full_name(t["server"], t["tool"])
        )
    domains = [
        d
        for d in await store.domains()
        if d["id"] in placed and (only is None or d["name"] in only)
    ]
    if not domains:
        return turn
    if request is not None and classify_model is not None:
        try:
            tools = {
                d["name"]: sorted(n for group in placed[d["id"]].values() for n in group)
                for d in domains
            }
            turn.picked = await classify(classify_model, domains, request, tools, zone)
        except Exception as exc:  # a miss of everything: the model loads what it needs
            turn.classify_error = f"classification failed: {type(exc).__name__}: {exc}"[:500]
            log.warning("MCP %s", turn.classify_error)
    names = {d["name"] for d in domains}
    turn.offered = sorted(names & {*turn.picked, *keep})
    for d in domains:
        parts: list[AbstractToolset[Any]] = [
            views[server].filtered(lambda ctx, td, tools=tools: td.name in tools)
            for server, tools in sorted(placed[d["id"]].items())
        ]
        toolset = parts[0] if len(parts) == 1 else CombinedToolset(parts)
        count = sum(len(t) for t in placed[d["id"]].values())
        deferred = d["name"] not in turn.offered
        if deferred:
            turn.deferred.append(d["name"])
        turn.capabilities.append(
            Toolset(
                toolset,
                id=CAPABILITY.format(d["name"]),
                description=f"{d['description']} ({count} tools)",
                defer_loading=deferred,
            )
        )
    return turn
