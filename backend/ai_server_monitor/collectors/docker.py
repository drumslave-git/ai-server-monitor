"""Docker containers: state, health, restarts, resource usage, disk usage, log errors.

Talks to the Docker Engine API with GET requests only, over a unix socket or TCP (e.g. a
GET-only socket proxy such as tecnativa/docker-socket-proxy).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import timedelta
from typing import Any

import httpx

from ..config import DockerCollectorSettings
from .base import Collector, CollectorUnavailable, HostEnv, RateTracker, round_or_none

DEFAULT_SOCKET = "unix:///var/run/docker.sock"
_ERROR_LINE = re.compile(r"\b(error|exception|fatal|panic|traceback|critical)\b", re.IGNORECASE)
_NOT_ERROR = re.compile(r"\b(no|0|zero)\s+errors?\b|\berrors?[=:]\s*0\b", re.IGNORECASE)
_MAX_LOG_SAMPLES = 3


def demux_logs(raw: bytes) -> list[str]:
    """Split Docker's multiplexed log stream (non-TTY containers) into lines.

    Frame: [stream:1][0:3][size:4 big-endian][payload]. Falls back to raw text when the
    stream is not multiplexed (TTY containers).
    """
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00":
        chunks: list[bytes] = []
        pos = 0
        while pos + 8 <= len(raw):
            size = int.from_bytes(raw[pos + 4 : pos + 8], "big")
            chunks.append(raw[pos + 8 : pos + 8 + size])
            pos += 8 + size
        text = b"".join(chunks).decode("utf-8", "replace")
    else:
        text = raw.decode("utf-8", "replace")
    return [line for line in text.splitlines() if line.strip()]


def is_error_line(line: str) -> bool:
    return bool(_ERROR_LINE.search(line)) and not _NOT_ERROR.search(line)


def memory_usage(stats: dict[str, Any]) -> int | None:
    """Container memory excluding reclaimable page cache (matches `docker stats`)."""
    mem = stats.get("memory_stats") or {}
    usage = mem.get("usage")
    if usage is None:
        return None
    detail = mem.get("stats") or {}
    cache = detail.get("inactive_file")  # cgroup v2
    if cache is None:
        cache = detail.get("total_inactive_file", detail.get("cache", 0))  # cgroup v1
    return max(int(usage) - int(cache or 0), 0)


def blkio_bytes(stats: dict[str, Any]) -> tuple[int, int]:
    entries = (stats.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
    read = sum(e.get("value", 0) for e in entries if str(e.get("op", "")).lower() == "read")
    write = sum(e.get("value", 0) for e in entries if str(e.get("op", "")).lower() == "write")
    return read, write


class DockerAPI:
    def __init__(self, url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self.url = url
        if transport is None:
            if url.startswith("unix://"):
                transport = httpx.AsyncHTTPTransport(uds=url.removeprefix("unix://"))
                base = "http://docker"
            elif url.startswith(("tcp://", "http://", "https://")):
                base = "http://" + url.removeprefix("tcp://") if url.startswith("tcp://") else url
            else:
                raise ValueError(f"unsupported docker url: {url}")
        else:
            base = "http://docker"
        self._http = httpx.AsyncClient(
            base_url=base, transport=transport, timeout=httpx.Timeout(30.0, connect=5.0)
        )

    async def get(self, path: str, **params: Any) -> Any:
        try:
            response = await self._http.get(path, params=params or None)
        except httpx.TransportError as exc:
            raise CollectorUnavailable(f"docker API unreachable at {self.url}: {exc}") from exc
        if response.status_code == 403:
            raise PermissionError(f"docker API denied {path} (socket proxy permissions?)")
        response.raise_for_status()
        return (
            response.json()
            if "json" in response.headers.get("content-type", "")
            else (response.content)
        )

    async def close(self) -> None:
        await self._http.aclose()


class DockerCollector(Collector):
    name = "docker"
    timeout = timedelta(seconds=90)
    cfg: DockerCollectorSettings

    def __init__(
        self,
        cfg: DockerCollectorSettings,
        env: HostEnv,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        super().__init__(cfg, env)
        url = cfg.url or os.environ.get("DOCKER_HOST") or DEFAULT_SOCKET
        if url.startswith("unix://") and transport is None:
            path = url.removeprefix("unix://")
            if not os.path.exists(path) and env.in_container:
                alt = env.path(path)
                if alt.exists():
                    url = f"unix://{alt}"
        self.api = DockerAPI(url, transport=transport)
        self._rates = RateTracker()
        self._prev_cpu: dict[str, tuple[int, int]] = {}
        self._prev_restarts: dict[str, tuple[int, str]] = {}
        self._log_since: dict[str, int] = {}
        self._df: dict[str, Any] | None = None
        self._df_ts = 0.0
        self._sem = asyncio.Semaphore(8)

    async def check(self) -> None:
        await self.api.get("/_ping")

    async def close(self) -> None:
        await self.api.close()

    async def collect(self) -> dict[str, Any]:
        info = await self.api.get("/info")
        listing = await self.api.get("/containers/json", all="true")
        containers = await asyncio.gather(*(self._container(c) for c in listing))
        containers.sort(key=lambda c: (c["state"] != "running", c["name"]))
        ids = {c["id"] for c in containers}
        for store in (self._prev_cpu, self._prev_restarts, self._log_since):
            for key in list(store):
                if key not in ids:
                    del store[key]
        await self._maybe_refresh_df()
        summary = {
            "total": len(containers),
            "running": sum(c["state"] == "running" for c in containers),
            "unhealthy": [c["name"] for c in containers if c.get("health") == "unhealthy"],
            "restarting": [c["name"] for c in containers if c["state"] == "restarting"],
            "exited_with_error": [
                c["name"]
                for c in containers
                if c["state"] == "exited" and c.get("exit_code") not in (0, None)
            ],
            "restarted_recently": [c["name"] for c in containers if c.get("restarts_delta")],
            "oom_killed": [c["name"] for c in containers if c.get("oom_killed")],
        }
        return {
            "engine": {
                "version": info.get("ServerVersion"),
                "storage_driver": info.get("Driver"),
                "images": info.get("Images"),
                "warnings": info.get("Warnings") or [],
            },
            "summary": summary,
            "containers": containers,
            "disk_usage": self._df,
        }

    async def _container(self, entry: dict[str, Any]) -> dict[str, Any]:
        async with self._sem:
            cid = entry["Id"]
            short = cid[:12]
            labels = entry.get("Labels") or {}
            state = entry.get("State", "unknown")
            row: dict[str, Any] = {
                "id": short,
                "name": (entry.get("Names") or ["/" + short])[0].lstrip("/"),
                "image": entry.get("Image"),
                "image_id": entry.get("ImageID"),
                "state": state,
                "status": entry.get("Status"),
                "compose_project": labels.get("com.docker.compose.project"),
                "compose_service": labels.get("com.docker.compose.service"),
            }
            try:
                inspect = await self.api.get(f"/containers/{cid}/json")
            except httpx.HTTPStatusError:  # removed between list and inspect
                return row
            st = inspect.get("State") or {}
            restarts = int(inspect.get("RestartCount") or 0)
            started_at = st.get("StartedAt") or ""
            prev = self._prev_restarts.get(short)
            self._prev_restarts[short] = (restarts, started_at)
            row.update(
                {
                    # Health of a stopped container is stale (last check before it stopped).
                    "health": (st.get("Health") or {}).get("Status")
                    if state == "running"
                    else None,
                    "restart_count": restarts,
                    "restarts_delta": max(restarts - prev[0], 0) if prev else 0,
                    "restarted_since_last": bool(prev and prev[1] != started_at),
                    "started_at": started_at,
                    "oom_killed": bool(st.get("OOMKilled")),
                    "exit_code": st.get("ExitCode") if state != "running" else None,
                    "restart_policy": (
                        (inspect.get("HostConfig") or {}).get("RestartPolicy") or {}
                    ).get("Name"),
                }
            )
            if state == "running":
                await self._add_stats(cid, row)
            if self.cfg.log_scan and state in ("running", "restarting", "exited"):
                tty = bool((inspect.get("Config") or {}).get("Tty"))
                await self._add_log_errors(cid, row, tty)
            return row

    async def _add_stats(self, cid: str, row: dict[str, Any]) -> None:
        try:
            stats = await self.api.get(f"/containers/{cid}/stats", stream="false", **{
                "one-shot": "true"})  # fmt: skip
        except (httpx.HTTPStatusError, httpx.TimeoutException):
            return
        short = row["id"]
        now = time.monotonic()
        cpu = stats.get("cpu_stats") or {}
        total = int((cpu.get("cpu_usage") or {}).get("total_usage") or 0)
        system = int(cpu.get("system_cpu_usage") or 0)
        online = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get(
            "percpu_usage") or []) or 1  # fmt: skip
        prev = self._prev_cpu.get(short)
        self._prev_cpu[short] = (total, system)
        cpu_pct = None
        if prev and system > prev[1] and total >= prev[0]:
            cpu_pct = (total - prev[0]) / (system - prev[1]) * online * 100
        mem = memory_usage(stats)
        limit = (stats.get("memory_stats") or {}).get("limit")
        nets = stats.get("networks") or {}
        rx = sum(n.get("rx_bytes", 0) for n in nets.values())
        tx = sum(n.get("tx_bytes", 0) for n in nets.values())
        read, write = blkio_bytes(stats)
        row.update(
            {
                "cpu_percent": round_or_none(cpu_pct),
                "mem_usage": mem,
                "mem_limit": limit,
                "mem_percent": round(mem / limit * 100, 1) if mem is not None and limit else None,
                "net_rx_bps": round_or_none(self._rates.rate(f"{short}:rx", rx, now), 0),
                "net_tx_bps": round_or_none(self._rates.rate(f"{short}:tx", tx, now), 0),
                "blk_read_bps": round_or_none(self._rates.rate(f"{short}:br", read, now), 0),
                "blk_write_bps": round_or_none(self._rates.rate(f"{short}:bw", write, now), 0),
                "pids": (stats.get("pids_stats") or {}).get("current"),
            }
        )

    async def _add_log_errors(self, cid: str, row: dict[str, Any], tty: bool) -> None:
        short = row["id"]
        now = int(time.time())
        since = self._log_since.get(short, now - int(self.cfg.interval.total_seconds()))
        self._log_since[short] = now
        try:
            raw = await self.api.get(
                f"/containers/{cid}/logs",
                stdout="1",
                stderr="1",
                since=str(since),
                tail=str(self.cfg.log_tail),
            )
        except (httpx.HTTPStatusError, httpx.TimeoutException, PermissionError):
            return
        if not isinstance(raw, bytes | bytearray):
            return
        lines = raw.decode("utf-8", "replace").splitlines() if tty else demux_logs(bytes(raw))
        errors = [line for line in lines if is_error_line(line)]
        row["log_lines"] = len(lines)
        row["log_errors"] = len(errors)
        row["log_error_samples"] = [line[:300] for line in errors[-_MAX_LOG_SAMPLES:]]

    async def _maybe_refresh_df(self) -> None:
        if (
            self._df is not None
            and time.time() - self._df_ts < self.cfg.df_interval.total_seconds()
        ):
            return
        try:
            df = await self.api.get("/system/df")
        except (httpx.HTTPError, PermissionError):
            return
        images = df.get("Images") or []
        volumes = df.get("Volumes") or []
        cache = df.get("BuildCache") or []
        containers = df.get("Containers") or []
        self._df = {
            "images_bytes": sum(i.get("Size", 0) for i in images),
            "images_unused_bytes": sum(i.get("Size", 0) for i in images if not i.get("Containers")),
            "images_count": len(images),
            "containers_rw_bytes": sum(c.get("SizeRw", 0) or 0 for c in containers),
            "volumes_bytes": sum((v.get("UsageData") or {}).get("Size", 0) or 0 for v in volumes),
            "volumes_count": len(volumes),
            "build_cache_bytes": sum(c.get("Size", 0) for c in cache),
            "largest_volumes": sorted(
                (
                    {"name": v.get("Name"), "bytes": (v.get("UsageData") or {}).get("Size", 0)}
                    for v in volumes
                ),
                key=lambda v: v["bytes"] or 0,
                reverse=True,
            )[:5],
            "ts": time.time(),
        }
        self._df_ts = time.time()

    def persist_view(self, data: dict[str, Any]) -> dict[str, Any]:
        keep = ("id", "name", "image", "state", "health", "restart_count", "cpu_percent",
                "mem_usage", "mem_percent", "log_errors", "oom_killed", "exit_code")  # fmt: skip
        return {
            **data,
            "containers": [{k: c.get(k) for k in keep} for c in data.get("containers", [])],
        }
