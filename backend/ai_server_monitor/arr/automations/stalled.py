"""Stalled / failed download cleanup.

Finds qBittorrent torrents that are stuck (no progress for `stalled_after`, stuck fetching
metadata, error state) and *arr queue items that failed to import. A torrent is mapped to
the *arr queue item that owns it by download id (the torrent hash); the owning app is then
asked to remove it, blocklist the release and search again. A torrent that no *arr owns is
only ever *suggested* for removal (it may be something the user added by hand).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from ...collectors.arr import is_stalled_candidate, parse_time, queue_row, torrent_row
from ...config import StalledCleanupSettings
from ..base import ArrError
from .base import SOURCE, AutomationEnv, recent_args, run_key, short, tally

log = logging.getLogger(__name__)

# Queue states that mean the download is finished or failed but could not be imported.
FAILED_STATES = {"failed", "failedPending", "importBlocked"}
DEFAULT_RUN_LIMIT = 25
LABEL = "Stalled cleanup"


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


async def run(env: AutomationEnv) -> dict[str, Any]:
    cfg: StalledCleanupSettings = env.settings.arr.actions.stalled_cleanup
    clients = env.service.clients
    now = env.clock()
    stalled_after = cfg.stalled_after.total_seconds()

    # ---- gather: every *arr queue (who owns which download?) and the torrents
    owners: dict[str, tuple[str, str, dict[str, Any]]] = {}  # hash -> (app, instance, row)
    queue_rows: list[tuple[str, str, dict[str, Any]]] = []
    arr_unreadable = False
    for (kind, name), app in clients.apps.items():
        try:
            items, _ = await app.queue(max_items=1000)
        except ArrError as exc:
            arr_unreadable = True
            log.warning("stalled cleanup: cannot read %s/%s queue: %s", kind, name, exc)
            continue
        for item in items:
            row = queue_row(item)
            queue_rows.append((kind, name, row))
            if row["download_id"]:
                owners.setdefault(str(row["download_id"]).lower(), (kind, name, row))

    torrents: list[dict[str, Any]] = []
    if clients.qbit is not None:
        try:
            torrents = [torrent_row(t, now) for t in await clients.qbit.torrents()]
        except ArrError as exc:
            log.warning("stalled cleanup: cannot read qBittorrent: %s", exc)
            return {"skipped": f"qBittorrent unreadable: {exc}"}

    # ---- what was already asked about recently (dedupe, incl. open approvals)
    seen_queue = {
        (a["app"], a["instance"], a["queue_id"])
        for a in await recent_args(env, "arr_remove_queue_item")
    }
    seen_hash = {a["hash"].lower() for a in await recent_args(env, "qbit_delete_torrent")}

    # Policy "auto" executes at once, so only as many items are asked for as the hourly cap
    # allows. Otherwise everything is filed and the cap is applied when items are executed
    # (the ones beyond it become "deferred").
    limit = (cfg.max_per_hour or DEFAULT_RUN_LIMIT) if cfg.policy == "auto" else None
    removals: list[tuple[dict[str, Any], str | None]] = []
    deletions: list[tuple[dict[str, Any], str | None]] = []
    requested_queue: set[tuple[str, str, int]] = set()

    def full() -> bool:
        return limit is not None and len(removals) + len(deletions) >= limit

    def remove_queue_item(kind: str, name: str, row: dict[str, Any], reason: str) -> None:
        key = (kind, name, int(row["id"]))
        if key in seen_queue or key in requested_queue or full():
            return
        requested_queue.add(key)
        removals.append(
            (
                {
                    "app": kind,
                    "instance": name,
                    "queue_id": row["id"],
                    "blocklist": True,
                    "remove_from_client": True,
                    "search": True,
                    "title": short(row["title"]),
                },
                reason,
            )
        )

    # ---- stalled torrents
    for row in sorted(torrents, key=lambda r: -r["inactive_s"]):
        if not is_stalled_candidate(row, stalled_after):
            continue
        reason = (
            f"qBittorrent state {row['state']}, {row['progress'] * 100:.0f}% done, "
            f"{row['seeds'] or 0} seed(s), no progress for {_hours(row['inactive_s'])}"
        )
        owner = owners.get(str(row["hash"]).lower())
        if owner:
            remove_queue_item(*owner[:2], owner[2], reason)
        elif not arr_unreadable and str(row["hash"]).lower() not in seen_hash:
            if full():
                continue
            seen_hash.add(str(row["hash"]).lower())
            deletions.append(
                (
                    {"hash": row["hash"], "delete_files": False, "title": short(row["name"])},
                    reason + "; not owned by any *arr, so it is only suggested",
                )
            )

    # ---- *arr queue items that failed to import / download
    min_age = timedelta(seconds=stalled_after).total_seconds()
    for kind, name, row in queue_rows:
        if row["tracked_state"] not in FAILED_STATES and row["status"] != "failed":
            continue
        added = parse_time(row["added"])
        if added is not None and now - added < min_age:
            continue  # give it time: imports often resolve themselves
        remove_queue_item(
            kind,
            name,
            row,
            f"{kind}/{name}: {row['tracked_state'] or row['status']}"
            + (f" ({row['messages'][0]})" if row["messages"] else ""),
        )

    # ---- one request per action type for the whole run: one approval batch
    key = run_key("stalled_cleanup", env)
    records: list[dict[str, Any]] = []
    if removals:
        records += await env.service.request_many(
            "arr_remove_queue_item", removals, SOURCE, run_key=key, label=LABEL
        )
    if deletions:
        records += await env.service.request_many(
            "qbit_delete_torrent",
            deletions,
            SOURCE,
            run_key=key,
            label=LABEL,
            suggest_only=True,
        )
    return {"requested": len(records), "outcomes": tally(records)}
