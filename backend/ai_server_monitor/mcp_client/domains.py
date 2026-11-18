"""MCP tool domains: where they are stored (`mcp_domains`, `mcp_tools`) and how tools are
sorted into them (one call of the main model).

A domain is an area a person asks about (an application, a device, a part of the host), not a
server. Six built-in domains exist from the start (migration 0020, `key` set). Every tool of a
configured server is recorded as it was last listed; a tool without a domain is sorted as soon
as a listing shows it (`sort_tools`), and again when the owner asks. The owner's own placements
and domain texts (`set_by='owner'`) are kept by a re-sort unless the owner asks to redo them.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models import Model

from ..db import Database
from .toolsets import SEPARATOR

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")

SORT_PROMPT = """\
You sort the tools of MCP servers into domains. A domain is one area a person would ask about \
(one application, device or service), not a server and not a verb: reading and changing the same \
thing belong to one domain. Put every tool you are given into exactly one domain: an existing one \
when it fits, otherwise a new one. The built-in domains (storage, journal, resources, docker, \
systemd, updates) are about this host; put a tool there only when it is about the same thing. \
New domain names: lowercase words joined by '-', at most 24 characters. Descriptions: one line \
saying what a person can ask about there. Return only the domains you put tools into, each with \
the full names of its tools as given."""


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


class DomainStore:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------ reading

    async def domains(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT d.id, d.key, d.name, d.description, d.set_by, COUNT(t.tool) AS tools "
            "FROM mcp_domains d LEFT JOIN mcp_tools t ON t.domain_id = d.id "
            "GROUP BY d.id ORDER BY d.key IS NULL, d.name"
        )
        return [dict(r) for r in rows]

    async def tools(self, servers: Iterable[str] | None = None) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT t.server, t.tool, t.description, t.read_only, t.domain_id, t.set_by, "
            "t.sort_error, t.seen_ts, d.name AS domain FROM mcp_tools t "
            "LEFT JOIN mcp_domains d ON d.id = t.domain_id ORDER BY t.server, t.tool"
        )
        wanted = None if servers is None else set(servers)
        return [dict(r) for r in rows if wanted is None or r["server"] in wanted]

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
        rows = await self.db.fetchall(
            "SELECT tool FROM mcp_tools WHERE server = ? AND domain_id IS NULL", (server,)
        )
        return [r["tool"] for r in rows]

    async def forget_servers(self, keep: Iterable[str]) -> None:
        """Drop the tools of servers that are no longer configured."""
        names = list(keep)
        gone = f" WHERE server NOT IN ({','.join('?' * len(names))})" if names else ""
        await self.db.write(f"DELETE FROM mcp_tools{gone}", names)

    # ------------------------------------------------------------------ sorting

    async def sort_tools(
        self,
        model: Model,
        servers: Iterable[str] | None = None,
        *,
        again: bool = False,
        include_owner: bool = False,
        in_use: Iterable[int] = (),
    ) -> dict[str, Any]:
        """Sort tools into domains with one call of `model`. Without `again`: only tools that
        have no domain. With `again` (the owner's re-sort): also the model's placements, and the
        owner's with `include_owner`. Domains the model made that end up empty are deleted,
        unless `in_use` (jobs use them). Returns {"sorted": n, "error": text or None}."""
        tools = await self.tools(servers)
        targets = [
            t
            for t in tools
            if t["domain_id"] is None or (again and (include_owner or t["set_by"] == "model"))
        ]
        if not targets:
            return {"sorted": 0, "error": None}
        domains = await self.domains()
        by_domain: dict[int, list[str]] = {}
        target_names = {full_name(t["server"], t["tool"]) for t in targets}
        for t in tools:
            name = full_name(t["server"], t["tool"])
            if t["domain_id"] is not None and name not in target_names:
                by_domain.setdefault(t["domain_id"], []).append(name)
        existing = {d["name"]: d for d in domains}
        prompt = "Existing domains:\n" + "\n".join(
            f"- {d['name']}: {d['description']}"
            + (f" (tools: {', '.join(by_domain[d['id']])})" if d["id"] in by_domain else "")
            for d in domains
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
        try:
            result = await agent.run(prompt)
        except Exception as exc:
            reason = f"sorting failed: {type(exc).__name__}: {exc}"[:500]
            await self.db.executemany(
                "UPDATE mcp_tools SET sort_error = ? WHERE server = ? AND tool = ?",
                [(reason, t["server"], t["tool"]) for t in targets if t["domain_id"] is None],
            )
            return {"sorted": 0, "error": reason}
        await self._apply(result.output, targets, existing, set(in_use))
        return {"sorted": len(targets), "error": None}

    async def _apply(
        self,
        out: list[DomainOut],
        targets: list[dict[str, Any]],
        existing: dict[str, dict[str, Any]],
        in_use: set[int],
    ) -> None:
        now = time.time()
        split = {full_name(t["server"], t["tool"]): (t["server"], t["tool"]) for t in targets}
        async with self.db.transaction() as conn:
            for d in out:
                if d.name in existing:
                    domain_id = existing[d.name]["id"]
                else:
                    cur = await conn.execute(
                        "INSERT INTO mcp_domains (name, description, set_by, updated_ts) "
                        "VALUES (?, ?, 'model', ?)",
                        (d.name, d.description.strip(), now),
                    )
                    domain_id = cur.lastrowid
                await conn.executemany(
                    "UPDATE mcp_tools SET domain_id = ?, set_by = 'model', sort_error = NULL "
                    "WHERE server = ? AND tool = ?",
                    [(domain_id, *split[name]) for name in d.tools],
                )
            keep = f" AND id NOT IN ({','.join(str(int(i)) for i in in_use)})" if in_use else ""
            await conn.execute(
                "DELETE FROM mcp_domains WHERE set_by = 'model' AND key IS NULL"
                f"{keep} AND NOT EXISTS "
                "(SELECT 1 FROM mcp_tools t WHERE t.domain_id = mcp_domains.id)"
            )

    # ------------------------------------------------------------------ the owner's edits

    async def edit_domain(self, domain_id: int, name: str, description: str) -> None:
        name = name.strip()
        if not NAME.match(name):
            raise DomainError("a domain name is lowercase letters, digits and '-' (up to 24)")
        try:
            changed = await self.db.write(
                "UPDATE mcp_domains SET name = ?, description = ?, set_by = 'owner', "
                "updated_ts = ? WHERE id = ?",
                (name, description.strip(), time.time(), domain_id),
            )
        except Exception as exc:  # the UNIQUE constraint
            raise DomainError(f"there is already a domain named {name!r}") from exc
        if not changed:
            raise DomainError("no such domain")

    async def new_domain(self, name: str, description: str) -> int:
        name = name.strip()
        if not NAME.match(name):
            raise DomainError("a domain name is lowercase letters, digits and '-' (up to 24)")
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

    async def delete_domain(self, domain_id: int, used_by: list[str]) -> None:
        """`used_by`: the jobs that have this domain (the delete is refused then)."""
        row = await self.db.fetchone("SELECT key FROM mcp_domains WHERE id = ?", (domain_id,))
        if row is None:
            raise DomainError("no such domain")
        if row["key"] is not None:
            raise DomainError("a built-in domain cannot be deleted")
        if used_by:
            raise DomainError(f"used by: {', '.join(used_by)}; remove it from those jobs first")
        if await self.db.fetchone("SELECT 1 FROM mcp_tools WHERE domain_id = ?", (domain_id,)):
            raise DomainError("move its tools to other domains first")
        await self.db.write("DELETE FROM mcp_domains WHERE id = ?", (domain_id,))
