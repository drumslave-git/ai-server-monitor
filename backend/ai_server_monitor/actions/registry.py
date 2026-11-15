"""The write actions: name, argument schema, which policy governs them, executor.

These are primitives. Automations (stalled cleanup, missing/cutoff search, ...) are built
from them later and call `ActionService.request`; nothing here decides *when* to act.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..apiaccess import (
    ACTION_DESCRIPTION,
    ACTION_NAME,
    POLICY_KEY,
    ApiCallActionArgs,
    action_check,
    action_run,
    action_target,
)
from ..arr import SEARCH_COMMANDS, ArrError, Clients
from ..config import Settings
from ..mcp_client import actions as mcp_actions

ArrKind = Literal["radarr", "sonarr", "whisparr"]
TORRENT_HASH = r"^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"


class ActionError(Exception):
    """An action cannot be requested or executed (bad arguments, unknown target, ...)."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    """Human-readable name of what this touches; only used in the audit log."""


class QbitDeleteArgs(_Args):
    hash: str | None = Field(default=None, pattern=TORRENT_HASH)
    """Torrent hash (v1: 40 hex chars, v2: 64). Or give `name` instead."""
    name: str | None = Field(default=None, max_length=300)
    """The torrent's name (or a distinctive part of it): the chat looks the hash up itself."""
    delete_files: bool = False
    """Also delete the downloaded files from disk. Default: keep them."""

    @model_validator(mode="after")
    def _identified(self) -> QbitDeleteArgs:
        if not self.hash and not (self.name or "").strip():
            raise ValueError("give the torrent's name or its hash")
        return self


class ArrRemoveQueueArgs(_Args):
    app: ArrKind
    instance: str = Field(min_length=1)
    """Instance name as configured under *arr & downloads."""
    queue_id: int = Field(ge=1)
    remove_from_client: bool = True
    """Also remove the download from the download client."""
    blocklist: bool = True
    """Blocklist the release so it is not grabbed again."""
    search: bool = False
    """Let the *arr search for a replacement release (otherwise it does not re-download)."""


class ArrSearchArgs(_Args):
    app: ArrKind
    instance: str = Field(min_length=1)
    ids: list[int] = Field(min_length=1, max_length=100)
    """Movie ids (Radarr) or episode ids (Sonarr, Whisparr) to search for."""
    purpose: Literal["missing", "cutoff"] = "missing"
    """Which policy applies: searching for missing items, or for quality upgrades."""


class ArrAddMediaArgs(_Args):
    app: ArrKind
    instance: str = Field(min_length=1)
    tmdb_id: int | None = Field(default=None, ge=1)
    """TMDB id (Radarr, Whisparr)."""
    tvdb_id: int | None = Field(default=None, ge=1)
    """TVDB id (Sonarr)."""
    quality_profile_id: int = Field(ge=1)
    root_folder_path: str = Field(min_length=1)
    search: bool = True
    """Search for the release right after adding."""


class ProwlarrTestArgs(_Args):
    indexer_id: int = Field(ge=1)


Executor = Callable[[Clients, Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ActionDef:
    """One kind of write. The *arr actions only need `policy_key` (their policy lives in
    `settings.arr.actions.<key>`), `target`, `check` and `execute`. Actions of other
    subsystems (MCP tools, generic API calls) plug in through the optional hooks:

        ActionDef(
            name="mcp_call", description=..., args_model=McpCallArgs,
            policy_key=lambda a: f"mcp:{a.server}",          # label stored in the audit row
            target=..., check=lambda settings, args: reason or None,
            policy_of=lambda settings, args: "approve",      # off | suggest | approve | auto
            max_per_hour_of=lambda settings, args: 0,        # 0 = no cap
            uses_dry_run=lambda settings: settings.mcp.dry_run,
            run=lambda service, args: <awaitable dict>,      # instead of `execute`
            chat_tool=False,                                 # the subsystem has its own chat tools
        )

    All hooks are optional; `run` replaces `execute` and gets the ActionService (so it can
    reach `service.mcp` and the like). A raised `ActionError` is recorded as `failed`."""

    name: str
    description: str
    args_model: type[_Args]
    policy_key: Callable[[Any], str]
    """Which `arr.actions.<key>` settings (policy, per-hour cap) govern this action."""
    target: Callable[[Any], str]
    execute: Executor | None = None
    check: Callable[[Settings, Any], str | None] = lambda settings, args: None
    """Returns a reason when the request cannot be carried out (unknown instance, ...)."""
    kind: str = "write"
    policy_of: Callable[[Settings, Any], str] | None = None
    """The policy for this request; default: `settings.arr.actions.<policy_key>.policy`."""
    max_per_hour_of: Callable[[Settings, Any], int] | None = None
    """The per-hour cap; default: `settings.arr.actions.<policy_key>.max_per_hour`."""
    uses_dry_run: Callable[[Settings], bool] = lambda settings: settings.arr.dry_run
    """Whether the global dry-run switch of this action's subsystem is on."""
    run: Callable[[Any, Any], Awaitable[dict[str, Any]]] | None = None
    """`run(service, args)` instead of `execute(clients, args)`."""
    chat_tool: bool = True
    """False = no generic chat tool is made for this action (its subsystem has its own)."""
    summary: Callable[[list[Any]], str] | None = None
    """What an approval batch of these items asks for, from their parsed arguments:
    "remove 40 queue items from sonarr (blocklist + re-search)". Default: the target of a
    single item, else "<name> × N"."""

    def schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


# --------------------------------------------------------------------------- checks


def _has_instance(settings: Settings, args: Any) -> str | None:
    if not any(i.name == args.instance for i in getattr(settings.arr, args.app)):
        return f"no {args.app} instance named {args.instance!r} is configured"
    return None


def _check_search(settings: Settings, args: Any) -> str | None:
    problem = _has_instance(settings, args)
    if problem:
        return problem
    limit = getattr(settings.arr.actions, f"{args.purpose}_search").batch
    if len(args.ids) > limit:
        return f"{len(args.ids)} ids exceed the batch size of {limit} for {args.purpose} searches"
    return None


def _check_add(settings: Settings, args: Any) -> str | None:
    problem = _has_instance(settings, args)
    if problem:
        return problem
    if args.app == "sonarr" and args.tvdb_id is None:
        return "tvdb_id is required for Sonarr"
    if args.app != "sonarr" and args.tmdb_id is None:
        return "tmdb_id is required for Radarr/Whisparr"
    return None


def _check_qbit(settings: Settings, args: Any) -> str | None:
    return None if settings.arr.qbittorrent else "qBittorrent is not configured"


def _check_prowlarr(settings: Settings, args: Any) -> str | None:
    return None if settings.arr.prowlarr else "Prowlarr is not configured"


# --------------------------------------------------------------------------- summaries


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


def _where(items: list[Any]) -> str:
    names = sorted({i.app if i.instance == i.app else f"{i.app}/{i.instance}" for i in items})
    return ", ".join(names)


def _remove_summary(items: list[Any]) -> str:
    flags = {(i.blocklist, i.search, i.remove_from_client) for i in items}
    note = ""
    if len(flags) == 1:
        blocklist, search, from_client = next(iter(flags))
        parts = (["blocklist"] if blocklist else []) + (["re-search"] if search else [])
        if not from_client:
            parts.append("keep in the download client")
        note = f" ({' + '.join(parts)})" if parts else ""
    return f"remove {_plural(len(items), 'queue item')} from {_where(items)}{note}"


def _search_summary(items: list[Any]) -> str:
    total = sum(len(i.ids) for i in items)
    purposes = " / ".join(sorted({i.purpose for i in items}))
    return f"search for {_plural(total, purposes + ' item')} in {_where(items)}"


# --------------------------------------------------------------------------- executors


async def _qbit_delete(clients: Clients, args: QbitDeleteArgs) -> dict[str, Any]:
    if clients.qbit is None:
        raise ActionError("qBittorrent is not configured")
    if not args.hash:
        raise ActionError("the torrent's hash is missing (the chat resolves it from the name)")
    found = await clients.qbit.torrents(hashes=args.hash)
    if not found:
        raise ActionError("torrent not found in qBittorrent (already removed?)")
    torrent = found[0]
    await clients.qbit.delete_torrent(args.hash, delete_files=args.delete_files)
    return {
        "name": torrent.get("name"),
        "state": torrent.get("state"),
        "size": torrent.get("size"),
        "deleted_files": args.delete_files,
    }


async def _arr_remove(clients: Clients, args: ArrRemoveQueueArgs) -> dict[str, Any]:
    app = clients.apps.get((args.app, args.instance))
    if app is None:
        raise ActionError(f"no {args.app} instance named {args.instance!r}")
    await app.remove_queue_item(
        args.queue_id,
        remove_from_client=args.remove_from_client,
        blocklist=args.blocklist,
        skip_redownload=not args.search,
    )
    return {
        "queue_id": args.queue_id,
        "removed_from_client": args.remove_from_client,
        "blocklisted": args.blocklist,
        "re_search": args.search,
    }


async def _arr_search(clients: Clients, args: ArrSearchArgs) -> dict[str, Any]:
    app = clients.apps.get((args.app, args.instance))
    if app is None:
        raise ActionError(f"no {args.app} instance named {args.instance!r}")
    command, field = SEARCH_COMMANDS[args.app]
    reply = await app.command(command, **{field: args.ids})
    return {"command": command, "ids": args.ids, "command_id": reply.get("id")}


async def _arr_add(clients: Clients, args: ArrAddMediaArgs) -> dict[str, Any]:
    app = clients.apps.get((args.app, args.instance))
    if app is None:
        raise ActionError(f"no {args.app} instance named {args.instance!r}")
    term, key = (
        (f"tvdb:{args.tvdb_id}", "tvdbId")
        if args.app == "sonarr"
        else (f"tmdb:{args.tmdb_id}", "tmdbId")
    )
    wanted_id = args.tvdb_id if args.app == "sonarr" else args.tmdb_id
    candidate = next((c for c in await app.lookup(term) if c.get(key) == wanted_id), None)
    if candidate is None:
        raise ActionError(f"nothing found for {term}")
    if candidate.get("id"):
        raise ActionError(f"{candidate.get('title')} is already in the library")
    added = await app.add(
        candidate,
        quality_profile_id=args.quality_profile_id,
        root_folder_path=args.root_folder_path,
        search=args.search,
    )
    return {"title": candidate.get("title"), "id": added.get("id"), "searching": args.search}


async def _prowlarr_test(clients: Clients, args: ProwlarrTestArgs) -> dict[str, Any]:
    if clients.prowlarr is None:
        raise ActionError("Prowlarr is not configured")
    try:
        ok, failures = await clients.prowlarr.test_indexer(args.indexer_id)
    except ArrError as exc:
        raise ActionError(str(exc)) from exc
    return {"indexer_id": args.indexer_id, "ok": ok, "failures": failures}


# --------------------------------------------------------------------------- registry

_ACTIONS = (
    ActionDef(
        name="qbit_delete_torrent",
        description=(
            "Remove a torrent from qBittorrent by `name` (a distinctive part is enough) or "
            "`hash`. The files are KEPT unless delete_files=true: do not ask about that, the "
            "owner confirms on the approval card."
        ),
        args_model=QbitDeleteArgs,
        policy_key=lambda a: "stalled_cleanup",
        target=lambda a: f"qbittorrent: {a.title or a.name or (a.hash or '')[:12]}",
        check=_check_qbit,
        execute=_qbit_delete,
        summary=lambda items: f"remove {_plural(len(items), 'torrent')} from qBittorrent",
    ),
    ActionDef(
        name="arr_remove_queue_item",
        description="Remove a download from an *arr queue, with optional blocklist and re-search.",
        args_model=ArrRemoveQueueArgs,
        policy_key=lambda a: "stalled_cleanup",
        target=lambda a: f"{a.app}/{a.instance}: {a.title or f'queue item {a.queue_id}'}",
        check=_has_instance,
        execute=_arr_remove,
        summary=_remove_summary,
    ),
    ActionDef(
        name="arr_search",
        description="Trigger a search for given movie/episode ids in an *arr app.",
        args_model=ArrSearchArgs,
        policy_key=lambda a: f"{a.purpose}_search",
        target=lambda a: f"{a.app}/{a.instance}: {a.title or f'{len(a.ids)} item(s)'}",
        check=_check_search,
        execute=_arr_search,
        summary=_search_summary,
    ),
    ActionDef(
        name="arr_add_media",
        description="Add a movie (Radarr/Whisparr) or series (Sonarr) to the library.",
        args_model=ArrAddMediaArgs,
        policy_key=lambda a: "add_media",
        target=lambda a: f"{a.app}/{a.instance}: add {a.title or (a.tmdb_id or a.tvdb_id)}",
        check=_check_add,
        execute=_arr_add,
        summary=lambda items: f"add {_plural(len(items), 'title')} to {_where(items)}",
    ),
    ActionDef(
        name="prowlarr_test_indexer",
        description="Run Prowlarr's connectivity test for one indexer.",
        args_model=ProwlarrTestArgs,
        policy_key=lambda a: "indexer_triage",
        target=lambda a: f"prowlarr: {a.title or f'indexer {a.indexer_id}'}",
        check=_check_prowlarr,
        execute=_prowlarr_test,
        summary=lambda items: f"test {_plural(len(items), 'indexer')} in Prowlarr",
    ),
    ActionDef(
        name=ACTION_NAME,
        description=ACTION_DESCRIPTION,
        args_model=ApiCallActionArgs,  # type: ignore[arg-type]
        policy_key=lambda a: POLICY_KEY,
        target=action_target,
        check=action_check,
        run=action_run,
        # uses_dry_run stays the default (arr.dry_run): the only services an API write can
        # reach are the *arr apps, Prowlarr and qBittorrent, whose safety switch that is.
        chat_tool=False,  # the chat has its own api_call tool (apiaccess)
    ),
    ActionDef(
        name=mcp_actions.ACTION_NAME,
        description=mcp_actions.DESCRIPTION,
        args_model=mcp_actions.McpCallArgs,  # type: ignore[arg-type]
        policy_key=mcp_actions.policy_key,
        target=mcp_actions.target,
        check=lambda settings, args: mcp_actions.check(settings.mcp, args),
        policy_of=lambda settings, args: mcp_actions.policy_of(settings.mcp, args),
        max_per_hour_of=lambda settings, args: 0,
        uses_dry_run=lambda settings: settings.mcp.dry_run,
        run=lambda service, args: mcp_actions.execute(service.mcp, args),
        chat_tool=False,
    ),
)

REGISTRY: dict[str, ActionDef] = {d.name: d for d in _ACTIONS}
