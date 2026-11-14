"""The write actions: name, argument schema, which policy governs them, executor.

These are primitives. Automations (stalled cleanup, missing/cutoff search, ...) are built
from them later and call `ActionService.request`; nothing here decides *when* to act.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..arr import SEARCH_COMMANDS, ArrError, Clients
from ..config import Settings

ArrKind = Literal["radarr", "sonarr", "whisparr"]
TORRENT_HASH = r"^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"


class ActionError(Exception):
    """An action cannot be requested or executed (bad arguments, unknown target, ...)."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    """Human-readable name of what this touches; only used in the audit log."""


class QbitDeleteArgs(_Args):
    hash: str = Field(pattern=TORRENT_HASH)
    """Torrent hash (v1: 40 hex chars, v2: 64)."""
    delete_files: bool = False
    """Also delete the downloaded files from disk."""


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


class ProwlarrTestArgs(_Args):
    indexer_id: int = Field(ge=1)


Executor = Callable[[Clients, Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ActionDef:
    name: str
    description: str
    args_model: type[_Args]
    policy_key: Callable[[Any], str]
    """Which `arr.actions.<key>` settings (policy, per-hour cap) govern this action."""
    target: Callable[[Any], str]
    execute: Executor
    check: Callable[[Settings, Any], str | None] = lambda settings, args: None
    """Returns a reason when the request cannot be carried out (unknown instance, ...)."""
    kind: str = "write"

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


def _check_qbit(settings: Settings, args: Any) -> str | None:
    return None if settings.arr.qbittorrent else "qBittorrent is not configured"


def _check_prowlarr(settings: Settings, args: Any) -> str | None:
    return None if settings.arr.prowlarr else "Prowlarr is not configured"


# --------------------------------------------------------------------------- executors


async def _qbit_delete(clients: Clients, args: QbitDeleteArgs) -> dict[str, Any]:
    if clients.qbit is None:
        raise ActionError("qBittorrent is not configured")
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
        description="Remove a torrent from qBittorrent, optionally with its files.",
        args_model=QbitDeleteArgs,
        policy_key=lambda a: "stalled_cleanup",
        target=lambda a: f"qbittorrent: {a.title or a.hash[:12]}",
        check=_check_qbit,
        execute=_qbit_delete,
    ),
    ActionDef(
        name="arr_remove_queue_item",
        description="Remove a download from an *arr queue, with optional blocklist and re-search.",
        args_model=ArrRemoveQueueArgs,
        policy_key=lambda a: "stalled_cleanup",
        target=lambda a: f"{a.app}/{a.instance}: {a.title or f'queue item {a.queue_id}'}",
        check=_has_instance,
        execute=_arr_remove,
    ),
    ActionDef(
        name="arr_search",
        description="Trigger a search for given movie/episode ids in an *arr app.",
        args_model=ArrSearchArgs,
        policy_key=lambda a: f"{a.purpose}_search",
        target=lambda a: f"{a.app}/{a.instance}: {a.title or f'{len(a.ids)} item(s)'}",
        check=_check_search,
        execute=_arr_search,
    ),
    ActionDef(
        name="prowlarr_test_indexer",
        description="Run Prowlarr's connectivity test for one indexer.",
        args_model=ProwlarrTestArgs,
        policy_key=lambda a: "indexer_triage",
        target=lambda a: f"prowlarr: {a.title or f'indexer {a.indexer_id}'}",
        check=_check_prowlarr,
        execute=_prowlarr_test,
    ),
)

REGISTRY: dict[str, ActionDef] = {d.name: d for d in _ACTIONS}
