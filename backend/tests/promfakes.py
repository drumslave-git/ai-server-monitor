"""A fake Prometheus and a scripted LLM for the Prometheus auto-query tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from .conftest import chat_response

PROM = "http://prom.test:9090"


def vector(*values: tuple[dict[str, str], str | float]) -> dict[str, Any]:
    return {
        "resultType": "vector",
        "result": [{"metric": m, "value": [1, str(v)]} for m, v in values],
    }


class FakeProm:
    """Targets, metadata, per-job metric names and canned query answers."""

    def __init__(self) -> None:
        self.targets: list[dict[str, Any]] = [
            {"labels": {"job": "node", "instance": "node-exporter:9100"}, "health": "up"},
            {"labels": {"job": "windows-pc", "instance": "192.168.88.30:9182"}, "health": "up"},
        ]
        self.metrics: dict[str, dict[str, int]] = {
            "node": {
                "node_network_receive_errs_total": 2,
                "node_pressure_cpu_waiting_seconds_total": 1,
                "node_cpu_seconds_total": 96,
                "node_memory_MemTotal_bytes": 1,
                "go_gc_duration_seconds": 5,
                "node_scrape_collector_duration_seconds": 40,
                "node_disk_io_time_seconds_bucket": 3,
                "up": 1,
            },
            "windows-pc": {
                "windows_gpu_temperature_celsius": 1,
                "windows_logical_disk_requests_queued": 3,
                "windows_service_state": 2268,
            },
        }
        self.meta: dict[str, list[dict[str, str]]] = {
            "node_network_receive_errs_total": [
                {"type": "counter", "help": "Network device statistic receive_errs."}
            ],
            "windows_gpu_temperature_celsius": [
                {"type": "gauge", "help": "GPU temperature in degrees Celsius."}
            ],
        }
        self.answers: dict[str, Any] = {}
        """query -> vector data | ("error", message) | callable(request)."""
        self.alerts: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.unreachable = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("refused")
        path = request.url.path
        self.calls.append(path)
        if path == "/api/v1/targets":
            return _ok({"activeTargets": self.targets})
        if path == "/api/v1/metadata":
            return _ok(self.meta)
        if path == "/api/v1/alerts":
            return _ok({"alerts": self.alerts})
        if path == "/api/v1/query":
            return self._query(request.url.params["query"])
        return httpx.Response(404, json={"status": "error", "error": "not found"})

    def _query(self, query: str) -> httpx.Response:
        if query.startswith("count by (__name__)"):
            job = query.split('job="')[1].split('"')[0]
            return _ok(
                vector(*[({"__name__": n}, c) for n, c in self.metrics.get(job, {}).items()])
            )
        self.calls.append(f"query:{query}")
        answer = self.answers.get(query)
        if answer is None:
            return _ok(vector())
        if isinstance(answer, tuple) and answer[0] == "error":
            return httpx.Response(
                400, json={"status": "error", "errorType": "bad_data", "error": answer[1]}
            )
        return _ok(answer)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


class FakeLLM:
    """Answers chat completions from a function of (messages, call number)."""

    def __init__(self, reply: Callable[[list[dict[str, Any]], int], str | httpx.Response]):
        self.reply = reply
        self.calls: list[list[dict[str, Any]]] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        messages = body["messages"]
        self.calls.append(messages)
        out = self.reply(messages, len(self.calls))
        return out if isinstance(out, httpx.Response) else chat_response(out)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def job_of(messages: list[dict[str, Any]]) -> str:
    """Which job's catalog the (first user) message carries."""
    text = next(m["content"] for m in messages if m["role"] == "user")
    return text.split("JOB ")[1].split(":")[0]


def plan(*items: dict[str, Any]) -> str:
    return json.dumps(list(items))
