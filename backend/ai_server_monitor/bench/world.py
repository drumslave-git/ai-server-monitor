"""The frozen world a scenario runs in: collector data from a snapshot, tool answers from rules.

Nothing here talks to a real service. The product code runs unchanged (report engine, chat
engine with its prompts and tool schemas, the action layer, the API gateway, the Prometheus
planner); what changes is what sits under it:

- `FakeCollectors`      the collector manager, holding the snapshot's data
- `world_transport`     an httpx transport that answers every *arr / qBittorrent / Prometheus /
                        Docker request from the scenario's `api` rules
- `prom_transport`      a fake Prometheus built from a `prom_plan` catalog
- `FakeMcp`             the MCP manager, with the scenario's MCP tools
- `swap_executors`      replaces the executors of `run_command` and `disk_usage`
- `BenchApp`            the object the chat engine takes for the application context
"""

from __future__ import annotations

import asyncio
import copy
import json
import posixpath
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..actions import ActionService
from ..apiaccess import ApiGateway
from ..chat import ChatEngine
from ..chat.host_tools import RESULT_SHARE, DiskUsageArgs, RunCommandArgs
from ..chat.tools import Tool, ToolContext, ToolError, build_tools
from ..collectors.base import CollectorResult, CollectorUnavailable, HostEnv
from ..config import (
    ChatSettings,
    LLMSettings,
    Settings,
)
from ..db import Database
from ..digest import WindowObserver
from ..events import EventBus
from ..hostinspect import disk
from ..hostinspect.commands import PathGuard, Refused, parse_pipeline
from ..hostinspect.output import human, scrub_output
from ..llm import LLMClient
from ..mcp_client import McpManager
from ..mcp_client.connection import CallOutcome, McpCallError, ToolInfo
from ..report.engine import ReportEngine
from .schema import (
    ApiRule,
    DiskNode,
    PromCatalog,
    Scenario,
    Snapshot,
    World,
    merge_patch,
)

ARR_ALIASES = {
    "movies": ("radarr",),
    "series": ("sonarr",),
    "all_arr": ("radarr", "sonarr", "whisparr"),
}
HOST_SUFFIX = ".bench"
_TIME_KEYS = {
    "since",
    "ts",
    "last_ts",
    "first_ts",
    "window_start",
    "window_end",
    "created",
    "updated",
    "expires",
    "last_seen",
}
_ISO_KEYS = {"started_at", "created_at", "finished_at", "created_iso"}


# --------------------------------------------------------------------------- settings


def _service_kind(name: str) -> str | None:
    for kind in ("radarr", "sonarr", "whisparr", "prowlarr", "qbittorrent"):
        if name == kind or name.startswith(kind + "-"):
            return kind
    return None


def services_in(world: World) -> set[str]:
    """Concrete service names the `api` rules name (aliases excluded)."""
    return {r.service for r in world.api if r.service not in ARR_ALIASES}


def bench_settings(
    llm: LLMSettings,
    world: World,
    patches: list[dict[str, Any]] | None = None,
    chat: ChatSettings | None = None,
) -> Settings:
    """The Settings profile every benchmark chat runs under: defaults, the LLM of the app, the
    chat settings of the app (routing, tool timeout, ...; defaults when not given) and a fake
    Radarr, Sonarr, Prowlarr, qBittorrent, Prometheus, Docker and shell so that every tool
    exists; every write action needs approval and nothing is ever sent (dry-run). Scenario and
    snapshot patches come last and win."""
    names = {"radarr", "sonarr", "prowlarr", "qbittorrent"} | services_in(world)
    arr: dict[str, Any] = {"dry_run": True, "radarr": [], "sonarr": [], "whisparr": []}
    for name in sorted(names):
        kind = _service_kind(name)
        url = f"http://{name}{HOST_SUFFIX}:7878"
        if kind in ("radarr", "sonarr", "whisparr"):
            arr[kind].append({"name": name, "url": url, "api_key": "bench-key"})
        elif kind == "prowlarr":
            arr["prowlarr"] = {"name": name, "url": url, "api_key": "bench-key"}
        elif kind == "qbittorrent":
            arr["qbittorrent"] = {"url": url, "username": "bench", "password": "bench"}
    arr["actions"] = {
        key: {"policy": "approve"}
        for key in (
            "stalled_cleanup",
            "missing_search",
            "cutoff_search",
            "add_media",
            "indexer_triage",
            "api_call",
        )
    }
    servers = []
    for server in sorted({r.server for r in world.mcp}):
        servers.append(
            {
                "name": server,
                "url": f"http://{server}{HOST_SUFFIX}/mcp",
                "tool_filter": [r.tool for r in world.mcp if r.server == server],
                "default_policy": "approve",
            }
        )
    base = Settings().storage_dict()
    base["llm"] = llm.model_dump(mode="json", context={"reveal_secrets": True})
    if chat is not None:
        base["chat"] = chat.model_dump(mode="json")
    base["chat"]["shell"] = True  # the fake world has a shell: its tool must exist
    base["arr"] = merge_patch(base["arr"], arr)
    base["collectors"]["prometheus"]["url"] = f"http://prometheus{HOST_SUFFIX}:9090"
    base["collectors"]["docker"]["enabled"] = True
    base["mcp"] = {"servers": servers, "dry_run": True}
    for patch in patches or []:
        if patch:
            base = merge_patch(base, patch)
    return Settings.model_validate(base)


# --------------------------------------------------------------------------- time handling


def rebase_times(data: Any, shift: float) -> Any:
    """Move the timestamps inside collector data by `shift` seconds (a snapshot is frozen at
    its own `now`; the digest and the tools measure ages against the real clock)."""
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if key in _TIME_KEYS and isinstance(value, int | float) and not isinstance(value, bool):
                out[key] = value + shift
            elif key in _ISO_KEYS and isinstance(value, str):
                out[key] = _shift_iso(value, shift)
            else:
                out[key] = rebase_times(value, shift)
        return out
    if isinstance(data, list):
        return [rebase_times(v, shift) for v in data]
    return data


def _shift_iso(text: str, shift: float) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z$", text)
    if not match:
        return text
    try:
        moved = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return text
    return datetime.fromtimestamp(moved.timestamp() + shift, UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z"
    )


# --------------------------------------------------------------------------- collectors


class FakeDockerApi:
    """What `container_inspect` reaches through the docker collector: answers from the `api`
    rules of service `docker`."""

    def __init__(self, world: World):
        self.world = world

    async def get(self, path: str, **params: Any) -> Any:
        for rule in self.world.api:
            if rule.service == "docker" and rule.method == "GET" and re.search(rule.path, path):
                if rule.status >= 400:
                    raise CollectorUnavailable(f"HTTP {rule.status}")
                raw = rule.body()
                return raw if raw is not None else copy.deepcopy(rule.response)
        raise CollectorUnavailable(self.world.default)


@dataclass
class _FakeDocker:
    api: FakeDockerApi


class FakeCollectors:
    """The slice of `CollectorManager` the digest and the tools use."""

    def __init__(self, snapshot: Snapshot, world: World, *, now: float | None = None):
        real_now = time.time() if now is None else now
        shift = 0.0 if snapshot.now is None else real_now - snapshot.now
        self.registry: dict[str, Any] = {
            name: None
            for name in (
                "system",
                "docker",
                "gpu",
                "systemd",
                "journal",
                "apt",
                "image_updates",
                "prometheus",
                "arr",
            )
        }
        for name in snapshot.collectors:
            self.registry.setdefault(name, None)
        self.collectors: dict[str, Any] = {"docker": _FakeDocker(FakeDockerApi(world))}
        self.latest: dict[str, CollectorResult] = {}
        self._status: dict[str, dict[str, Any]] = {}
        for name in self.registry:
            col = snapshot.collectors.get(name)
            if col is None:
                self._status[name] = {
                    "state": "disabled",
                    "reason": "not in this snapshot",
                    "last_ts": None,
                    "ok": None,
                    "error": None,
                    "duration_s": None,
                }
                continue
            ts = real_now - col.age_s
            if col.state in ("ok", "active") and col.ok:
                state = "active"
            else:
                state = col.state if col.state != "ok" else "failing"
            data = rebase_times(col.data, shift) if shift else copy.deepcopy(col.data)
            self.latest[name] = CollectorResult(
                name=name,
                ts=ts,
                ok=col.ok,
                data=data,
                error=col.error,
                unavailable=col.state == "unavailable",
                duration_s=0.05,
            )
            self._status[name] = {
                "state": state,
                "reason": col.reason,
                "last_ts": ts,
                "ok": col.ok,
                "error": col.error,
                "duration_s": 0.05,
            }

    def status(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._status)

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- HTTP world


def _rule_matches(rule: ApiRule, host: str, method: str, target: str) -> bool:
    if rule.method != method:
        return False
    service = rule.service
    if service in ARR_ALIASES:
        if not any(host == k or host.startswith(k + "-") for k in ARR_ALIASES[service]):
            return False
    elif service != host:
        return False
    return re.search(rule.path, target) is not None


def world_transport(world: World) -> httpx.MockTransport:
    """Answers every HTTP request of the gateway, the *arr clients and the PromQL client from
    the scenario's `api` rules. The host (`radarr.bench`) names the service."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host.split(".")[0]
        path = request.url.path
        target = path + (f"?{request.url.query.decode()}" if request.url.query else "")
        if host == "qbittorrent" and path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=bench; Path=/"})
        for rule in world.api:
            if _rule_matches(rule, host, request.method, target):
                raw = rule.body()
                if raw is not None:
                    return httpx.Response(rule.status, content=raw)
                return httpx.Response(rule.status, json=copy.deepcopy(rule.response))
        return httpx.Response(404, text=world.default)

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- fake Prometheus

_AGGREGATE = re.compile(r"\b(sum|max|min|avg|count|topk|bottomk|quantile|group)\b", re.I)
_TOKEN = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_JOB = re.compile(r"job\s*=~?\s*\"([^\"]*)\"")
_BARE_RANGE = re.compile(r"^\s*[A-Za-z_:][A-Za-z0-9_:]*\s*(\{[^}]*\})?\s*\[[^\]]+\]\s*$")


def _balanced(query: str) -> bool:
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    in_str = False
    for ch in query:
        if ch == '"':
            in_str = not in_str
        elif in_str:
            continue
        elif ch in "([{":
            stack.append(ch)
        elif ch in ")]}" and (not stack or stack.pop() != pairs[ch]):
            return False
    return not stack and not in_str


def prom_transport(catalog: PromCatalog) -> httpx.MockTransport:
    """A Prometheus built from the catalog: targets, metadata, the per-job metric listing the
    planner reads, and instant queries (see `PromCatalog`)."""
    jobs = {j.job: j for j in catalog.jobs}

    def ok(data: Any) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": data})

    def vector(series: list[tuple[dict[str, str], float]]) -> httpx.Response:
        return ok(
            {
                "resultType": "vector",
                "result": [{"metric": m, "value": [time.time(), repr(v)]} for m, v in series],
            }
        )

    def error(message: str) -> httpx.Response:
        return httpx.Response(
            400, json={"status": "error", "errorType": "bad_data", "error": message}
        )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        if path == "/api/v1/targets":
            return ok(
                {
                    "activeTargets": [
                        {
                            "labels": {"job": j.job, "instance": t.instance},
                            "scrapeUrl": f"http://{t.instance}/metrics",
                            "health": t.health,
                        }
                        for j in catalog.jobs
                        for t in j.targets
                    ]
                }
            )
        if path == "/api/v1/metadata":
            meta: dict[str, list[dict[str, str]]] = {}
            for j in catalog.jobs:
                for name, m in j.metrics.items():
                    meta.setdefault(name, [{"type": m.type, "help": m.help, "unit": ""}])
            return ok(meta)
        if path != "/api/v1/query":
            return httpx.Response(404, json={"status": "error", "error": "not found"})
        query = params.get("query", "")
        count = re.match(r'^\s*count by \(__name__\) \(\{job="([^"]*)"\}\)\s*$', query)
        if count:
            job = jobs.get(count.group(1).replace('\\"', '"'))
            if job is None:
                return vector([])
            return vector([({"__name__": n}, float(m.series)) for n, m in job.metrics.items()])
        for answer in catalog.queries:
            if re.search(answer.match, query):
                if answer.error:
                    return error(answer.error)
                return vector([({"instance": f"i{i}"}, v) for i, v in enumerate(answer.values)])
        if not query.strip() or not _balanced(query):
            return error(f"1:1: parse error: unexpected end of input in {query[:40]!r}")
        scoped = [j for j in jobs.values() if any(j.job in m for m in _JOB.findall(query))]
        pool = scoped or list(jobs.values())
        found = None
        for token in _TOKEN.findall(query):
            for j in pool:
                if token in j.metrics:
                    found = j.metrics[token]
                    break
            if found:
                break
        if found is None:
            return vector([])
        if _BARE_RANGE.match(query):
            return ok({"resultType": "matrix", "result": []})
        if found.value is None:
            return vector([])
        count_series = 1 if _AGGREGATE.search(query) else found.series
        return vector([({"instance": f"i{i}"}, found.value) for i in range(count_series)])

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- MCP


class FakeMcp(McpManager):
    """The MCP manager with the scenario's tools. Nothing connects anywhere."""

    def __init__(self, settings: Settings, world: World):
        super().__init__(settings.mcp)
        self.world = world
        self.calls: list[dict[str, Any]] = []

    async def prepare(self, wait: float = 0) -> None:
        return None

    def tools_of(self, name: str) -> list[ToolInfo] | None:
        rules = [r for r in self.world.mcp if r.server == name]
        if not rules:
            return None
        seen: dict[str, ToolInfo] = {}
        for r in rules:
            seen.setdefault(
                r.tool,
                ToolInfo(
                    name=r.tool,
                    description=r.description,
                    input_schema=copy.deepcopy(r.input_schema),
                    read_only=not r.write,
                ),
            )
        return list(seen.values())

    def unavailable(self) -> list[str]:
        return []

    async def call(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        *,
        source: str | None = None,
        timeout: float | None = None,
    ) -> CallOutcome:
        self.calls.append({"server": server, "tool": tool, "arguments": arguments})
        why = self.allowed(server, tool)
        if why:
            raise McpCallError(why)
        for rule in self.world.mcp:
            if rule.server != server or rule.tool != tool:
                continue
            if all(
                re.search(pattern, str(arguments.get(key, "")), re.I)
                for key, pattern in rule.match.items()
            ):
                if rule.error:
                    return CallOutcome(is_error=True, text=rule.error)
                text = (
                    rule.result
                    if isinstance(rule.result, str)
                    else json.dumps(rule.result, ensure_ascii=False)
                )
                return CallOutcome(is_error=False, text=text, structured=None)
        raise McpCallError(self.world.default)


# --------------------------------------------------------------------------- tool executors


def _find_node(roots: list[DiskNode], path: str) -> tuple[DiskNode, str] | None:
    """The node for `path` and its absolute path, descending from a root."""
    for root in roots:
        base = posixpath.normpath(root.path or "/")
        if path == base:
            return root, base
        if path.startswith(base.rstrip("/") + "/"):
            node, current = root, base
            for part in path[len(base) :].strip("/").split("/"):
                nxt = next((c for c in node.children if c.name == part), None)
                if nxt is None:
                    return None
                node, current = nxt, f"{current.rstrip('/')}/{part}"
            return node, current
    return None


def _walk(node: DiskNode, path: str, depth: int, out: list[tuple[str, int, str | None]]) -> None:
    if depth <= 0:
        return
    for child in node.children:
        child_path = f"{path.rstrip('/')}/{child.name}"
        out.append((child_path, child.size_bytes, child.owner))
        _walk(child, child_path, depth - 1, out)


def fake_disk_usage(world: World) -> Any:
    async def handler(ctx: ToolContext, args: DiskUsageArgs) -> Any:
        path = posixpath.normpath(args.path.strip()) if args.path.strip() else "/"
        if not path.startswith("/"):
            raise ToolError("path must be absolute, e.g. /mnt/disks/disk14TB")
        if why := PathGuard().reason(path):
            raise ToolError(f"refused: {why}")
        disk_world = world.disk
        if disk_world is None:
            raise ToolError(world.default)
        if args.top_files:
            files = sorted(
                (
                    f
                    for f in disk_world.files
                    if f.path.startswith(path.rstrip("/") + "/") or f.path == path
                ),
                key=lambda f: f.size_bytes,
                reverse=True,
            )
            return {
                "path": path,
                "min_size": "100 MB",
                "files": [
                    {
                        "path": f.path,
                        "size": human(f.size_bytes),
                        **({"owner": [f.owner]} if f.owner else {}),
                    }
                    for f in files[: args.top_files]
                ],
                "found": len(files),
            }
        hit = _find_node(disk_world.roots, path)
        if hit is None:
            raise ToolError(f"cannot read {path}: No such file or directory")
        node, base = hit
        rows: list[tuple[str, int, str | None]] = []
        _walk(node, base, args.depth, rows)
        owners = {p: o for p, _, o in rows}
        scan = disk.Scan(
            entries=[disk.Entry(base, node.size_bytes), *(disk.Entry(p, b) for p, b, _ in rows)],
            elapsed_s=0.4,
            complete=True,
        )
        scan.entries.sort(key=lambda e: e.bytes, reverse=True)
        return disk.summarize(
            scan, base, args.depth, lambda p: [owners[p]] if owners.get(p) else [], []
        )

    return handler


def fake_run_command(world: World) -> Any:
    async def handler(ctx: ToolContext, args: RunCommandArgs) -> Any:
        try:
            plan = parse_pipeline(args.cmd, PathGuard())
        except Refused as exc:
            raise ToolError(f"refused: {exc}") from exc
        for rule in world.commands:
            if re.search(rule.match, args.cmd):
                out: dict[str, Any] = {
                    "exit_code": rule.exit_code,
                    "output": scrub_output(rule.output)[
                        : int(ctx.settings.chat.max_tool_result_chars * RESULT_SHARE)
                    ],
                }
                if rule.stderr.strip() and (rule.exit_code != 0 or not rule.output.strip()):
                    out["stderr"] = scrub_output(rule.stderr.strip())[-1500:]
                if plan.notes:
                    out["notes"] = list(plan.notes)
                return out
        return {"exit_code": 127, "output": "", "stderr": world.default}

    return handler


def swap_executors(tools: dict[str, Tool], world: World) -> dict[str, Tool]:
    """The real tool definitions with the host-facing executors replaced."""
    from dataclasses import replace

    out = dict(tools)
    if "run_command" in out:
        out["run_command"] = replace(out["run_command"], handler=fake_run_command(world))
    if "disk_usage" in out:
        out["disk_usage"] = replace(out["disk_usage"], handler=fake_disk_usage(world))
    return out


def tools_builder(world: World) -> Any:
    def build(settings: Settings, mcp: Any) -> dict[str, Tool]:
        return swap_executors(build_tools(settings, mcp, assume_linux=True), world)

    return build


# --------------------------------------------------------------------------- the app stand-in


class FixedToolMode:
    """`ToolModeResolver` with the answer already known."""

    def __init__(self, mode: str):
        self.mode = mode

    async def resolve(self) -> str:
        return self.mode


@dataclass
class BenchApp:
    """Everything the chat engine, the tools and the report engine reach through `app`."""

    settings: Settings
    db: Database
    llm: LLMClient
    tracing: Any
    tool_mode: FixedToolMode
    collectors: FakeCollectors
    mcp: FakeMcp
    api_gateway: ApiGateway
    actions: ActionService
    reports: ReportEngine
    events: EventBus
    host_env: HostEnv = field(default_factory=lambda: HostEnv(root=Path("/")))
    chat: ChatEngine | None = None
    transport: httpx.MockTransport | None = None
    snapshot: Snapshot | None = None

    async def pending_approvals_count(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM actions WHERE status = 'pending'")
        return int(row["n"]) if row else 0

    async def close(self) -> None:
        await self.actions.close()
        await self.api_gateway.close()
        await self.db.close()


async def build_app(
    scenario: Scenario,
    snapshot: Snapshot,
    llm: LLMClient,
    tracing: Any,
    *,
    tool_mode: str,
    now: float | None = None,
    chat: ChatSettings | None = None,
    effort: str | None = None,
) -> BenchApp:
    """A throw-away application around the scenario's world: in-memory database, fake
    collectors, MCP and HTTP. `llm` is the one real connection; `chat` the chat settings of the
    app (None = defaults). `effort` is the thinking level under test (the API's
    reasoning_effort) for the report; the chat answers take it from `llm.cfg`."""
    world = scenario.world
    settings = bench_settings(llm.cfg, world, [snapshot.settings, scenario.settings], chat)
    if effort is not None:
        settings.report.reasoning_effort = effort  # type: ignore[assignment]
    db = Database(":memory:")
    await db.connect()
    await _load_history(db, snapshot, now)
    collectors = FakeCollectors(snapshot, world, now=now)
    transport = world_transport(world)
    mcp = FakeMcp(settings, world)
    gateway = ApiGateway(settings, transport=transport)
    actions = ActionService(db, settings, transport=transport, mcp=mcp, api=gateway)
    events = EventBus()
    reports = ReportEngine(settings, db, llm, collectors, events, WindowObserver())
    reports.time_limit = lambda: float(scenario.timeout_s or 600)  # type: ignore[method-assign]
    app = BenchApp(
        settings=settings,
        db=db,
        llm=llm,
        tracing=tracing,
        tool_mode=FixedToolMode(tool_mode),
        collectors=collectors,
        mcp=mcp,
        api_gateway=gateway,
        actions=actions,
        reports=reports,
        events=events,
        transport=transport,
        snapshot=snapshot,
    )
    app.chat = ChatEngine(
        app,  # type: ignore[arg-type]
        promql_transport=transport,
        tools_builder=tools_builder(world),
    )
    return app


async def _load_history(db: Database, snapshot: Snapshot, now: float | None) -> None:
    """Older collector samples, so the digest can compute trends."""
    base = time.time() if now is None else now
    rows = []
    for name, samples in snapshot.history.items():
        shift = 0.0 if snapshot.now is None else base - snapshot.now
        for sample in samples:
            data = rebase_times(sample.data, shift) if shift else sample.data
            rows.append((base - sample.hours_ago * 3600, name, 1, json.dumps(data), None))
    if rows:
        await db.executemany(
            "INSERT INTO collector_samples (ts, collector, ok, data, error) VALUES (?, ?, ?, ?, ?)",
            rows,
        )


async def wait_idle(app: BenchApp) -> None:
    """Let background tasks the engines started (chat title) settle before closing."""
    await asyncio.sleep(0)
    await app.tracing.flush()
