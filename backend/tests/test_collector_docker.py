from __future__ import annotations

import struct
from typing import Any

import httpx
import pytest

from servermon.collectors.base import CollectorUnavailable, HostEnv
from servermon.collectors.docker import (
    DockerCollector,
    blkio_bytes,
    demux_logs,
    is_error_line,
    memory_usage,
)
from servermon.config import DockerCollectorConfig

WEB_ID = "a" * 64
DB_ID = "b" * 64
OLD_ID = "c" * 64


def frame(stream: int, text: str) -> bytes:
    payload = text.encode()
    return struct.pack(">BxxxL", stream, len(payload)) + payload


def test_demux_logs() -> None:
    raw = frame(1, "line one\n") + frame(2, "ERROR boom\nsecond\n")
    assert demux_logs(raw) == ["line one", "ERROR boom", "second"]
    assert demux_logs(b"plain tty output\nmore\n") == ["plain tty output", "more"]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("2026-01-01 ERROR could not connect", True),
        ("level=error msg=failed", True),
        ("Traceback (most recent call last):", True),
        ("panic: runtime error", True),
        ("completed with 0 errors", False),
        ("no errors found", False),
        ("errors=0 warnings=2", False),
        ("terrorist is not a word we match", False),
        ("INFO all good", False),
    ],
)
def test_is_error_line(line: str, expected: bool) -> None:
    assert is_error_line(line) is expected


def test_memory_usage_cgroup_v1_and_v2() -> None:
    v2 = {"memory_stats": {"usage": 1000, "stats": {"inactive_file": 300}}}
    v1 = {"memory_stats": {"usage": 1000, "stats": {"total_inactive_file": 100}}}
    assert memory_usage(v2) == 700
    assert memory_usage(v1) == 900
    assert memory_usage({}) is None


def test_blkio_bytes_case_insensitive() -> None:
    stats = {
        "blkio_stats": {
            "io_service_bytes_recursive": [
                {"op": "read", "value": 10},
                {"op": "Write", "value": 5},
                {"op": "Read", "value": 1},
            ]
        }
    }
    assert blkio_bytes(stats) == (11, 5)


class FakeDocker:
    def __init__(self) -> None:
        self.cpu_total = 1_000_000_000
        self.system = 100_000_000_000
        self.web_restarts = 0
        self.web_started = "2026-11-14T10:00:00Z"
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path == "/_ping":
            return httpx.Response(200, text="OK")
        if path == "/info":
            return httpx.Response(
                200,
                json={
                    "ServerVersion": "29.0",
                    "Driver": "overlay2",
                    "Images": 12,
                    "Warnings": None,
                },
            )
        if path == "/containers/json":
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": WEB_ID,
                        "Names": ["/web"],
                        "Image": "nginx:1.27",
                        "ImageID": "sha256:1",
                        "State": "running",
                        "Status": "Up 2 hours",
                        "Labels": {"com.docker.compose.project": "site"},
                    },
                    {
                        "Id": DB_ID,
                        "Names": ["/db"],
                        "Image": "postgres:17",
                        "State": "running",
                        "Status": "Up 2 hours (unhealthy)",
                        "Labels": {},
                    },
                    {
                        "Id": OLD_ID,
                        "Names": ["/old"],
                        "Image": "busybox",
                        "State": "exited",
                        "Status": "Exited (137)",
                        "Labels": {},
                    },
                ],
            )
        if path.endswith("/json"):
            cid = path.split("/")[2]
            state: dict[str, Any] = {
                "StartedAt": "2026-11-14T09:00:00Z",
                "OOMKilled": False,
                "ExitCode": 0,
            }
            restarts = 0
            if cid == WEB_ID:
                state["StartedAt"] = self.web_started
                restarts = self.web_restarts
            if cid == DB_ID:
                state["Health"] = {"Status": "unhealthy"}
            if cid == OLD_ID:  # stale health from before it stopped must be ignored
                state.update(
                    {"ExitCode": 137, "OOMKilled": True, "Health": {"Status": "unhealthy"}}
                )
            return httpx.Response(
                200,
                json={
                    "State": state,
                    "RestartCount": restarts,
                    "Config": {"Tty": cid == DB_ID},
                    "HostConfig": {"RestartPolicy": {"Name": "always"}},
                },
            )
        if path.endswith("/stats"):
            assert request.url.params["one-shot"] == "true"
            return httpx.Response(
                200,
                json={
                    "cpu_stats": {
                        "cpu_usage": {"total_usage": self.cpu_total},
                        "system_cpu_usage": self.system,
                        "online_cpus": 4,
                    },
                    "memory_stats": {
                        "usage": 200 * 2**20,
                        "limit": 1024 * 2**20,
                        "stats": {"inactive_file": 72 * 2**20},
                    },
                    "networks": {"eth0": {"rx_bytes": 1000, "tx_bytes": 2000}},
                    "pids_stats": {"current": 7},
                },
            )
        if path.endswith("/logs"):
            cid = path.split("/")[2]
            if cid == DB_ID:  # TTY container: raw text
                return httpx.Response(
                    200,
                    content=b"FATAL: password authentication failed\n",
                    headers={"content-type": "application/vnd.docker.raw-stream"},
                )
            body = frame(1, "GET / 200\n") + frame(2, "error: upstream timed out\n")
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "application/vnd.docker.multiplexed-stream"},
            )
        if path == "/system/df":
            return httpx.Response(
                200,
                json={
                    "Images": [{"Size": 100, "Containers": 1}, {"Size": 50, "Containers": 0}],
                    "Containers": [{"SizeRw": 5}],
                    "Volumes": [{"Name": "pgdata", "UsageData": {"Size": 900}}],
                    "BuildCache": [{"Size": 30}],
                },
            )
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def fake_docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
async def collector(fake_docker: FakeDocker):  # type: ignore[no-untyped-def]
    c = DockerCollector(
        DockerCollectorConfig(), HostEnv(), transport=httpx.MockTransport(fake_docker.handler)
    )
    yield c
    await c.close()


async def test_collect(collector: DockerCollector, fake_docker: FakeDocker) -> None:
    await collector.check()
    first = await collector.collect()

    assert first["engine"]["version"] == "29.0"
    names = [c["name"] for c in first["containers"]]
    assert names == ["db", "web", "old"]  # running first, then by name
    web = next(c for c in first["containers"] if c["name"] == "web")
    assert web["compose_project"] == "site"
    assert web["mem_usage"] == 128 * 2**20 and web["mem_percent"] == 12.5
    assert web["cpu_percent"] is None  # needs two samples
    assert web["log_errors"] == 1 and "upstream timed out" in web["log_error_samples"][0]
    db = next(c for c in first["containers"] if c["name"] == "db")
    assert db["log_errors"] == 1  # TTY stream parsed as raw text
    summary = first["summary"]
    assert summary["unhealthy"] == ["db"]
    assert summary["exited_with_error"] == ["old"]
    assert summary["oom_killed"] == ["old"]
    assert first["disk_usage"]["images_unused_bytes"] == 50
    assert first["disk_usage"]["largest_volumes"][0] == {"name": "pgdata", "bytes": 900}

    # Second run: 1 CPU-second used out of 10 system-seconds on 4 CPUs → 40%; web restarted.
    fake_docker.cpu_total += 1_000_000_000
    fake_docker.system += 10_000_000_000
    fake_docker.web_restarts = 2
    fake_docker.web_started = "2026-11-14T11:00:00Z"
    second = await collector.collect()
    web = next(c for c in second["containers"] if c["name"] == "web")
    assert web["cpu_percent"] == 40.0
    assert web["restarts_delta"] == 2 and web["restarted_since_last"] is True
    assert second["summary"]["restarted_recently"] == ["web"]
    assert fake_docker.requests.count("/system/df") == 1  # cached for df_interval

    slim = collector.persist_view(second)
    assert "log_error_samples" not in slim["containers"][0]


async def test_unreachable_socket_is_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no such file")

    c = DockerCollector(DockerCollectorConfig(), HostEnv(), transport=httpx.MockTransport(refuse))
    with pytest.raises(CollectorUnavailable):
        await c.check()
    await c.close()


def test_url_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
    c = DockerCollector(DockerCollectorConfig(), HostEnv())
    assert c.api.url == "tcp://proxy:2375"
    assert str(c.api._http.base_url).startswith("http://proxy:2375")
    explicit = DockerCollector(DockerCollectorConfig(url="tcp://other:2375"), HostEnv())
    assert explicit.api.url == "tcp://other:2375"
