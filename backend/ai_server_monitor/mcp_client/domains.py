"""MCP tool domains: where they are stored (`mcp_domains`, `mcp_tools`, `mcp_listings`) and how
tools are sorted into them (one structured call of the main model).

A domain is an area a person asks about (an application, a device, a service), not a server.
None is predefined. Every tool of a configured server is recorded as it was last listed; a tool
without a domain is sorted as soon as a listing shows it, and again when the owner asks. The
owner's own placements and domain texts (`set_by='owner'`) are kept by a re-sort unless the
owner asks to redo his placements. A tool whose sorting failed keeps no domain and the reason.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model

from ..db import Database
from ..tracing import trace_scope
from .settings import McpServerSettings
from .toolsets import SEPARATOR, failure_text, server_toolset

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
NAME_RULE = "a domain name is lowercase letters, digits and '-' (up to 24)"

SORT_PROMPT = """\
You sort the tools of MCP servers into domains. A domain is one area a person would ask about \
(one application, device or service), not a server and not a verb: reading and changing the same \
thing belong to one domain. Put every tool you are given into exactly one domain: an existing one \
when it fits, otherwise a new one. New domain names: lowercase words joined by '-', at most 24 \
characters. Descriptions: one line saying what a person can ask about there. Return only the \
domains you put tools into, each with the full names of its tools as given."""


class DomainOut(BaseModel):
    name: str = Field(description="An existing domain's name, or a new one.")
    description: str = Field(default="", description="One line; required for a new domain.")
    tools: list[str] = Field(description="Full tool names as given (server_tool).")


@dataclass
class ListedTool:
    name: str
    description: str
    read_only: bool | None


class DomainError(Exception):
    """A domain change the owner asked for that cannot be done (shown as is)."""


def full_name(server: str, tool: str) -> str:
    return f"{server}{SEPARATOR}{tool}"


def listed(tools: Iterable[Any]) -> list[ListedTool]:
    """`mcp` Tool objects (as `MCPToolset.list_tools()` returns them) → ListedTool."""
    out = []
    for tool in tools:
        notes = tool.annotations.model_dump(by_alias=True) if tool.annotations else {}
        hint = notes.get("readOnlyHint")
        out.append(ListedTool(tool.name, (tool.description or "").strip(), hint))
    return out


async def list_server(cfg: McpServerSettings) -> list[ListedTool]:
    """Connect one server and list its tools (raises with a text safe to show)."""
    toolset = server_toolset(cfg)
    try:
        async with toolset:
            return listed(await toolset.list_tools())
    except Exception as exc:
        raise RuntimeError(failure_text(exc, cfg)) from None


class DomainStore:
    def __init__(self, db: Database):
        self.db = db
        self._sorting = asyncio.Lock()
        """One sorting at a time: a run that needs its tools sorted waits for a running sort
        and then finds them placed."""

    # ------------------------------------------------------------------ reading

    async def domains(self) -> list[dict[str, Any]]:
        """Every domain with its tool count and the names of the jobs that use it."""
        rows = await self.db.fetchall(
            "SELECT d.id, d.name, d.description, d.set_by, "
            "(SELECT COUNT(*) FROM mcp_tools t WHERE t.domain_id = d.id) AS tools "
            "FROM mcp_domains d ORDER BY d.name"
        )
        users = await self.used_by()
        return [{**dict(r), "used_by": users.get(r["id"], [])} for r in rows]

    async def used_by(self) -> dict[int, list[str]]:
        rows = await self.db.fetchall(
            "SELECT jd.domain_id, j.name FROM job_domains jd JOIN jobs j ON j.id = jd.job_id "
            "ORDER BY j.name"
        )
        out: dict[int, list[str]] = {}
        for r in rows:
            out.setdefault(r["domain_id"], []).append(r["name"])
        return out

    async def tools(self, servers: Iterable[str] | None = None) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT t.server, t.tool, t.description, t.read_only, t.domain_id, t.set_by, "
            "t.sort_error, t.seen_ts, d.name AS domain FROM mcp_tools t "
            "LEFT JOIN mcp_domains d ON d.id = t.domain_id ORDER BY t.server, t.tool"
        )
        wanted = None if servers is None else set(servers)
        return [dict(r) for r in rows if wanted is None or r["server"] in wanted]

    async def listings(self) -> dict[str, dict[str, Any]]:
        rows = await self.db.fetchall("SELECT server, ts, error FROM mcp_listings")
        return {r["server"]: {"ts": r["ts"], "error": r["error"]} for r in rows}

    # ------------------------------------------------------------------ listings

    async def record_listing(self, server: str, tools: list[ListedTool]) -> list[str]:
        """Store what a server lists now (tools it no longer lists are dropped) and return the
        names of its tools that have no domain."""
        now = time.time()
        async with self.db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO mcp_tools (server, tool, description, read_only, seen_ts) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (server, tool) DO UPDATE SET "
                "description = excluded.description, read_only = excluded.read_only, "
                "seen_ts = excluded.seen_ts",
                [(server, t.name, t.description, t.read_only, now) for t in tools],
            )
            names = [t.name for t in tools]
            gone = f" AND tool NOT IN ({','.join('?' * len(names))})" if names else ""
            await conn.execute(f"DELETE FROM mcp_tools WHERE server = ?{gone}", (server, *names))
            await conn.execute(
                "INSERT INTO mcp_listings (server, ts, error) VALUES (?, ?, NULL) "
                "ON CONFLICT (server) DO UPDATE SET ts = excluded.ts, error = NULL",
                (server, now),
            )
        rows = await self.db.fetchall(
            "SELECT tool FROM mcp_tools WHERE server = ? AND domain_id IS NULL", (server,)
        )
        return [r["tool"] for r in rows]

    async def record_failure(self, server: str, error: str) -> None:
        """A listing that failed: the stored tools stay (the page shows them as unreachable)."""
        await self.db.write(
            "INSERT INTO mcp_listings (server, ts, error) VALUES (?, ?, ?) "
            "ON CONFLICT (server) DO UPDATE SET ts = excluded.ts, error = excluded.error",
            (server, time.time(), error),
        )

    async def forget_servers(self, keep: Iterable[str]) -> None:
        """Drop the tools and listing state of servers that are no longer configured."""
        names = list(keep)
        gone = f" WHERE server NOT IN ({','.join('?' * len(names))})" if names else ""
        async with self.db.transaction() as conn:
            await conn.execute(f"DELETE FROM mcp_tools{gone}", names)
            await conn.execute(f"DELETE FROM mcp_listings{gone}", names)

    async def refresh(
        self,
        servers: list[McpServerSettings],
        model: Model | None,
        *,
        again: bool = False,
        include_owner: bool = False,
    ) -> dict[str, Any]:
        """List the given servers live (in parallel), record what they list, then sort: the
        tools without a domain, or with `again` the servers' tools as `sort_tools` says.
        Returns {"sorted": n, "error": sorting error or None, "unreachable": {server: why}}."""
        results = await asyncio.gather(
            *(list_server(cfg) for cfg in servers), return_exceptions=True
        )
        unreachable: dict[str, str] = {}
        reached = []
        for cfg, result in zip(servers, results, strict=True):
            if isinstance(result, BaseException):
                unreachable[cfg.name] = str(result)
                await self.record_failure(cfg.name, str(result))
            else:
                await self.record_listing(cfg.name, result)
                reached.append(cfg.name)
        sort = await self.sort_tools(model, reached, again=again, include_owner=include_owner)
        return {**sort, "unreachable": unreachable}

    # ------------------------------------------------------------------ sorting

    async def sort_tools(
        self,
        model: Model | None,
        servers: Iterable[str] | None = None,
        *,
        again: bool = False,
        include_owner: bool = False,
    ) -> dict[str, Any]:
        """Sort tools of `servers` (all when None) into domains with one call of `model`.
        Without `again`: only tools that have no domain. With `again` (the owner's re-sort):
        also the model's placements, and the owner's with `include_owner`. Domains the model
        made that end up empty are deleted unless a job uses them. Returns {"sorted": n,
        "error": text or None}."""
        servers = None if servers is None else list(servers)
        async with self._sorting:
            tools = await self.tools(servers)
            targets = [
                t
                for t in tools
                if t["domain_id"] is None or (again and (include_owner or t["set_by"] == "model"))
            ]
            if not targets:
                return {"sorted": 0, "error": None}
            if model is None:
                return await self._failed(targets, "the main model is not configured")
            try:
                out = await self._ask(model, targets, await self.tools(), await self.domains())
            except Exception as exc:
                return await self._failed(targets, f"{type(exc).__name__}: {exc}")
            await self._apply(out, targets)
            return {"sorted": len(targets), "error": None}

    async def _failed(self, targets: list[dict[str, Any]], why: str) -> dict[str, Any]:
        reason = f"sorting failed: {why}"[:500]
        await self.db.executemany(
            "UPDATE mcp_tools SET sort_error = ? WHERE server = ? AND tool = ?",
            [(reason, t["server"], t["tool"]) for t in targets if t["domain_id"] is None],
        )
        return {"sorted": 0, "error": reason}

    async def _ask(
        self,
        model: Model,
        targets: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        domains: list[dict[str, Any]],
    ) -> list[DomainOut]:
        target_names = {full_name(t["server"], t["tool"]) for t in targets}
        by_domain: dict[int, list[str]] = {}
        for t in tools:
            name = full_name(t["server"], t["tool"])
            if t["domain_id"] is not None and name not in target_names:
                by_domain.setdefault(t["domain_id"], []).append(name)
        existing = {d["name"] for d in domains}
        prompt = "Existing domains:\n" + (
            "\n".join(
                f"- {d['name']}: {d['description']}"
                + (f" (tools: {', '.join(by_domain[d['id']])})" if d["id"] in by_domain else "")
                for d in domains
            )
            or "(none yet)"
        )
        prompt += "\n\nTools to sort:\n" + "\n".join(
            f"- {full_name(t['server'], t['tool'])}: {t['description'] or '(no description)'}"
            for t in targets
        )

        def check(out: list[DomainOut]) -> list[DomainOut]:
            placed = [n for d in out for n in d.tools]
            problems = []
            missing = sorted(target_names - set(placed))
            if missing:
                problems.append(f"not placed: {', '.join(missing)}")
            twice = sorted({n for n in placed if placed.count(n) > 1})
            if twice:
                problems.append(f"placed more than once: {', '.join(twice)}")
            unknown = sorted(set(placed) - target_names)
            if unknown:
                problems.append(f"not among the tools to sort: {', '.join(unknown)}")
            for d in out:
                if d.name not in existing and not NAME.match(d.name):
                    problems.append(f"bad domain name {d.name!r} (lowercase, '-', max 24)")
                if d.name not in existing and not d.description.strip():
                    problems.append(f"new domain {d.name!r} needs a description")
            if problems:
                raise ModelRetry("; ".join(problems))
            return out

        agent: Agent[None, list[DomainOut]] = Agent(
            model, instructions=SORT_PROMPT, output_type=list[DomainOut]
        )
        agent.output_validator(check)
        with trace_scope("mcp_sort"):
            result = await agent.run(prompt)
        return result.output

    async def _apply(self, out: list[DomainOut], targets: list[dict[str, Any]]) -> None:
        now = time.time()
        split = {full_name(t["server"], t["tool"]): (t["server"], t["tool"]) for t in targets}
        in_use = set(await self.used_by())
        async with self.db.transaction() as conn:
            async with conn.execute("SELECT id, name FROM mcp_domains") as cur:
                existing = {r["name"]: r["id"] for r in await cur.fetchall()}
            for d in out:
                domain_id = existing.get(d.name)
                if domain_id is None:
                    cur = await conn.execute(
                        "INSERT INTO mcp_domains (name, description, set_by, updated_ts) "
                        "VALUES (?, ?, 'model', ?)",
                        (d.name, d.description.strip(), now),
                    )
                    domain_id = existing[d.name] = cur.lastrowid
                await conn.executemany(
                    "UPDATE mcp_tools SET domain_id = ?, set_by = 'model', sort_error = NULL "
                    "WHERE server = ? AND tool = ?",
                    [(domain_id, *split[name]) for name in d.tools],
                )
            keep = f" AND id NOT IN ({','.join(str(int(i)) for i in in_use)})" if in_use else ""
            await conn.execute(
                f"DELETE FROM mcp_domains WHERE set_by = 'model'{keep} AND NOT EXISTS "
                "(SELECT 1 FROM mcp_tools t WHERE t.domain_id = mcp_domains.id)"
            )

    # ------------------------------------------------------------------ the owner's edits

    async def edit_domain(self, domain_id: int, name: str, description: str) -> None:
        name = name.strip()
        if not NAME.match(name):
            raise DomainError(NAME_RULE)
        clash = await self.db.fetchone(
            "SELECT 1 FROM mcp_domains WHERE name = ? AND id != ?", (name, domain_id)
        )
        if clash:
            raise DomainError(f"there is already a domain named {name!r}")
        changed = await self.db.write(
            "UPDATE mcp_domains SET name = ?, description = ?, set_by = 'owner', "
            "updated_ts = ? WHERE id = ?",
            (name, description.strip(), time.time(), domain_id),
        )
        if not changed:
            raise DomainError("no such domain")

    async def new_domain(self, name: str, description: str) -> int:
        name = name.strip()
        if not NAME.match(name):
            raise DomainError(NAME_RULE)
        if await self.db.fetchone("SELECT 1 FROM mcp_domains WHERE name = ?", (name,)):
            raise DomainError(f"there is already a domain named {name!r}")
        return await self.db.execute(
            "INSERT INTO mcp_domains (name, description, set_by, updated_ts) "
            "VALUES (?, ?, 'owner', ?)",
            (name, description.strip(), time.time()),
        )

    async def move_tool(self, server: str, tool: str, domain_id: int) -> None:
        if not await self.db.fetchone("SELECT 1 FROM mcp_domains WHERE id = ?", (domain_id,)):
            raise DomainError("no such domain")
        changed = await self.db.write(
            "UPDATE mcp_tools SET domain_id = ?, set_by = 'owner', sort_error = NULL "
            "WHERE server = ? AND tool = ?",
            (domain_id, server, tool),
        )
        if not changed:
            raise DomainError(f"no tool {tool!r} of server {server!r}")

    async def delete_domain(self, domain_id: int) -> None:
        if not await self.db.fetchone("SELECT 1 FROM mcp_domains WHERE id = ?", (domain_id,)):
            raise DomainError("no such domain")
        jobs = (await self.used_by()).get(domain_id)
        if jobs:
            raise DomainError(f"used by: {', '.join(jobs)}; remove it from those jobs first")
        # Its tools lose their domain (ON DELETE SET NULL) and are sorted at the next listing.
        await self.db.write("DELETE FROM mcp_domains WHERE id = ?", (domain_id,))
