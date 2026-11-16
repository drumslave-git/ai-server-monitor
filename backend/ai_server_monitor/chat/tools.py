"""Tool registry: everything the chat model can call.

Each tool has a name, description, argument model (→ JSON schema, validation), a kind
(`read` | `write`) and a handler. Write tools never execute anything themselves: they file
an `ActionService.request(..., source="chat:<session>")` and report what policy decided
(suggested / pending approval / rejected / executed / dry_run).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..actions import REGISTRY as ACTIONS
from ..actions import ActionError
from ..arr import ArrApp, ArrError
from ..config import Settings
from ..digest.snapshot import sanitize
from ..promql import PromQL, PromQLError

if TYPE_CHECKING:
    from ..context import AppContext

ToolKind = Literal["read", "write"]
LOOKUP_RESULTS = 8
PROMQL_SERIES = 20
PROMQL_POINTS = 30


class ToolError(Exception):
    """The tool could not do what was asked; the message goes back to the model."""


@dataclass
class ToolContext:
    app: AppContext
    session_id: str
    promql_transport: Any = None
    source: str | None = None
    """Who asks, for the audit log of write actions. Default: chat:<session>."""
    turn: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    """Identifies this chat turn: the write requests of one turn share an approval batch."""
    routing: Any = None
    """The tool-routing state of this turn (`chat/routing.py`), when routing is on."""

    @property
    def settings(self) -> Settings:
        return self.app.settings


Handler = Callable[[ToolContext, Any], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    kind: ToolKind
    handler: Handler
    enabled: Callable[[Settings], bool] = field(default=lambda settings: True)
    kind_for: Callable[[dict[str, Any]], ToolKind] | None = None
    """For tools that read or write depending on the call (generic API / MCP calls): the kind
    of this call from its raw arguments. `kind` is the fallback."""

    def call_kind(self, args: dict[str, Any]) -> ToolKind:
        if self.kind_for is not None:
            try:
                return self.kind_for(args)
            except Exception:
                return self.kind
        return self.kind

    def definition(self) -> dict[str, Any]:
        """OpenAI-style function definition (also used for the JSON-in-text prompt)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": _clean_schema(self.args_model.model_json_schema()),
            },
        }


def _clean_schema(node: Any) -> Any:
    """Drop pydantic's `title` noise: small models read every token of the schema."""
    if isinstance(node, dict):
        return {k: _clean_schema(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_clean_schema(v) for v in node]
    return node


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- read tools


class NoArgs(_Args):
    pass


async def _get_status(ctx: ToolContext, args: NoArgs) -> Any:
    app = ctx.app
    digest = await app.reports.digest()
    latest = await app.reports.store.latest(status="ok")
    return {
        "health_flags": [f.to_dict() for f in digest.flags],
        "digest": digest.text.splitlines(),
        "latest_report": (
            {
                k: latest.get(k)
                for k in ("id", "ts", "health", "score", "summary", "findings", "model")
            }
            if latest
            else None
        ),
        "pending_approvals": await app.pending_approvals_count(),
    }


class CollectorArgs(_Args):
    collector: str = Field(
        description="system, docker, gpu, systemd, journal, apt, image_updates, prometheus or arr"
    )
    section: str | None = Field(
        default=None,
        description="Part of the data, e.g. 'memory', 'filesystems', 'processes.top_memory', "
        "'containers', 'failed', 'packages', 'gpus'. Omit to get the list of sections plus a "
        "short default view (for gpu: each GPU with its top processes by VRAM).",
    )
    contains: str | None = Field(
        default=None, description="Keep only list items with this text in any of their values."
    )
    sort_by: str | None = Field(
        default=None, description="Numeric field of the list items to sort by, largest first."
    )
    limit: int = Field(default=15, ge=1, le=100, description="Max list items returned.")


def _overview(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = f"object with keys: {', '.join(list(value)[:12])}"
        elif isinstance(value, list):
            out[key] = f"list of {len(value)} items"
        else:
            out[key] = value
    return out


def _section_names(data: dict[str, Any]) -> list[str]:
    """Every section the model can ask for: top-level keys, and `key.sub` for objects."""
    names: list[str] = []
    for key, value in data.items():
        names.append(key)
        if isinstance(value, dict):
            names.extend(f"{key}.{sub}" for sub in list(value)[:8])
    return names


GPU_TOP_PROCESSES = 5

FALLBACKS = {
    "disk_usage": "run_command `df -h PATH` or get_collector_data system section=filesystems",
    "container_inspect": "get_collector_data docker section=containers, or run_command `ps aux`",
    "container_logs": "run_command `journalctl -n 100 --no-pager`, or container_inspect",
    "listening_ports": "run_command `ss -tlnp` and container_inspect (published ports)",
    "run_command": "the matching tool: get_collector_data, disk_usage, container_inspect",
    "get_collector_data": "run_command (e.g. `df -h`, `free -h`, `ps aux`) or get_status",
    "metric_history": "get_collector_data for the current value",
    "diagnose_service": "container_logs and the service's settings, one at a time",
}
"""What to try when a tool fails (appended to its error, so a small model does not stop at
'unable to retrieve')."""


def failure_hint(tool_name: str) -> str:
    fallback = FALLBACKS.get(tool_name)
    return f" Do not stop here: try {fallback}." if fallback else ""


def _default_view(collector: str, data: dict[str, Any]) -> dict[str, Any] | None:
    """What a bare `get_collector_data <collector>` shows besides the section list, for the
    collectors whose answer lives one level down (a model that sees only 'gpus: list of 1
    items' stops there)."""
    if collector != "gpu":
        return None
    gpus = []
    for gpu in data.get("gpus") or []:
        if not isinstance(gpu, dict):
            continue
        keep = ("index", "name", "util_percent", "mem_used", "mem_total", "mem_percent")
        keep += ("temperature_c", "power_w")
        view = {k: gpu[k] for k in keep if k in gpu}
        procs = [p for p in gpu.get("processes") or [] if isinstance(p, dict)]
        procs.sort(key=lambda p: p.get("mem_used") or 0, reverse=True)
        view["top_processes_by_vram"] = [
            {k: p[k] for k in ("name", "pid", "mem_used") if k in p}
            for p in procs[:GPU_TOP_PROCESSES]
        ]
        if len(procs) > GPU_TOP_PROCESSES:
            view["more_processes"] = len(procs) - GPU_TOP_PROCESSES
        gpus.append(view)
    return {"gpus": gpus} if gpus else None


def _ranked(items: list[Any]) -> list[Any]:
    """Number list items 1, 2, 3... in their current order (dict items get a `rank` field)."""
    return [
        {"rank": n, **item} if isinstance(item, dict) else item for n, item in enumerate(items, 1)
    ]


def _dig(data: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            raise KeyError(part)
        data = data[part]
    return data


async def _collector_data(ctx: ToolContext, args: CollectorArgs) -> Any:
    manager = ctx.app.collectors
    if args.collector not in manager.registry:
        raise ToolError(f"unknown collector; choose one of: {', '.join(manager.registry)}")
    status = manager.status()[args.collector]
    result = manager.latest.get(args.collector)
    if result is None or not result.ok:
        return {
            "collector": args.collector,
            "state": status["state"],
            "reason": status["reason"],
            "error": result.error if result else None,
            "data": None,
        }
    data = sanitize(args.collector, result.data)
    base = {"collector": args.collector, "age_s": round(time.time() - result.ts)}
    if args.section is None:
        out = {**base, "sections": _overview(data), "available_sections": _section_names(data)}
        if view := _default_view(args.collector, data):
            out["default_view"] = view
        return out
    try:
        part = _dig(data, args.section)
    except KeyError:
        raise ToolError(f"no section {args.section!r}; available: {', '.join(data)}") from None
    if isinstance(part, list):
        items = part
        if args.contains:
            needle = args.contains.lower()
            items = [i for i in items if needle in str(i).lower()]
        if args.sort_by:

            def sort_key(item: Any) -> tuple[bool, float]:
                value = item.get(args.sort_by) if isinstance(item, dict) else None
                numeric = isinstance(value, int | float) and not isinstance(value, bool)
                return (numeric, float(value) if numeric else 0.0)

            if not any(sort_key(i)[0] for i in items):
                fields = sorted(
                    {
                        k
                        for i in items
                        if isinstance(i, dict)
                        for k, v in i.items()
                        if isinstance(v, int | float) and not isinstance(v, bool)
                    }
                )
                raise ToolError(
                    f"no numeric field {args.sort_by!r} in these items; "
                    f"numeric fields: {', '.join(fields) or 'none'}"
                )
            items = sorted(items, key=sort_key, reverse=True)
        shown = items[: args.limit]
        out = {**base, "section": args.section, "total": len(part)}
        if args.sort_by or args.section.rsplit(".", 1)[-1].startswith("top"):
            # Ranked: the order is final and numbered, so the model never has to compare
            # numbers with different units ("859.8 MiB" vs "914.6 MiB") by itself.
            shown = _ranked(shown)
            if args.sort_by:
                out["sorted_by"] = f"{args.sort_by}, largest first (rank 1 = largest)"
        return {**out, "items": shown}
    return {**base, "section": args.section, "value": part}


class ListReportsArgs(_Args):
    limit: int = Field(default=5, ge=1, le=20)
    status: Literal["ok", "failed", "skipped"] | None = None


async def _list_reports(ctx: ToolContext, args: ListReportsArgs) -> Any:
    items, total = await ctx.app.reports.store.page(limit=args.limit, status=args.status)
    keep = ("id", "ts", "status", "health", "score", "summary", "findings", "error")
    return {"total": total, "reports": [{k: r.get(k) for k in keep} for r in items]}


class GetReportArgs(_Args):
    id: int = Field(ge=1)


async def _get_report(ctx: ToolContext, args: GetReportArgs) -> Any:
    report = await ctx.app.reports.store.get(args.id)
    if report is None:
        raise ToolError(f"no report with id {args.id}")
    return {
        **{k: report.get(k) for k in ("id", "ts", "status", "health", "score", "summary")},
        "markdown": (report.get("markdown") or "").splitlines(),
    }


class PromQLArgs(_Args):
    query: str = Field(min_length=1, description="PromQL expression.")
    mode: Literal["instant", "range"] = "instant"
    lookback_minutes: int = Field(
        default=60, ge=1, le=10080, description="For mode=range: how far back to look."
    )
    step_seconds: int = Field(default=60, ge=5, le=86400, description="For mode=range.")


async def _promql(ctx: ToolContext, args: PromQLArgs) -> Any:
    cfg = ctx.settings.collectors.prometheus
    if not cfg.url:
        raise ToolError("Prometheus is not configured (Settings → Collectors → Prometheus)")
    client = PromQL(
        cfg.url,
        token=cfg.token.get_secret_value(),
        username=cfg.username,
        password=cfg.password.get_secret_value(),
        timeout=cfg.timeout.total_seconds(),
        max_series=PROMQL_SERIES,
        max_points=PROMQL_POINTS,
        transport=ctx.promql_transport,
    )
    try:
        if args.mode == "instant":
            result = await client.instant(args.query)
        else:
            now = time.time()
            result = await client.range(
                args.query,
                start=now - args.lookback_minutes * 60,
                end=now,
                step=args.step_seconds,
            )
    except PromQLError as exc:
        raise ToolError(str(exc)) from exc
    finally:
        await client.close()
    return result.to_dict()


ArrKind = Literal["radarr", "sonarr", "whisparr"]


class ArrTarget(_Args):
    app: ArrKind
    instance: str | None = Field(
        default=None, description="Instance name; optional when only one is configured."
    )


class ArrLookupArgs(ArrTarget):
    term: str = Field(min_length=1, max_length=100, description="Title to search for.")


def _arr_app(ctx: ToolContext, args: ArrTarget) -> ArrApp:
    apps = ctx.app.actions.clients.apps
    names = [name for (kind, name) in apps if kind == args.app]
    if not names:
        raise ToolError(f"no {args.app} instance is configured")
    name = args.instance or (names[0] if len(names) == 1 else None)
    if name is None or (args.app, name) not in apps:
        raise ToolError(f"choose an instance: {', '.join(names)}")
    return apps[(args.app, name)]


def _guard(exc: ArrError) -> ToolError:
    return ToolError(str(exc))


async def _arr_lookup(ctx: ToolContext, args: ArrLookupArgs) -> Any:
    app = _arr_app(ctx, args)
    try:
        found = await app.lookup(args.term)
    except ArrError as exc:
        raise _guard(exc) from exc
    keep = ("title", "year", "tmdbId", "tvdbId", "imdbId", "status", "overview")
    return {
        "instance": app.name,
        "candidates": [
            {
                **{k: c[k] for k in keep if c.get(k) not in (None, "")},
                "in_library": bool(c.get("id")),
            }
            for c in found[:LOOKUP_RESULTS]
        ],
        "shown": min(len(found), LOOKUP_RESULTS),
        "found": len(found),
    }


async def _quality_profiles(ctx: ToolContext, args: ArrTarget) -> Any:
    app = _arr_app(ctx, args)
    try:
        return {
            "instance": app.name,
            "quality_profiles": [
                {"id": p.get("id"), "name": p.get("name")} for p in await app.quality_profiles()
            ],
        }
    except ArrError as exc:
        raise _guard(exc) from exc


async def _root_folders(ctx: ToolContext, args: ArrTarget) -> Any:
    app = _arr_app(ctx, args)
    try:
        return {
            "instance": app.name,
            "root_folders": [
                {"path": f.get("path"), "free_bytes": f.get("freeSpace")}
                for f in await app.root_folders()
            ],
        }
    except ArrError as exc:
        raise _guard(exc) from exc


async def _arr_overview(ctx: ToolContext, args: NoArgs) -> Any:
    result = ctx.app.collectors.latest.get("arr")
    if result is None or not result.ok:
        raise ToolError("no *arr/qBittorrent data yet (not configured, or not collected yet)")
    data = result.data
    apps = [
        {
            "type": a["type"],
            "name": a["name"],
            "ok": a["ok"],
            "error": a.get("error"),
            "queue": {k: v for k, v in (a.get("queue") or {}).items() if k != "items"},
            "queue_problems": [
                {k: r.get(k) for k in ("id", "title", "tracked_state", "messages")}
                for r in (a.get("queue") or {}).get("items", [])
                if r["problem"]
            ][:8],
            "wanted": a.get("wanted"),
            "health": a.get("health"),
        }
        for a in data.get("apps", [])
    ]
    qbit = data.get("qbittorrent") or {}
    return {
        "summary": data.get("summary"),
        "findings": data.get("findings"),
        "apps": apps,
        "failing_indexers": [
            i for i in (data.get("prowlarr") or {}).get("indexers", []) if i.get("failing")
        ],
        "qbittorrent": {
            "summary": qbit.get("summary"),
            "stalled_total": qbit.get("stalled_total"),
            "stalled": [
                {k: t.get(k) for k in ("hash", "name", "state", "progress", "inactive_s")}
                for t in qbit.get("stalled", [])[:10]
            ],
        },
    }


class RecentActionsArgs(_Args):
    limit: int = Field(default=10, ge=1, le=30)
    status: str | None = Field(
        default=None,
        description="suggested, pending, rejected, expired, executed, failed or dry_run",
    )


def _action_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        k: record.get(k)
        for k in (
            "id",
            "batch_id",
            "ts",
            "action",
            "target",
            "status",
            "policy",
            "source",
            "reason",
            "result",
        )
    }


async def _pending(ctx: ToolContext, args: NoArgs) -> Any:
    return {"pending": [_action_view(r) for r in await ctx.app.actions.pending()]}


async def _recent_actions(ctx: ToolContext, args: RecentActionsArgs) -> Any:
    page = await ctx.app.actions.list_actions(limit=args.limit, status=args.status)
    return {"total": page["total"], "actions": [_action_view(r) for r in page["items"]]}


# --------------------------------------------------------------------------- write tools

_NOTES = {
    "pending": "NOT DONE: waiting for the user's approval (approval id {id}; the requests "
    "of this turn are one approval). Say 'waiting for your approval', never 'saved' or "
    "'done'. Tell the user to approve or reject it in the approvals list; it has NOT been "
    "done yet.",
    "suggested": "Recorded as a suggestion only; the policy does not let it run. "
    "It has NOT been done.",
    "executed": "Done.",
    "dry_run": "Dry-run mode is on: nothing was sent. It has NOT been done.",
    "failed": "The request failed (see result.error). It has NOT been done.",
    "rejected": "Rejected (see result.detail). It has NOT been done.",
    "queued": "Approved, but held back by the hourly limit; it runs by itself when the limit "
    "frees up. It has NOT been done yet.",
    "deferred": "Held back by the hourly limit (see result.detail). It has NOT been done.",
}


async def _resolve_torrent(ctx: ToolContext, name: str) -> tuple[str, str]:
    """(hash, full name) of the one torrent whose name matches `name`; the model gives a name
    instead of fumbling with a query to find the hash."""
    qbit = ctx.app.actions.clients.qbit
    if qbit is None:
        raise ToolError("qBittorrent is not configured")
    try:
        torrents = await qbit.torrents()
    except Exception as exc:
        raise ToolError(f"cannot list the torrents: {exc}"[:300]) from exc
    needle = name.strip().lower()
    hits = [t for t in torrents if needle in str(t.get("name", "")).lower()]
    exact = [t for t in hits if str(t.get("name", "")).lower() == needle]
    hits = exact or hits
    if not hits:
        raise ToolError(f"no torrent in qBittorrent matches {name!r}")
    if len(hits) > 1:
        shown = "; ".join(str(t.get("name"))[:80] for t in hits[:8])
        raise ToolError(
            f"{len(hits)} torrents match {name!r}: {shown}. Ask which one (or call again with "
            "more of the name)."
        )
    return str(hits[0]["hash"]), str(hits[0].get("name") or name)


def _write_handler(action: str) -> Handler:
    async def handler(ctx: ToolContext, args: BaseModel) -> Any:
        payload = args.model_dump(exclude_none=True)
        if action == "qbit_delete_torrent" and not payload.get("hash"):
            payload["hash"], payload["title"] = await _resolve_torrent(ctx, payload["name"])
        try:
            record = await ctx.app.actions.request(
                action,
                payload,
                ctx.source or f"chat:{ctx.session_id}",
                reason="requested in chat",
                run_key=f"turn:{ctx.turn}",
                label="Chat",
            )
        except ActionError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "action_id": record["id"],
            "approval_id": record.get("batch_id"),
            "status": record["status"],
            "policy": record["policy"],
            "target": record["target"],
            "result": record["result"],
            "note": _NOTES[record["status"]].format(id=record["id"]),
        }

    return handler


_WRITE_CONFIG: dict[str, Callable[[Settings], bool]] = {
    "qbit_delete_torrent": lambda s: s.arr.qbittorrent is not None,
    "arr_remove_queue_item": lambda s: bool(s.arr.radarr or s.arr.sonarr or s.arr.whisparr),
    "arr_search": lambda s: bool(s.arr.radarr or s.arr.sonarr or s.arr.whisparr),
    "arr_add_media": lambda s: bool(s.arr.radarr or s.arr.sonarr or s.arr.whisparr),
    "prowlarr_test_indexer": lambda s: s.arr.prowlarr is not None,
}


def _write_tools() -> list[Tool]:
    tools = []
    for name, definition in ACTIONS.items():
        if not definition.chat_tool:
            continue
        sample = _sample_args(definition)

        def enabled(
            settings: Settings, name: str = name, definition: Any = definition, sample: Any = sample
        ) -> bool:
            if not _WRITE_CONFIG[name](settings):
                return False
            policy_key = definition.policy_key(sample)
            return getattr(settings.arr.actions, policy_key).policy != "off"

        tools.append(
            Tool(
                name=name,
                description=(
                    f"WRITE. {definition.description} Call it as soon as the user asked for this "
                    "change, with the safe default for every option they did not mention: they "
                    "confirm on the approval card, so do not ask first (ask only when several "
                    "things match). It goes through the owner's policy and may need their "
                    "approval; report exactly what the result says."
                ),
                args_model=definition.args_model,
                kind="write",
                handler=_write_handler(name),
                enabled=enabled,
            )
        )
    return tools


def _sample_args(definition: Any) -> Any:
    """An instance of the action's args to ask `policy_key` which policy applies. The key
    only depends on `purpose` (searches); the default is enough for the on/off check."""
    fields = definition.args_model.model_fields
    values: dict[str, Any] = {}
    for key, info in fields.items():
        if info.is_required():
            values[key] = _PLACEHOLDERS.get(key, "x")
    return definition.args_model.model_construct(**values)


_PLACEHOLDERS: dict[str, Any] = {
    "app": "radarr",
    "ids": [1],
    "hash": "0" * 40,
    "queue_id": 1,
    "indexer_id": 1,
    "quality_profile_id": 1,
}


def _arr_configured(settings: Settings) -> bool:
    return bool(settings.arr.radarr or settings.arr.sonarr or settings.arr.whisparr)


READ_TOOLS: list[Tool] = [
    Tool(
        "get_status",
        "Current health of the server: health flags, the pre-analyzed data digest and the "
        "latest status report. Start here for general questions.",
        NoArgs,
        "read",
        _get_status,
    ),
    Tool(
        "get_collector_data",
        "Latest raw data of one collector, sliced. Without `section` you get the list of "
        "sections (gpu: also each GPU with its top VRAM processes). Examples: "
        "collector=system section=memory; collector=system section=processes.top_memory; "
        "collector=system section=filesystems; "
        "collector=docker section=containers sort_by=mem_usage limit=5.",
        CollectorArgs,
        "read",
        _collector_data,
    ),
    Tool(
        "list_reports",
        "Recent status reports (newest first) with health, score and summary.",
        ListReportsArgs,
        "read",
        _list_reports,
    ),
    Tool("get_report", "One full status report by id.", GetReportArgs, "read", _get_report),
    Tool(
        "promql_query",
        "Run a PromQL query against Prometheus (instant, or a range over the last N minutes).",
        PromQLArgs,
        "read",
        _promql,
        enabled=lambda s: bool(s.collectors.prometheus.url),
    ),
    Tool(
        "arr_lookup",
        "Search Radarr/Sonarr/Whisparr for a movie or series to add. Returns candidates "
        "with their TMDB/TVDB ids.",
        ArrLookupArgs,
        "read",
        _arr_lookup,
        enabled=_arr_configured,
    ),
    Tool(
        "arr_quality_profiles",
        "Quality profiles of a Radarr/Sonarr/Whisparr instance (needed to add media).",
        ArrTarget,
        "read",
        _quality_profiles,
        enabled=_arr_configured,
    ),
    Tool(
        "arr_root_folders",
        "Root folders of a Radarr/Sonarr/Whisparr instance (needed to add media).",
        ArrTarget,
        "read",
        _root_folders,
        enabled=_arr_configured,
    ),
    Tool(
        "get_arr_overview",
        "Download queues, problems, stalled torrents and failing indexers across the "
        "*arr apps, Prowlarr and qBittorrent.",
        NoArgs,
        "read",
        _arr_overview,
        enabled=lambda s: _arr_configured(s) or bool(s.arr.qbittorrent or s.arr.prowlarr),
    ),
    Tool(
        "list_pending_approvals",
        "Actions waiting for the owner's approval.",
        NoArgs,
        "read",
        _pending,
    ),
    Tool(
        "list_recent_actions",
        "The audit log of recent actions (what was suggested, approved, done).",
        RecentActionsArgs,
        "read",
        _recent_actions,
    ),
]


def build_tools(
    settings: Settings, mcp: Any = None, *, assume_linux: bool = False
) -> dict[str, Tool]:
    """The tools available under these settings, by name. `mcp` is the McpManager: the tools
    of its connected servers (as far as chosen in the settings) are added."""
    from .host_tools import host_tools  # (imports this module: not at the top)

    tools = [*READ_TOOLS, *host_tools(assume_linux), *_write_tools()]
    found = {t.name: t for t in tools if t.enabled(settings)}
    from ..apiaccess import api_tool_specs  # (the specs are plain data; keep imports local)

    for spec in api_tool_specs(settings, error_cls=ToolError):
        found.setdefault(spec.name, Tool(**spec.as_kwargs()))
    from ..apiaccess.diagnose import diagnose_tool_spec  # (imports conntest: late)

    diagnose = diagnose_tool_spec(ToolError)
    if diagnose.enabled(settings):
        found.setdefault(diagnose.name, Tool(**diagnose.as_kwargs()))
    if mcp is not None:
        from ..mcp_client import build_mcp_tools  # (imports this module: not at the top)

        for tool in build_mcp_tools(mcp):
            found.setdefault(tool.name, tool)
    return found
