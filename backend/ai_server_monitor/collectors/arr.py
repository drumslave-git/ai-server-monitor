"""Radarr / Sonarr / Whisparr, Prowlarr and qBittorrent: queues, health, indexers, torrents.

Read-only. Which instances exist comes from the `arr` settings (*arr & downloads); this
collector's own settings only say whether and how often to look. One unreachable
instance is reported in its own entry and does not hide the others.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..arr import ArrApp, ArrError, Clients, Prowlarr, QBittorrent
from ..config import ArrCollectorSettings
from .base import Collector, CollectorUnavailable, HostEnv

_MAX_MESSAGES = 3
_MAX_STALLED = 30
_MAX_FINDINGS = 20
_PERSIST_PROBLEMS = 10

# qBittorrent states in which a torrent is expected to make progress.
_ACTIVE_DOWNLOAD = {"downloading", "forcedDL", "stalledDL", "metaDL", "forcedMetaDL"}
# States that are problems by themselves, whatever the activity.
_BROKEN = {"error", "missingFiles"}
_BAD_QUEUE_STATES = {"importBlocked", "failedPending", "failed"}
_BAD_QUEUE_STATUSES = {"failed", "warning", "downloadClientUnavailable"}


def parse_time(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def queue_row(item: dict[str, Any]) -> dict[str, Any]:
    messages: list[str] = []
    for entry in item.get("statusMessages") or []:
        text = "; ".join(str(m) for m in entry.get("messages") or [])
        title = entry.get("title")
        messages.append(f"{title}: {text}" if title and text else str(title or text))
    status = item.get("trackedDownloadStatus")
    state = item.get("trackedDownloadState")
    return {
        "id": item.get("id"),
        "download_id": item.get("downloadId"),
        "title": str(item.get("title") or "")[:200],
        "status": item.get("status"),
        "tracked_status": status,
        "tracked_state": state,
        "error": (item.get("errorMessage") or None) and str(item["errorMessage"])[:300],
        "messages": [m[:300] for m in messages[:_MAX_MESSAGES]],
        "size": item.get("size"),
        "size_left": item.get("sizeleft"),
        "protocol": item.get("protocol"),
        "indexer": item.get("indexer"),
        "download_client": item.get("downloadClient"),
        "added": item.get("added"),
        "problem": status in ("warning", "error")
        or state in _BAD_QUEUE_STATES
        or item.get("status") in _BAD_QUEUE_STATUSES,
    }


def torrent_row(t: dict[str, Any], now: float) -> dict[str, Any]:
    last = t.get("last_activity") or 0
    if last <= 0:
        last = t.get("added_on") or now
    return {
        "hash": t.get("hash"),
        "name": str(t.get("name") or "")[:200],
        "state": t.get("state"),
        "progress": round(float(t.get("progress") or 0), 4),
        "seeds": t.get("num_seeds"),
        "seeds_in_swarm": t.get("num_complete"),
        "availability": round(float(t.get("availability") or 0), 2),
        "size": t.get("size"),
        "category": t.get("category") or None,
        "inactive_s": max(int(now - last), 0),
        "dlspeed": t.get("dlspeed"),
    }


def is_stalled_candidate(row: dict[str, Any], stalled_after: float) -> bool:
    if row["state"] in _BROKEN:
        return True
    return (
        row["state"] in _ACTIVE_DOWNLOAD
        and row["progress"] < 1
        and row["inactive_s"] >= stalled_after
        and not row["dlspeed"]
    )


def build_findings(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Compact problem list for the report digest: [{severity: warn|crit, source, message}]."""
    out: list[dict[str, str]] = []
    for e in entries:
        source = f"{e['type']}/{e['name']}" if e["type"] != "qbittorrent" else "qbittorrent"
        if not e["ok"]:
            out.append(
                {"severity": "crit", "source": source, "message": f"unreachable: {e['error']}"}
            )
            continue
        for h in e.get("health", []):
            if h["level"] in ("error", "warning"):
                out.append(
                    {
                        "severity": "crit" if h["level"] == "error" else "warn",
                        "source": source,
                        "message": f"health: {h['message']}",
                    }
                )
        queue = e.get("queue")
        if queue and queue["problems"]:
            states: dict[str, int] = {}
            for row in queue["items"]:
                if row["problem"]:
                    key = str(row["tracked_state"] or row["status"])
                    states[key] = states.get(key, 0) + 1
            detail = ", ".join(f"{k}: {v}" for k, v in sorted(states.items()))
            out.append(
                {
                    "severity": "warn",
                    "source": source,
                    "message": f"{queue['problems']} queue item(s) with problems ({detail})",
                }
            )
        for ix in e.get("indexers", []):
            if ix["failing"]:
                out.append(
                    {
                        "severity": "warn",
                        "source": source,
                        "message": f"indexer {ix['name']} is failing "
                        f"(disabled until {ix['disabled_till']})",
                    }
                )
        if e.get("stalled_total"):
            out.append(
                {
                    "severity": "warn",
                    "source": source,
                    "message": f"{e['stalled_total']} stalled/broken torrent(s) "
                    f"(no progress for {e['stalled_after_s'] // 3600}h or in an error state)",
                }
            )
    return out[:_MAX_FINDINGS]


class ArrCollector(Collector):
    name = "arr"
    timeout = timedelta(seconds=90)
    cfg: ArrCollectorSettings

    def __init__(
        self,
        cfg: ArrCollectorSettings,
        env: HostEnv,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(cfg, env)
        self._transport = transport
        self._clients: Clients | None = None

    def _ensure(self) -> Clients:
        if self._clients is None:
            if self.app_settings is None:
                raise CollectorUnavailable("settings not available")
            self._clients = Clients(self.app_settings.arr, transport=self._transport)
        return self._clients

    async def check(self) -> None:
        if self._ensure().empty:
            raise CollectorUnavailable(
                "no *arr app, Prowlarr or qBittorrent configured (Settings → *arr & downloads)"
            )

    async def close(self) -> None:
        if self._clients is not None:
            await self._clients.close()

    async def collect(self) -> dict[str, Any]:
        assert self.app_settings is not None
        clients = self._ensure()
        stalled_after = self.app_settings.arr.actions.stalled_cleanup.stalled_after.total_seconds()
        jobs: list[Any] = [self._app(app) for app in clients.apps.values()]
        if clients.prowlarr:
            jobs.append(self._prowlarr(clients.prowlarr))
        if clients.qbit:
            jobs.append(self._qbit(clients.qbit, stalled_after))
        outcomes = await asyncio.gather(*jobs)
        if outcomes and all(unreachable for _, unreachable in outcomes):
            raise CollectorUnavailable(outcomes[0][0]["error"])
        entries = [entry for entry, _ in outcomes]
        apps = [e for e in entries if e["type"] in ("radarr", "sonarr", "whisparr")]
        prowlarr = next((e for e in entries if e["type"] == "prowlarr"), None)
        qbit = next((e for e in entries if e["type"] == "qbittorrent"), None)
        return {
            "summary": {
                "instances": len(entries),
                "unreachable": [f"{e['type']}/{e['name']}" for e in entries if not e["ok"]],
                "queue_total": sum(a.get("queue", {}).get("total", 0) for a in apps),
                "queue_problems": sum(a.get("queue", {}).get("problems", 0) for a in apps),
                "health_issues": sum(len(e.get("health", [])) for e in entries),
                "wanted_missing": sum(a.get("wanted", {}).get("missing") or 0 for a in apps),
                "wanted_cutoff_unmet": sum(
                    a.get("wanted", {}).get("cutoff_unmet") or 0 for a in apps
                ),
                "indexers_failing": (prowlarr or {}).get("summary", {}).get("failing", 0),
                "stalled_candidates": (qbit or {}).get("stalled_total", 0),
            },
            "findings": build_findings(entries),
            "apps": apps,
            "prowlarr": prowlarr,
            "qbittorrent": qbit,
        }

    # ------------------------------------------------------------------ per instance

    @staticmethod
    def _failed(kind: str, name: str, exc: ArrError) -> tuple[dict[str, Any], bool]:
        return {"type": kind, "name": name, "ok": False, "error": str(exc)}, exc.unreachable

    @staticmethod
    def _health(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [
            {
                "source": h.get("source"),
                "level": h.get("type"),
                "message": str(h.get("message") or "")[:300],
            }
            for h in raw or []
        ]

    async def _app(self, app: ArrApp) -> tuple[dict[str, Any], bool]:
        try:
            status = await app.status()
            health = await app.health()
            items, total = await app.queue(max_items=self.cfg.max_queue_items)
            missing = await app.wanted_total("missing")
            cutoff = await app.wanted_total("cutoff")
        except ArrError as exc:
            return self._failed(app.kind, app.name, exc)
        rows = [queue_row(i) for i in items]
        by_state: dict[str, int] = {}
        for row in rows:
            key = str(row["tracked_state"] or row["status"])
            by_state[key] = by_state.get(key, 0) + 1
        return {
            "type": app.kind,
            "name": app.name,
            "ok": True,
            "version": status.get("version"),
            "health": self._health(health),
            "queue": {
                "total": total,
                "listed": len(rows),
                "problems": sum(r["problem"] for r in rows),
                "by_state": by_state,
                "items": rows,
            },
            "wanted": {"missing": missing, "cutoff_unmet": cutoff},
        }, False

    async def _prowlarr(self, client: Prowlarr) -> tuple[dict[str, Any], bool]:
        try:
            status = await client.status()
            health = await client.health()
            indexers = await client.indexers()
            statuses = await client.indexer_status()
        except ArrError as exc:
            return self._failed("prowlarr", client.name, exc)
        now = time.time()
        by_id = {s.get("indexerId"): s for s in statuses or []}
        rows = []
        for ix in indexers or []:
            st = by_id.get(ix.get("id")) or {}
            until = parse_time(st.get("disabledTill"))
            rows.append(
                {
                    "id": ix.get("id"),
                    "name": ix.get("name"),
                    "protocol": ix.get("protocol"),
                    "enabled": bool(ix.get("enable")),
                    "failing": bool(until and until > now),
                    "disabled_till": st.get("disabledTill") if until and until > now else None,
                    "most_recent_failure": st.get("mostRecentFailure"),
                }
            )
        return {
            "type": "prowlarr",
            "name": client.name,
            "ok": True,
            "version": status.get("version"),
            "health": self._health(health),
            "indexers": rows,
            "summary": {
                "total": len(rows),
                "enabled": sum(r["enabled"] for r in rows),
                "failing": sum(r["failing"] for r in rows),
            },
        }, False

    async def _qbit(self, client: QBittorrent, stalled_after: float) -> tuple[dict[str, Any], bool]:
        try:
            version = await client.version()
            transfer = await client.transfer_info()
            torrents = await client.torrents()
        except ArrError as exc:
            return self._failed("qbittorrent", "qbittorrent", exc)
        now = time.time()
        rows = [torrent_row(t, now) for t in torrents]
        by_state: dict[str, int] = {}
        for row in rows:
            by_state[str(row["state"])] = by_state.get(str(row["state"]), 0) + 1
        stalled = [r for r in rows if is_stalled_candidate(r, stalled_after)]
        stalled.sort(key=lambda r: -r["inactive_s"])
        return {
            "type": "qbittorrent",
            "name": "qbittorrent",
            "ok": True,
            "version": version,
            "transfer": {
                "dl_speed": transfer.get("dl_info_speed"),
                "up_speed": transfer.get("up_info_speed"),
                "connection_status": transfer.get("connection_status"),
            },
            "summary": {"total": len(rows), "by_state": by_state},
            "stalled_after_s": int(stalled_after),
            "stalled_total": len(stalled),
            "stalled": stalled[:_MAX_STALLED],
        }, False

    def persist_view(self, data: dict[str, Any]) -> dict[str, Any]:
        apps = []
        for app in data.get("apps", []):
            queue = app.get("queue")
            slim = {k: v for k, v in app.items() if k != "queue"}
            if queue:
                problems = [
                    {k: r.get(k) for k in ("id", "title", "tracked_state", "messages")}
                    for r in queue["items"]
                    if r["problem"]
                ][:_PERSIST_PROBLEMS]
                slim["queue"] = {**{k: v for k, v in queue.items() if k != "items"}}
                slim["problem_items"] = problems
            apps.append(slim)
        qbit = data.get("qbittorrent")
        if qbit and qbit.get("ok"):
            qbit = {
                **{k: v for k, v in qbit.items() if k != "stalled"},
                "stalled": [
                    {k: r.get(k) for k in ("hash", "name", "state", "progress", "inactive_s")}
                    for r in qbit["stalled"][:_PERSIST_PROBLEMS]
                ],
            }
        return {**data, "apps": apps, "qbittorrent": qbit}
