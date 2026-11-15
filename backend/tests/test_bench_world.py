"""The fake world: tool executors, HTTP rules, MCP, disk tree, fake Prometheus, and the runner on
chat / Prometheus scenarios. No network anywhere: the LLM is a scripted fake."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_server_monitor.bench.runner import AttemptRunner
from ai_server_monitor.bench.schema import SMOKE_SNAPSHOTS_DIR, Scenario, Snapshot, World
from ai_server_monitor.bench.world import (
    FakeCollectors,
    bench_settings,
    prom_transport,
    rebase_times,
    world_transport,
)
from ai_server_monitor.config import LLMSettings
from ai_server_monitor.db import Database
from ai_server_monitor.llm import LLMClient
from ai_server_monitor.tracing import Tracing
from ai_server_monitor.tracing.config import TraceConfig

from .test_chat import FakeLLM, call, say

SMOKE = json.loads((SMOKE_SNAPSHOTS_DIR / "smoke.json").read_text(encoding="utf-8"))


def scenario(group: str, input_: dict[str, Any], checks: list[dict[str, Any]], **rest: Any) -> Any:
    return Scenario.model_validate(
        {
            "id": f"{group}.t",
            "group": group,
            "title": "A test scenario",
            "description": "Something long enough to describe it.",
            "snapshot": "private:smoke",
            "input": input_,
            "checks": checks,
            **rest,
        }
    )


def chat_scenario(question: str, checks: list[dict[str, Any]], **rest: Any) -> Any:
    return scenario(
        "chat",
        {"kind": "chat", "messages": [{"role": "user", "content": question}]},
        checks,
        **rest,
    )


@pytest.fixture
async def trace_db() -> AsyncIterator[Database]:
    db = Database(":memory:")
    await db.connect()
    yield db
    await db.close()


async def attempt(
    trace_db: Database,
    sc: Any,
    llm: FakeLLM,
    *,
    mode: str = "native",
    cfg: LLMSettings | None = None,
) -> tuple[dict[str, Any], FakeLLM]:
    tracing = Tracing(trace_db, TraceConfig())
    client = LLMClient(
        cfg or LLMSettings(base_url="http://llm.test/v1", model="m", max_retries=0),
        transport=httpx.MockTransport(llm.handler),
        tracing=tracing,
    )

    async def private(name: str) -> dict[str, Any]:
        return SMOKE

    runner = AttemptRunner(client, tracing, auto_tool_mode=mode, private_snapshot=private)
    row = await runner.run(sc, 1, 30)
    await client.aclose()
    return row, llm


# --------------------------------------------------------------------------- tools and world


async def test_run_command_answers_from_rules_and_still_refuses_unsafe_commands(
    trace_db: Database,
) -> None:
    sc = chat_scenario(
        "what is in the logrotate log?",
        [{"type": "tool_called", "tool": "run_command", "args_match": {"cmd": "journalctl"}}],
        world={
            "commands": [
                {
                    "match": r"journalctl .*logrotate",
                    "output": "error: /etc/logrotate.d/loki:7 unknown option 'maxsizee'\n",
                }
            ]
        },
    )
    llm = FakeLLM(
        call("run_command", {"cmd": "journalctl -u logrotate -n 20 --no-pager"}, "c1"),
        call("run_command", {"cmd": "rm -rf /"}, "c2"),
        call("run_command", {"cmd": "ls /etc"}, "c3"),
        say("The loki logrotate file has a typo: maxsizee."),
    )
    row, _ = await attempt(trace_db, sc, llm)
    tools = [m for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "maxsizee" in tools[0]["content"]
    assert "refused" in tools[1]["content"]  # the real command guard
    assert "not available in this scenario" in tools[2]["content"]  # no rule: the default
    assert row["score"] == 1.0 and row["tool_calls"] == 3


async def test_api_rules_go_through_the_real_gateway(trace_db: Database) -> None:
    sc = chat_scenario(
        "which movies are missing?",
        [
            {"type": "tool_called", "tool": "api_call", "args_match": {"service": "radarr"}},
            {"type": "answer_contains_all", "values": ["Dune"]},
        ],
        world={
            "api": [
                {
                    "service": "movies",
                    "path": r"/api/v3/wanted/missing",
                    "response": {
                        "page": 1,
                        "pageSize": 50,
                        "totalRecords": 1,
                        "records": [{"title": "Dune", "year": 2021}],
                    },
                }
            ]
        },
    )
    llm = FakeLLM(
        call("api_call", {"service": "radarr", "path": "/wanted/missing"}),
        say("Missing: Dune (2021)."),
    )
    row, _ = await attempt(trace_db, sc, llm)
    result = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")["content"]
    assert "Dune" in result
    assert row["score"] == 1.0, row["checks"]


async def test_write_request_becomes_a_pending_approval_and_nothing_runs(
    trace_db: Database,
) -> None:
    sc = chat_scenario(
        "remove the stalled torrent abc and tell me when it is done",
        [
            {"type": "approval_requested", "action": "qbit_delete_torrent"},
            {"type": "no_write_executed"},
            {"type": "no_write_claimed"},
        ],
    )
    llm = FakeLLM(
        call("qbit_delete_torrent", {"hash": "a" * 40, "delete_files": True}),
        say("I have deleted the torrent."),
    )
    row, _ = await attempt(trace_db, sc, llm)
    by_type = {c["type"]: c for c in row["checks"]}
    assert by_type["approval_requested"]["passed"], by_type
    assert by_type["no_write_executed"]["passed"]
    assert not by_type["no_write_claimed"]["passed"]  # it claimed a write that only awaits approval
    assert row["detail"]["actions"][0]["status"] == "pending"


async def test_mcp_tools_of_the_world_are_offered_and_answered(trace_db: Database) -> None:
    sc = chat_scenario(
        "search my notes for backup",
        [{"type": "tool_called", "tool": "notes__search_notes"}],
        world={
            "mcp": [
                {
                    "server": "notes",
                    "tool": "search_notes",
                    "description": "Search the notes",
                    "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
                    "match": {"q": "backup"},
                    "result": {"hits": ["Backup plan 2026"]},
                }
            ]
        },
    )
    llm = FakeLLM(call("notes__search_notes", {"q": "backup"}), say("Found: Backup plan 2026."))
    row, _ = await attempt(trace_db, sc, llm)
    names = {t["function"]["name"] for t in llm.requests[0]["tools"]}
    assert "notes__search_notes" in names
    assert (
        "Backup plan 2026"
        in next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")["content"]
    )
    assert row["score"] == 1.0


async def test_disk_usage_walks_the_tree_with_owners(trace_db: Database) -> None:
    sc = chat_scenario(
        "what takes the space on the big disk?",
        [{"type": "answer_contains_all", "values": ["movies"]}],
        world={
            "disk": {
                "roots": [
                    {
                        "path": "/mnt/disks/big",
                        "size_bytes": 10_000_000_000,
                        "children": [
                            {
                                "name": "movies",
                                "size_bytes": 7_000_000_000,
                                "owner": "radarr library",
                            },
                            {"name": "tv", "size_bytes": 2_000_000_000},
                        ],
                    }
                ],
                "files": [{"path": "/mnt/disks/big/movies/x.mkv", "size_bytes": 5_000_000_000}],
            }
        },
    )
    llm = FakeLLM(
        call("disk_usage", {"path": "/mnt/disks/big"}, "c1"),
        call("disk_usage", {"path": "/mnt/disks/big", "top_files": 3}, "c2"),
        call("disk_usage", {"path": "/nowhere"}, "c3"),
        say("movies takes 7 GB."),
    )
    row, _ = await attempt(trace_db, sc, llm)
    results = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert "/mnt/disks/big/movies" in results[0] and "radarr library" in results[0]
    assert "x.mkv" in results[1]
    assert "No such file" in results[2]
    assert row["score"] == 1.0


async def test_collector_slices_come_from_the_snapshot_with_a_patch(trace_db: Database) -> None:
    sc = chat_scenario(
        "how much memory is used?",
        [{"type": "answer_contains_all", "values": ["96"]}],
        patch={"collectors": {"system": {"data": {"memory": {"percent": 96.0}}}}},
    )
    llm = FakeLLM(
        call("get_collector_data", {"collector": "system", "section": "memory"}),
        say("Memory is 96% used."),
    )
    row, _ = await attempt(trace_db, sc, llm)
    result = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")["content"]
    assert ("96" in result and "8.0 GB" in result) or "96" in result
    assert row["score"] == 1.0


async def test_unconfigured_world_services_answer_with_the_default(trace_db: Database) -> None:
    sc = chat_scenario("anything", [{"type": "tool_called", "tool": "api_call"}])
    llm = FakeLLM(call("api_call", {"service": "radarr", "path": "/movie"}), say("ok"))
    await attempt(trace_db, sc, llm)
    result = next(m for m in llm.requests[1]["messages"] if m["role"] == "tool")["content"]
    assert "not available in this scenario" in result


async def test_failed_attempt_scores_zero_and_keeps_the_reason(trace_db: Database) -> None:
    sc = chat_scenario("hi", [{"type": "answer_contains_any", "values": ["x"]}])

    def boom(body: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("never reached")

    class Down(FakeLLM):
        def handler(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="overloaded")

    row, _ = await attempt(trace_db, sc, Down())
    assert row["status"] == "error" and row["score"] == 0
    assert "503" in row["error"]
    assert row["checks"][0]["reason"].startswith("attempt failed")


# --------------------------------------------------------------------------- history & time


async def test_snapshot_now_rebases_timestamps_to_the_present() -> None:
    snap = Snapshot.model_validate(
        {
            "now": 1_000_000.0,
            "collectors": {
                "systemd": {
                    "age_s": 10,
                    "data": {"failed": [{"name": "a.service", "since": 999_000.0}]},
                },
                "docker": {
                    "data": {
                        "containers": [
                            {"name": "x", "started_at": "1970-01-12T13:46:40.000000000Z"}
                        ]
                    }
                },
            },
        }
    )
    fake = FakeCollectors(snap, World(), now=5_000_000.0)
    since = fake.latest["systemd"].data["failed"][0]["since"]
    assert since == pytest.approx(5_000_000.0 - 1_000)
    assert fake.latest["systemd"].ts == pytest.approx(5_000_000.0 - 10)
    started = fake.latest["docker"].data["containers"][0]["started_at"]
    assert started.startswith("1970-02-") or started.startswith("1970-03-")
    assert rebase_times({"since": 1, "n": 2}, 10) == {"since": 11, "n": 2}
    status = fake.status()
    assert status["systemd"]["state"] == "active" and status["gpu"]["state"] == "disabled"


async def test_history_gives_the_digest_trends(trace_db: Database) -> None:
    from ai_server_monitor.bench.world import _load_history

    snap = Snapshot.model_validate(
        {
            "collectors": {"system": {"data": {}}},
            "history": {"system": [{"hours_ago": 48, "data": {"filesystems": []}}]},
        }
    )
    await _load_history(trace_db, snap, None)
    rows = await trace_db.fetchall("SELECT collector, ok FROM collector_samples")
    assert [(r["collector"], r["ok"]) for r in rows] == [("system", 1)]


# --------------------------------------------------------------------------- settings profile


def test_profile_configures_every_service_and_requires_approval() -> None:
    world = World.model_validate(
        {
            "api": [{"service": "radarr-4k", "path": "x"}, {"service": "movies", "path": "y"}],
            "mcp": [{"server": "notes", "tool": "t"}],
        }
    )
    s = bench_settings(LLMSettings(base_url="http://x/v1", model="m"), world)
    assert [i.name for i in s.arr.radarr] == ["radarr", "radarr-4k"]
    assert [i.name for i in s.arr.sonarr] == ["sonarr"]
    assert s.arr.prowlarr is not None and s.arr.qbittorrent is not None
    assert s.arr.dry_run is True and s.arr.actions.add_media.policy == "approve"
    assert s.chat.shell is True and s.mcp.dry_run is True
    assert s.mcp.servers[0].tool_filter == ["t"]
    assert str(s.collectors.prometheus.url).startswith("http://prometheus.bench")
    # the LLM keeps its secret; the scenario can patch the profile
    patched = bench_settings(
        LLMSettings(base_url="http://x/v1", model="m", api_key="sk-1"),
        World(),
        [{"chat": {"shell": False}}],
    )
    assert patched.llm.api_key.get_secret_value() == "sk-1" and patched.chat.shell is False


async def test_world_transport_routes_by_host_and_method() -> None:
    world = World.model_validate(
        {
            "api": [
                {"service": "radarr", "path": r"^/api/v3/movie$", "response": [1]},
                {"service": "all_arr", "method": "POST", "path": "command", "status": 201},
            ]
        }
    )
    async with httpx.AsyncClient(transport=world_transport(world)) as client:
        assert (await client.get("http://radarr.bench:7878/api/v3/movie")).json() == [1]
        assert (await client.post("http://sonarr.bench:7878/api/v3/command")).status_code == 201
        miss = await client.get("http://radarr.bench:7878/api/v3/other")
        assert miss.status_code == 404 and "not available" in miss.text


# --------------------------------------------------------------------------- Prometheus


PROM_INPUT = {
    "kind": "prom_plan",
    "catalog": {
        "jobs": [
            {
                "job": "node",
                "targets": [{"instance": "host:9100"}],
                "metrics": {
                    "node_netstat_Tcp_RetransSegs": {"type": "counter", "value": 120},
                    "node_load5": {"type": "gauge", "value": 0.4},
                    "node_disk_io_now": {"type": "gauge", "value": 1, "series": 3},
                },
            }
        ]
    },
}
PROM_CHECKS = [
    {"type": "prom_queries_valid", "min": 2},
    {"type": "prom_counters_rated"},
    {"type": "prom_job_scoped"},
]


async def test_prom_planner_runs_against_the_fake_prometheus(trace_db: Database) -> None:
    good = json.dumps(
        [
            {
                "name": "tcp_retransmits",
                "query": 'sum(rate(node_netstat_Tcp_RetransSegs{job="node"}[5m]))',
                "description": "Retransmitted segments per second",
                "unit": "per_s",
                "kind": "value",
            },
            {
                "name": "disk_queue",
                "query": 'max by (instance) (node_disk_io_now{job="node"})',
                "description": "Disk queue",
                "unit": "count",
                "kind": "value",
            },
        ]
    )
    llm = FakeLLM(say(good))
    row, _ = await attempt(trace_db, scenario("prom_plan", PROM_INPUT, PROM_CHECKS), llm)
    assert row["score"] == 1.0, row["checks"]
    prom = row["detail"]["prom"]
    assert len(prom["accepted"]) == 2 and len(prom["first_replies"]) == 2
    assert "tcp_retransmits" in row["answer"]


async def test_prom_first_reply_mistakes_are_not_hidden_by_the_repair_round(
    trace_db: Database,
) -> None:
    bad = json.dumps(
        [
            {
                "name": "retrans_total",
                "query": 'node_netstat_Tcp_RetransSegs{job="node"} > 100',
                "description": "bare counter",
                "kind": "alert",
            },
            {"name": "load", "query": "node_load5", "description": "no job selector"},
        ]
    )
    fixed = json.dumps(
        [
            {
                "name": "retrans_rate",
                "query": 'rate(node_netstat_Tcp_RetransSegs{job="node"}[5m])',
                "description": "fixed",
            },
            {
                "name": "load5",
                "query": 'node_load5{job="node"}',
                "description": "fixed",
            },
        ]
    )
    row, _ = await attempt(
        trace_db, scenario("prom_plan", PROM_INPUT, PROM_CHECKS), FakeLLM(say(bad), say(fixed))
    )
    by_type = {c["type"]: c for c in row["checks"]}
    assert by_type["prom_queries_valid"]["passed"]  # the repair round saved the final set
    assert not by_type["prom_counters_rated"]["passed"]  # ...but the first reply was wrong
    assert "node_netstat_Tcp_RetransSegs" in by_type["prom_counters_rated"]["reason"]
    assert not by_type["prom_job_scoped"]["passed"]


async def test_prom_catalog_overrides_and_parse_errors() -> None:
    from ai_server_monitor.bench.schema import PromCatalog

    catalog = PromCatalog.model_validate(
        {
            **PROM_INPUT["catalog"],
            "queries": [{"match": "boom", "error": "execution: many-to-many"}],
        }
    )
    async with httpx.AsyncClient(transport=prom_transport(catalog)) as client:
        base = "http://prometheus.bench:9090/api/v1"
        targets = (await client.get(f"{base}/targets", params={"state": "active"})).json()
        assert targets["data"]["activeTargets"][0]["labels"]["job"] == "node"
        count = await client.get(
            f"{base}/query", params={"query": 'count by (__name__) ({job="node"})'}
        )
        names = {r["metric"]["__name__"]: r["value"][1] for r in count.json()["data"]["result"]}
        assert names["node_disk_io_now"] == "3.0"
        assert (
            await client.get(f"{base}/query", params={"query": "boom(node_load5)"})
        ).status_code == 400
        assert (
            await client.get(f"{base}/query", params={"query": "sum(node_load5"})
        ).status_code == 400
        none = await client.get(f"{base}/query", params={"query": "unknown_metric"})
        assert none.json()["data"]["result"] == []
        rng = await client.get(f"{base}/query", params={"query": "node_load5[5m]"})
        assert rng.json()["data"]["resultType"] == "matrix"


_ = Path
