"""A fake home server for the generic API access tests: Radarr (big library), Sonarr (queue
with problems), Prowlarr, qBittorrent, Docker and Prometheus behind one httpx MockTransport.

Nothing here touches the network. Every request is recorded; writes are listed in `mutations`
so a test can prove that something was (not) executed.
"""

from __future__ import annotations

import json
import struct
from typing import Any

import httpx

from ai_server_monitor.config import Settings

from .arrfakes import API_KEY, QBIT_PASSWORD, arr_settings

PROM_TOKEN = "prom-bearer-s3cret"
DOCKER_ENV_SECRET = "hunter2-db-pass"
DOWNLOAD_CLIENT_KEY = "dlclient-apikey-9999"
BIG = 3000  # movies in the fake library


def api_settings(**arr: Any) -> Settings:
    return Settings.model_validate(
        {
            "arr": arr_settings(**arr),
            "collectors": {
                "docker": {"url": "tcp://docker.test:2375"},
                "prometheus": {"url": "http://prom.test:9090", "token": PROM_TOKEN},
            },
        }
    )


def movie(i: int) -> dict[str, Any]:
    return {
        "id": i,
        "title": f"Movie {i:04d}",
        "year": 1950 + i % 70,
        "overview": "A long plot. " * 60,
        "monitored": i % 3 != 0,
        "hasFile": i % 2 == 0,
        "images": [{"coverType": "poster", "url": f"/MediaCover/{i}/poster.jpg"}] * 4,
        "remotePoster": f"https://image.tmdb.org/t/p/original/{i}.jpg",
        "alternateTitles": [{"title": f"Alt {n}"} for n in range(25)],
        "ratings": {"imdb": {"votes": 10, "value": 7.1}},
        "genres": ["Drama"],
        "tags": [],
        "path": f"/movies/Movie {i}",
        "folder": None,
    }


def docker_log_frames(lines: list[str]) -> bytes:
    out = b""
    for line in lines:
        data = (line + "\n").encode()
        out += bytes([1, 0, 0, 0]) + struct.pack(">I", len(data)) + data
    return out


class FakeServices:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.mutations: list[dict[str, Any]] = []
        self.movies = [movie(i) for i in range(1, BIG + 1)]
        self.missing = [
            {"id": i, "title": f"Missing {i:03d}", "year": 2000 + i % 20} for i in range(1, 251)
        ]
        self.missing_by_kind: dict[str, list[dict[str, Any]]] = {
            "radarr": self.missing,
            "whisparr": [],
        }
        self.sonarr_queue = [
            {
                "id": 1,
                "title": "Show.S01E01.1080p",
                "status": "completed",
                "trackedDownloadStatus": "warning",
                "trackedDownloadState": "importBlocked",
                "statusMessages": [
                    {"title": "Show.S01E01", "messages": ["No files found are eligible for import"]}
                ],
            },
            {
                "id": 2,
                "title": "Show.S01E02.1080p",
                "status": "downloading",
                "trackedDownloadStatus": "ok",
                "trackedDownloadState": "downloading",
                "statusMessages": [],
            },
        ]
        self.indexers = [
            {"id": 1, "name": "GoodIndexer", "enable": True, "protocol": "torrent"},
            {"id": 2, "name": "BadIndexer", "enable": True, "protocol": "torrent"},
        ]
        self.indexer_status = [
            {
                "indexerId": 2,
                "disabledTill": "2026-11-16T10:00:00Z",
                "mostRecentFailure": "2026-11-15T09:00:00Z",
            }
        ]
        self.session_valid = False
        self.logins = 0
        self.rate_headers = False

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
        record = {
            "host": host,
            "method": request.method,
            "path": request.url.path,
            "params": list(request.url.params.multi_items()),
            "body": body,
            "headers": dict(request.headers),
            "url": str(request.url),
        }
        self.requests.append(record)
        if request.method not in ("GET", "HEAD") and request.url.path != "/api/v2/auth/login":
            self.mutations.append(record)
        if host in ("radarr.test", "sonarr.test", "whisparr.test"):
            return self._arr(request, host.split(".")[0], body)
        if host == "prowlarr.test":
            return self._prowlarr(request)
        if host == "qbit.test":
            return self._qbit(request, body)
        if host == "docker.test":
            return self._docker(request)
        if host == "prom.test":
            return self._prom(request)
        return httpx.Response(404)

    # ------------------------------------------------------------------ *arr

    def _arr(self, request: httpx.Request, kind: str, body: str) -> httpx.Response:
        if request.headers.get("x-api-key") != API_KEY:
            return httpx.Response(401, json={"error": "Unauthorized"})
        path = request.url.path
        params = request.url.params
        if path == "/api/v3/movie" and request.method == "GET" and kind == "radarr":
            return httpx.Response(200, json=self.movies)
        if path == "/api/v3/wanted/missing" and kind in self.missing_by_kind:
            records = self.missing_by_kind[kind]
            page = int(params.get("page", 1))
            size = int(params.get("pageSize", 10))
            return httpx.Response(
                200,
                json={
                    "page": page,
                    "pageSize": size,
                    "totalRecords": len(records),
                    "records": records[(page - 1) * size : page * size],
                },
            )
        if path == "/api/v3/queue" and kind == "sonarr":
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "pageSize": 50,
                    "totalRecords": len(self.sonarr_queue),
                    "records": self.sonarr_queue,
                },
            )
        if path == "/api/v3/downloadclient":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "name": "qBittorrent",
                        "fields": [
                            {"name": "host", "value": "qbit"},
                            {"name": "password", "value": "pw-in-field-value"},
                            {"name": "apiKey", "value": DOWNLOAD_CLIENT_KEY, "privacy": "apiKey"},
                        ],
                    }
                ],
            )
        if path == "/api/v3/health":
            return httpx.Response(
                200,
                json=[
                    {
                        "source": "DownloadClientCheck",
                        "type": "error",
                        "message": f"Unable to connect (key {API_KEY})",
                    }
                ],
            )
        if path == "/api/v3/movie/999":
            return httpx.Response(404, json={"message": "NotFound"})
        if path == "/api/v3/release":
            return httpx.Response(200, json=[{"title": "Some.Release"}])
        if request.method in ("POST", "PUT", "DELETE"):
            return httpx.Response(201, json={"id": 77, "echo": json.loads(body) if body else None})
        return httpx.Response(404, json={"message": "NotFound"})

    def _prowlarr(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("x-api-key") != API_KEY:
            return httpx.Response(401, json={"error": "Unauthorized"})
        path = request.url.path
        if path == "/api/v1/indexer":
            return httpx.Response(200, json=self.indexers)
        if path == "/api/v1/indexerstatus":
            return httpx.Response(200, json=self.indexer_status)
        if path == "/api/v1/search":
            return httpx.Response(200, json=[{"title": "ubuntu.iso"}])
        return httpx.Response(404, json={"message": "NotFound"})

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
        if path == "/api/v2/torrents/info":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": "a" * 40,
                        "name": "Big.Movie.2024",
                        "state": "downloading",
                        "progress": 0.42,
                        "dlspeed": 1_500_000,
                        "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 300,
                        "save_path": "/downloads",
                    }
                ],
            )
        if path == "/api/v2/app/version":
            return httpx.Response(200, text="v5.0.1")
        if path == "/api/v2/torrents/pause":
            return httpx.Response(200)
        return httpx.Response(404)

    def _docker(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method != "GET":
            return httpx.Response(403, text="forbidden by proxy")
        if path == "/containers/json":
            return httpx.Response(
                200,
                json=[
                    {"Id": "abc", "Names": ["/web"], "State": "running", "Image": "nginx"},
                    {"Id": "def", "Names": ["/db"], "State": "exited", "Image": "postgres"},
                ],
            )
        if path == "/containers/web/json":
            return httpx.Response(
                200,
                json={
                    "Name": "/web",
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": "/srv/web",
                            "Destination": "/usr/share",
                            "RW": True,
                        }
                    ],
                    "Config": {"Env": ["PATH=/usr/bin", f"DB_PASSWORD={DOCKER_ENV_SECRET}"]},
                },
            )
        if path == "/containers/web/logs":
            lines = [f"line {i}" for i in range(1, 121)]
            return httpx.Response(
                200,
                content=docker_log_frames(lines),
                headers={"content-type": "application/vnd.docker.multiplexed-stream"},
            )
        return httpx.Response(404, json={"message": "no such container"})

    def _prom(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {PROM_TOKEN}":
            return httpx.Response(401, text="unauthorized")
        if request.url.path == "/api/v1/query":
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{"metric": {"job": "node"}, "value": [1700000000, "1"]}],
                    },
                },
            )
        return httpx.Response(404, json={"status": "error", "error": "not found"})
