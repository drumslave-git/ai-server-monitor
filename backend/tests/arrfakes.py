"""In-memory stand-ins for qBittorrent, Radarr, Sonarr and Prowlarr (httpx MockTransport).

Nothing here ever touches the network: tests must never reach real *arr/qBit instances.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ai_server_monitor.config import ArrSettings, Settings

HASH_A = "a" * 40
HASH_B = "b" * 40
API_KEY = "k3y-s3cret"
QBIT_PASSWORD = "pa55-s3cret"


def arr_settings(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "dry_run": False,
        "radarr": [{"name": "radarr", "url": "http://radarr.test:7878", "api_key": API_KEY}],
        "sonarr": [{"name": "sonarr", "url": "http://sonarr.test:8989", "api_key": API_KEY}],
        "whisparr": [{"name": "whisparr", "url": "http://whisparr.test:6969", "api_key": API_KEY}],
        "prowlarr": {"name": "prowlarr", "url": "http://prowlarr.test:9696", "api_key": API_KEY},
        "qbittorrent": {
            "url": "http://qbit.test:8080",
            "username": "admin",
            "password": QBIT_PASSWORD,
        },
    }
    base.update(overrides)
    return base


def make_settings(actions: dict[str, Any] | None = None, **arr: Any) -> Settings:
    cfg = arr_settings(**arr)
    if actions:
        cfg["actions"] = actions
    return Settings(arr=ArrSettings.model_validate(cfg))


# Query parameters the real apps accept (from Radarr/Sonarr/Whisparr openapi.json and source).
# Anything else is a bug in our client, so the fake answers 400 instead of ignoring it.
QUEUE_PARAMS = {
    "radarr": {"page", "pageSize", "sortKey", "sortDirection", "includeUnknownMovieItems"},
    "whisparr": {"page", "pageSize", "sortKey", "sortDirection", "includeUnknownMovieItems"},
    "sonarr": {"page", "pageSize", "sortKey", "sortDirection", "includeUnknownSeriesItems"},
}
WANTED_PARAMS = {"page", "pageSize", "sortKey", "sortDirection", "monitored"}
WANTED_SORT = {
    "radarr": "movies.lastSearchTime",
    "whisparr": "movies.lastSearchTime",
    "sonarr": "episodes.lastSearchTime",
}
QUEUE_DELETE_PARAMS = {"removeFromClient", "blocklist", "skipRedownload", "changeCategory"}
SEARCH_BODY = {
    "radarr": ("MoviesSearch", "movieIds"),
    "whisparr": ("MoviesSearch", "movieIds"),
    "sonarr": ("EpisodeSearch", "episodeIds"),
}
QBIT_INFO_PARAMS = {"filter", "category", "tag", "sort", "reverse", "limit", "offset", "hashes"}


def bad_request(message: str) -> httpx.Response:
    return httpx.Response(400, json={"message": f"fake: {message}"})


class FakeArrWorld:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.session_valid = False
        self.logins = 0
        self.torrents: dict[str, dict[str, Any]] = {}
        self.queue: dict[str, list[dict[str, Any]]] = {"radarr": [], "sonarr": [], "whisparr": []}
        # (app, "missing"|"cutoff") -> records, each with a lastSearchTime
        self.wanted: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.wanted_unsupported: set[str] = set()
        self.lookup_results: list[dict[str, Any]] = []
        self.added: list[dict[str, Any]] = []
        self.health: dict[str, list[dict[str, Any]]] = {}
        self.indexers: list[dict[str, Any]] = []
        self.indexer_status: list[dict[str, Any]] = []
        self.fail_hosts: set[str] = set()
        self.indexer_test_ok = True
        self.deny_arr_key = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls(self, host: str, method: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in self.requests
            if r["host"] == host and (method is None or r["method"] == method)
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        body = request.content.decode() if request.content else ""
        self.requests.append(
            {
                "host": host,
                "method": request.method,
                "path": request.url.path,
                "params": dict(request.url.params),
                "body": body,
                "headers": dict(request.headers),
            }
        )
        if host in self.fail_hosts:
            raise httpx.ConnectError("refused")
        if host == "qbit.test":
            return self._qbit(request, body)
        if host in ("radarr.test", "sonarr.test", "whisparr.test"):
            return self._arr(request, host.split(".")[0], body)
        if host == "prowlarr.test":
            return self._prowlarr(request, body)
        return httpx.Response(404)

    # ---------------------------------------------------------------- qBittorrent

    def _qbit(self, request: httpx.Request, body: str) -> httpx.Response:
        path = request.url.path
        if path == "/api/v2/auth/login":
            if QBIT_PASSWORD not in body:
                return httpx.Response(200, text="Fails.")
            self.session_valid = True
            self.logins += 1
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=abc; path=/"})
        if not self.session_valid:
            return httpx.Response(403, text="Forbidden")
        if path == "/api/v2/app/version":
            return httpx.Response(200, text="v5.0.1")
        if path == "/api/v2/transfer/info":
            return httpx.Response(
                200,
                json={"dl_info_speed": 1000, "up_info_speed": 50, "connection_status": "connected"},
            )
        if path == "/api/v2/torrents/info":
            unknown = set(request.url.params) - QBIT_INFO_PARAMS
            if unknown:
                return bad_request(f"unknown torrents/info params {unknown}")
            wanted = request.url.params.get("hashes")
            items = [t for h, t in self.torrents.items() if wanted in (None, h)]
            return httpx.Response(200, json=items)
        if path == "/api/v2/torrents/delete":
            for h in dict(p.split("=") for p in body.split("&"))["hashes"].split("%7C"):
                self.torrents.pop(h, None)
            return httpx.Response(200)
        return httpx.Response(404)

    # ---------------------------------------------------------------- *arr

    def _arr(self, request: httpx.Request, kind: str, body: str) -> httpx.Response:
        if request.headers.get("x-api-key") != API_KEY or self.deny_arr_key:
            return httpx.Response(401, json={"error": "Unauthorized"})
        path = request.url.path
        if path == "/api/v3/system/status":
            return httpx.Response(200, json={"version": "5.9.1"})
        if path == "/api/v3/health":
            return httpx.Response(200, json=self.health.get(kind, []))
        if path == "/api/v3/queue" and request.method == "GET":
            unknown = set(request.url.params) - QUEUE_PARAMS[kind]
            if unknown:
                return bad_request(f"unknown queue params {unknown}")
            records = self.queue[kind]
            page = int(request.url.params.get("page", 1))
            size = int(request.url.params.get("pageSize", 10))
            return httpx.Response(
                200,
                json={
                    "page": page,
                    "pageSize": size,
                    "totalRecords": len(records),
                    "records": records[(page - 1) * size : page * size],
                },
            )
        if path.startswith("/api/v3/queue/") and request.method == "DELETE":
            unknown = set(request.url.params) - QUEUE_DELETE_PARAMS
            if unknown:
                return bad_request(f"unknown queue delete params {unknown}")
            return httpx.Response(200)
        if path in ("/api/v3/wanted/missing", "/api/v3/wanted/cutoff"):
            if kind in self.wanted_unsupported:
                return httpx.Response(404, json={"message": "NotFound"})
            allowed = WANTED_PARAMS | ({"includeSeries"} if kind == "sonarr" else set())
            unknown = set(request.url.params) - allowed
            if unknown:
                return bad_request(f"unknown wanted params {unknown}")
            sort_key = request.url.params.get("sortKey")
            if sort_key and sort_key != WANTED_SORT[kind]:
                return bad_request(f"sortKey {sort_key} not allowed")
            records = list(self.wanted.get((kind, path.rsplit("/", 1)[1]), []))
            if sort_key:  # least recently searched first
                records.sort(key=lambda r: r.get("lastSearchTime") or "")
            page = int(request.url.params.get("page", 1))
            size = int(request.url.params.get("pageSize", 10))
            return httpx.Response(
                200,
                json={
                    "page": page,
                    "pageSize": size,
                    "totalRecords": len(records),
                    "records": records[(page - 1) * size : page * size],
                },
            )
        if path == "/api/v3/command" and request.method == "POST":
            command = json.loads(body)
            name, field = SEARCH_BODY[kind]
            if command.get("name") != name or not isinstance(command.get(field), list):
                return bad_request(f"{kind} expects {name} with {field}, got {command}")
            for records in self.wanted.values():  # a search updates lastSearchTime
                for record in records:
                    if record["id"] in command[field]:
                        record["lastSearchTime"] = "2999-01-01T00:00:00Z"
            return httpx.Response(201, json={"id": 99, **command})
        lookup = "/api/v3/series/lookup" if kind == "sonarr" else "/api/v3/movie/lookup"
        if path == lookup and request.method == "GET":
            if set(request.url.params) - {"term"}:
                return bad_request("lookup takes only term")
            return httpx.Response(200, json=self.lookup_results)
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 4, "name": "HD-1080p"}])
        if path == "/api/v3/rootfolder":
            return httpx.Response(200, json=[{"id": 1, "path": "/media/movies", "freeSpace": 10}])
        add_path = "/api/v3/series" if kind == "sonarr" else "/api/v3/movie"
        if path == add_path and request.method == "POST":
            added = json.loads(body)
            self.added.append(added)
            return httpx.Response(201, json={**added, "id": 501})
        return httpx.Response(404, json={"message": "NotFound"})

    # ---------------------------------------------------------------- Prowlarr

    def _prowlarr(self, request: httpx.Request, body: str) -> httpx.Response:
        if request.headers.get("x-api-key") != API_KEY:
            return httpx.Response(401, json={"error": "Unauthorized"})
        path = request.url.path
        if path == "/api/v1/system/status":
            return httpx.Response(200, json={"version": "2.1.0"})
        if path == "/api/v1/health":
            return httpx.Response(200, json=self.health.get("prowlarr", []))
        if path == "/api/v1/indexer":
            return httpx.Response(200, json=self.indexers)
        if path == "/api/v1/indexerstatus":
            return httpx.Response(200, json=self.indexer_status)
        if (
            path.startswith("/api/v1/indexer/")
            and request.method == "GET"
            and path != "/api/v1/indexer/test"
        ):
            wanted = int(path.rsplit("/", 1)[1])
            found = [i for i in self.indexers if i["id"] == wanted]
            return httpx.Response(200, json=found[0]) if found else httpx.Response(404)
        if path == "/api/v1/indexer/test":
            if self.indexer_test_ok:
                return httpx.Response(200, json={})
            return httpx.Response(
                400, json=[{"propertyName": "", "errorMessage": "Unable to connect to indexer"}]
            )
        return httpx.Response(404)


def torrent(
    h: str,
    name: str,
    state: str = "downloading",
    progress: float = 0.5,
    inactive_s: int = 0,
    dlspeed: int = 0,
) -> dict[str, Any]:
    return {
        "hash": h,
        "name": name,
        "state": state,
        "progress": progress,
        "num_seeds": 0,
        "num_complete": 0,
        "availability": 0.4,
        "size": 1000,
        "category": "radarr",
        "num_leechs": 0,
        "amount_left": 500,
        "eta": 8640000,
        "last_activity": int(time.time()) - inactive_s,
        "added_on": int(time.time()) - inactive_s - 100,
        "dlspeed": dlspeed,
    }
