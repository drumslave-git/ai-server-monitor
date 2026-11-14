"""Missing / cutoff-unmet searches in small, rotating batches.

Per run and instance: ask the *arr for the wanted items it searched least recently, skip the
ones already requested in the last day, and request one search for at most `batch / n`
items. Executing searches moves items to the back of the app's own last-search order, and
the dedupe covers suggested/pending ones, so successive runs walk through the list instead
of repeating the first N.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ...config import SearchActionSettings
from ..arr import MOVIE_LIKE
from ..base import ArrError
from .base import SOURCE, AutomationEnv, recent_args, short, tally

log = logging.getLogger(__name__)

PAGE = 100
MAX_PAGES = 5


def describe(kind: str, record: dict[str, Any]) -> str:
    if kind in MOVIE_LIKE:
        year = record.get("year")
        return f"{record.get('title') or '?'}" + (f" ({year})" if year else "")
    series = (record.get("series") or {}).get("title") or "?"
    return f"{series} S{record.get('seasonNumber', 0):02d}E{record.get('episodeNumber', 0):02d}"


async def run(env: AutomationEnv, purpose: Literal["missing", "cutoff"]) -> dict[str, Any]:
    cfg: SearchActionSettings = getattr(env.settings.arr.actions, f"{purpose}_search")
    clients = env.service.clients
    if not clients.apps:
        return {"skipped": "no *arr app configured"}
    done = await recent_args(env, "arr_search")
    per_instance = max(1, cfg.batch // len(clients.apps))
    records: list[dict[str, Any]] = []
    for (kind, name), app in clients.apps.items():
        taken = {
            i
            for a in done
            if (a["app"], a["instance"], a["purpose"]) == (kind, name, purpose)
            for i in a["ids"]
        }
        ids: list[int] = []
        titles: list[str] = []
        try:
            for page in range(1, MAX_PAGES + 1):
                items, total = await app.wanted(
                    purpose, page=page, page_size=PAGE, least_recently_searched=True
                )
                if total is None:
                    break  # this app offers no wanted list
                for item in items:
                    if (
                        item["id"] in taken
                        or item["id"] in ids
                        or item.get("isAvailable") is False  # movie not released yet
                    ):
                        continue
                    ids.append(item["id"])
                    titles.append(describe(kind, item))
                    if len(ids) >= per_instance:
                        break
                if len(ids) >= per_instance or page * PAGE >= total:
                    break
        except ArrError as exc:
            log.warning("%s search: cannot read %s/%s: %s", purpose, kind, name, exc)
            continue
        if not ids:
            continue
        records.append(
            await env.service.request(
                "arr_search",
                {
                    "app": kind,
                    "instance": name,
                    "ids": ids,
                    "purpose": purpose,
                    "title": short(", ".join(titles)),
                },
                SOURCE,
                reason=f"{purpose} items searched least recently ({len(ids)} of the wanted list)",
            )
        )
    return {"requested": len(records), "outcomes": tally(records)}
