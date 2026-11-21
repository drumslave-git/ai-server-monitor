"""HTTP API of the MCP tools page: every stored tool with its domain, on/off and kind; the
owner's domain edits; the manual re-sort. On/off writes `tool_filter` in the settings, the
same value the Settings page's tools table edits."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from .domains import DomainError, ListedTool
from .toolsets import kind_of

if TYPE_CHECKING:
    from ..context import AppContext

router = APIRouter()


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


class McpServerState(BaseModel):
    name: str
    enabled: bool
    listed_ts: float | None = Field(description="Last listing attempt (None: never listed).")
    error: str | None = Field(description="Why the last listing failed (None: it worked).")


class McpDomainJob(BaseModel):
    slug: str
    name: str


class McpDomain(BaseModel):
    id: int
    name: str
    description: str
    set_by: Literal["model", "owner"]
    tools: int
    used_by: list[McpDomainJob] = Field(description="The jobs that have this domain.")


class McpTool(BaseModel):
    server: str
    tool: str
    name: str = Field(description="What the model sees: server_tool.")
    description: str
    read_only: bool | None = Field(description="The server's readOnlyHint.")
    kind: Literal["read", "write"] = Field(description="Effective, after the owner's override.")
    enabled: bool = Field(description="Chosen for the agents (the server's tool_filter).")
    domain_id: int | None
    domain: str | None
    set_by: Literal["model", "owner"]
    sort_error: str | None
    seen_ts: float


class McpToolsPage(BaseModel):
    sorting: bool = Field(description="A sorting into domains is running right now.")
    servers: list[McpServerState]
    domains: list[McpDomain]
    tools: list[McpTool]


class SortRequest(BaseModel):
    server: str | None = Field(default=None, description="One server; None = all enabled.")
    include_owner: bool = Field(
        default=False, description="Also re-sort the tools the owner placed by hand."
    )


class SortResult(BaseModel):
    sorted: int
    error: str | None
    unreachable: dict[str, str]


class DomainEdit(BaseModel):
    name: str
    description: str = ""


class ToolRef(BaseModel):
    server: str
    tool: str


class ToolSwitch(BaseModel):
    tools: list[ToolRef] = Field(min_length=1)
    enabled: bool


class ToolEdit(BaseModel):
    domain_id: int | None = Field(default=None, description="Move the tool to this domain.")
    enabled: bool | None = Field(default=None, description="Switch the tool on or off.")


async def page(ctx: AppContext) -> McpToolsPage:
    configured = {s.name: s for s in ctx.settings.mcp.servers}
    listings = await ctx.domains.listings()
    tools = []
    for t in await ctx.domains.tools(configured):
        cfg = configured[t["server"]]
        hint = None if t["read_only"] is None else bool(t["read_only"])
        tools.append(
            McpTool(
                **{**t, "read_only": hint},
                name=f"{t['server']}_{t['tool']}",
                kind=kind_of(cfg, t["tool"], {"readOnlyHint": hint}),
                enabled=t["tool"] in cfg.tool_filter,
            )
        )
    return McpToolsPage(
        sorting=ctx.domains.sorting,
        servers=[
            McpServerState(
                name=s.name,
                enabled=s.enabled,
                listed_ts=listings.get(s.name, {}).get("ts"),
                error=listings.get(s.name, {}).get("error"),
            )
            for s in configured.values()
        ],
        domains=[McpDomain.model_validate(d) for d in await ctx.domains.domains()],
        tools=tools,
    )


@router.get("/api/mcp/tools")
async def get_tools(request: Request) -> McpToolsPage:
    """Every tool of the configured servers as last listed, with its domain; the domains;
    each server's state (last listing and why it failed)."""
    return await page(_ctx(request))


@router.post("/api/mcp/sort")
async def sort(request: Request, body: SortRequest) -> SortResult:
    """List the server (or all enabled servers) live and sort its tools into domains again
    with the main model. The owner's placements stay unless `include_owner`; domain names
    and descriptions the owner wrote always stay."""
    ctx = _ctx(request)
    servers = [s for s in ctx.settings.mcp.servers if s.enabled]
    if body.server is not None:
        servers = [s for s in servers if s.name == body.server]
        if not servers:
            raise HTTPException(404, f"no enabled MCP server {body.server!r}")
    result = await ctx.domains.refresh(
        servers, ctx.sort_model(), again=True, include_owner=body.include_owner
    )
    return SortResult.model_validate(result)


@router.post("/api/mcp/domains", status_code=201)
async def create_domain(request: Request, body: DomainEdit) -> McpDomain:
    ctx = _ctx(request)
    try:
        domain_id = await ctx.domains.new_domain(body.name, body.description)
    except DomainError as exc:
        raise HTTPException(409, str(exc)) from None
    return await _domain(ctx, domain_id)


@router.put("/api/mcp/domains/{domain_id}")
async def edit_domain(request: Request, domain_id: int, body: DomainEdit) -> McpDomain:
    """Rename or describe a domain (kept by later re-sorts)."""
    ctx = _ctx(request)
    try:
        await ctx.domains.edit_domain(domain_id, body.name, body.description)
    except DomainError as exc:
        raise HTTPException(409, str(exc)) from None
    return await _domain(ctx, domain_id)


@router.delete("/api/mcp/domains/{domain_id}", status_code=204)
async def delete_domain(request: Request, domain_id: int) -> None:
    """Refused while a job uses the domain (names the jobs). Its tools lose their domain and
    are sorted at the next listing."""
    try:
        await _ctx(request).domains.delete_domain(domain_id)
    except DomainError as exc:
        raise HTTPException(409, str(exc)) from None


@router.patch("/api/mcp/tools/{server}/{tool}")
async def edit_tool(request: Request, server: str, tool: str, body: ToolEdit) -> McpTool:
    """Move a tool to a domain (kept by later re-sorts) and/or switch it on or off."""
    ctx = _ctx(request)
    if body.domain_id is not None:
        try:
            await ctx.domains.move_tool(server, tool, body.domain_id)
        except DomainError as exc:
            raise HTTPException(409, str(exc)) from None
    if body.enabled is not None:
        await _switch(ctx, server, tool, body.enabled)
    found = [t for t in (await page(ctx)).tools if t.server == server and t.tool == tool]
    if not found:
        raise HTTPException(404, f"no tool {tool!r} of server {server!r}")
    return found[0]


@router.post("/api/mcp/tools/switch")
async def switch_tools(request: Request, body: ToolSwitch) -> McpToolsPage:
    """Switch several tools on or off in one settings write (the page's Select all /
    Deselect all). Returns the page data."""
    ctx = _ctx(request)
    await _switch_many(ctx, [(t.server, t.tool) for t in body.tools], body.enabled)
    return await page(ctx)


async def _switch(ctx: AppContext, server: str, tool: str, on: bool) -> None:
    await _switch_many(ctx, [(server, tool)], on)


async def _switch_many(ctx: AppContext, tools: list[tuple[str, str]], on: bool) -> None:
    data = ctx.settings.storage_dict()
    entries = {e["name"]: e for e in data["mcp"]["servers"]}
    for server in {s for s, _ in tools}:
        if server not in entries:
            raise HTTPException(404, f"no MCP server {server!r}")
        names = {t for s, t in tools if s == server}
        chosen = [t for t in entries[server].get("tool_filter") or [] if t not in names]
        entries[server]["tool_filter"] = [*chosen, *sorted(names)] if on else chosen
    try:
        new = Settings.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from None
    await ctx.apply_settings(new)


async def _domain(ctx: AppContext, domain_id: int) -> McpDomain:
    for d in await ctx.domains.domains():
        if d["id"] == domain_id:
            return McpDomain.model_validate(d)
    raise HTTPException(404, f"no domain {domain_id}")


async def sort_tested(ctx: AppContext, body: dict[str, Any], details: dict[str, Any]) -> str:
    """After a working Settings "Test": record the tools it listed and sort the new ones
    (the test waits for it). Returns a sentence for the test result."""
    server = str(body.get("name") or "")
    tools = [
        ListedTool(t["name"], t.get("description") or "", t.get("read_only"))
        for t in details.get("tools", [])
    ]
    unsorted = await ctx.domains.record_listing(server, tools)
    if not unsorted:
        return "All its tools have a domain."
    result = await ctx.domains.sort_tools(ctx.sort_model(), [server])
    if result["error"]:
        return f"Sorting its tools into domains failed ({result['error']})."
    placed = {t["domain"] for t in await ctx.domains.tools([server]) if t["domain"]}
    return f"{result['sorted']} new tools sorted into domains: {', '.join(sorted(placed))}."
