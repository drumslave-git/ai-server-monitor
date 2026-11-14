"""Metrics for the *arr integration and the action layer, evaluated at scrape time.

Everything comes from the arr collector's latest in-memory result (no *arr API calls) plus
one indexed COUNT for pending approvals.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from .metrics import Family, Sample

ARR_KINDS = ("radarr", "sonarr", "whisparr")


def configured_instances(settings: Settings) -> list[tuple[str, str]]:
    """(type, name) of every configured *arr instance, Prowlarr and qBittorrent."""
    arr = settings.arr
    out = [(kind, inst.name) for kind in ARR_KINDS for inst in getattr(arr, kind)]
    if arr.prowlarr is not None:
        out.append(("prowlarr", arr.prowlarr.name))
    if arr.qbittorrent is not None:
        out.append(("qbittorrent", "qbittorrent"))
    return out


def arr_families(
    settings: Settings, arr_data: dict[str, Any] | None, pending_approvals: int
) -> list[Family]:
    pending = Family(
        "asm_pending_approvals",
        "gauge",
        "Actions waiting for a human decision.",
        [Sample(pending_approvals)],
    )
    if not arr_data:
        # No result yet (or every instance unreachable): per-app numbers would be guesses.
        return [pending]
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for e in [*(arr_data.get("apps") or []), arr_data.get("prowlarr"), arr_data.get("qbittorrent")]:
        if isinstance(e, dict):
            entries[(str(e.get("type")), str(e.get("name")))] = e
    up: list[Sample] = []
    queue: list[Sample] = []
    stalled: list[Sample] = []
    for kind, name in configured_instances(settings):
        entry = entries.get((kind, name))
        reachable = bool(entry and entry.get("ok"))
        up.append(Sample(1 if reachable else 0, {"app": name}))
        if entry and reachable:
            if kind in ARR_KINDS:
                total = (entry.get("queue") or {}).get("total")
                if total is not None:
                    queue.append(Sample(total, {"app": name}))
            if kind == "qbittorrent":
                stalled.append(Sample(entry.get("stalled_total") or 0, {"app": name}))
    qbit = entries.get(("qbittorrent", "qbittorrent")) or {}
    states = (qbit.get("summary") or {}).get("by_state") or {}
    return [
        pending,
        Family("asm_arr_up", "gauge", "1 if the app's API answered at the last check.", up),
        Family("asm_arr_queue_size", "gauge", "Items in the app's download queue.", queue),
        Family(
            "asm_arr_stalled_downloads",
            "gauge",
            "Stalled or broken downloads (no progress for the configured time).",
            stalled,
        ),
        Family(
            "asm_qbittorrent_torrents",
            "gauge",
            "Torrents in qBittorrent by state.",
            [Sample(n, {"state": s}) for s, n in sorted(states.items())],
        ),
    ]
