"""Prometheus auto-queries: catalog, planner (fake Prometheus + scripted LLM), store, collector."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ai_server_monitor.collectors.base import HostEnv
from ai_server_monitor.collectors.prometheus import PrometheusCollector
from ai_server_monitor.config import Settings
from ai_server_monitor.db import Database
from ai_server_monitor.llm import LLMClient
from ai_server_monitor.promauto import Planner, QueryStore, list_enabled_queries
from ai_server_monitor.promauto.catalog import build_catalog, keep_metric, mentions_known_metric
from ai_server_monitor.promauto.hosts import is_other_host
from ai_server_monitor.promauto.planner import extract_json_array, make_proposal
from ai_server_monitor.promauto.store import MAX_FAILS, Proposal
from ai_server_monitor.promql import PromQL
from ai_server_monitor.thresholds import breaches, normalize_threshold, parse_threshold

from .conftest import LLM_URL
from .promfakes import PROM, FakeLLM, FakeProm, job_of, plan, vector

ERRS = 'sum(rate(node_network_receive_errs_total{job="node"}[5m]))'
PRESSURE = 'rate(node_pressure_cpu_waiting_seconds_total{job="node"}[5m])'
GPU_T = 'max by (instance) (windows_gpu_temperature_celsius{job="windows-pc"})'
QUEUE = 'max by (instance) (windows_logical_disk_requests_queued{job="windows-pc"})'


def settings(*, llm: bool = True, **prom: Any) -> Settings:
    prom = {"url": PROM, "queries": [], **prom}
    return Settings(
        llm={"base_url": LLM_URL if llm else "", "model": "m" if llm else "", "max_retries": 0},
        collectors={"prometheus": prom},
    )


def make_planner(
    db: Database, prom: FakeProm, llm: FakeLLM, cfg: Settings | None = None
) -> Planner:
    cfg = cfg or settings()
    client = LLMClient(cfg.llm, transport=llm.transport())
    return Planner(cfg, db, client, transport=prom.transport())


def good_reply(messages: list[dict[str, Any]], n: int) -> str:
    if job_of(messages) == "node":
        return plan(
            {
                "name": "net_errors",
                "query": ERRS,
                "description": "Receive errors per second",
                "unit": "per_s",
                "warn_if": "> 1",
            },
            {"name": "cpu_pressure", "query": PRESSURE, "description": "CPU stall"},
        )
    return plan(
        {"name": "gpu_temp", "query": GPU_T, "unit": "celsius", "crit_if": ">= 90"},
        {"name": "disk_queue", "query": QUEUE, "description": "Disk queue", "warn_if": "> 4"},
    )


def prom_with_answers() -> FakeProm:
    prom = FakeProm()
    prom.answers = {
        ERRS: vector(({}, 0)),
        PRESSURE: vector(({"instance": "node-exporter:9100"}, 0.01)),
        GPU_T: vector(({"instance": "192.168.88.30:9835"}, 61)),
        QUEUE: vector(({"instance": "192.168.88.30:9182"}, 0.2)),
    }
    return prom


# --------------------------------------------------------------------------- thresholds


def test_threshold_grammar_is_strict() -> None:
    assert parse_threshold(">= 0.5") == (">=", 0.5)
    assert parse_threshold("<=-3") == ("<=", -3.0)
    assert normalize_threshold(" >  80 ") == "> 80"
    assert normalize_threshold("") is None and normalize_threshold(None) is None
    for bad in ("> 80%", "80", "> x", "> 1 and", "=> 1", "> 1; drop", "> inf", ">", "> 1e999"):
        with pytest.raises(ValueError):
            parse_threshold(bad)
    assert breaches("> 80", 81) and not breaches("> 80", 80)
    assert breaches("== 0", 0) and breaches("!= 1", 0.0)
    assert not breaches("> 80", "NaN") and not breaches("> 80", float("nan"))
    assert not breaches("garbage", 5) and not breaches(None, 5)


def test_hosts_heuristic() -> None:
    assert is_other_host("192.168.88.30:9182", "windows-pc")
    assert is_other_host("10.0.0.5:9100", "node")
    assert is_other_host("anything", "windows-gpu")
    assert not is_other_host("node-exporter:9100", "node")
    assert not is_other_host("172.18.0.4:8080", "cadvisor")
    assert not is_other_host("127.0.0.1:9090", "prometheus")


# --------------------------------------------------------------------------- parsing


def test_extract_json_array_forms() -> None:
    arr = '[{"name": "a", "query": "up"}]'
    assert extract_json_array(arr) == [{"name": "a", "query": "up"}]
    assert extract_json_array(f"Sure!\n```json\n{arr}\n```\nHope it helps") is not None
    assert extract_json_array(f"<think>hmm [1]</think>{arr}") == [{"name": "a", "query": "up"}]
    assert extract_json_array(f"Here you go: {arr} bye") is not None
    assert extract_json_array('{"queries": [{"name": "a", "query": "up"}]}') is not None
    assert extract_json_array('{"name": "a", "query": "up"}') == [{"name": "a", "query": "up"}]
    assert extract_json_array("no json here") is None
    assert extract_json_array("[1, 2, 3]") is None
    assert extract_json_array('[{"name": "a", "query": "up"') is None


def test_make_proposal_normalizes_and_rejects() -> None:
    p = make_proposal(
        {"name": "Net Errors!", "query": "sum(\n  up) > 0", "warn_if": ">  5", "kind": "ALERT"},
        "node",
    )
    assert (p.name, p.query, p.warn_if, p.kind, p.job) == (
        "net_errors",
        "sum( up) > 0",
        "> 5",
        "alert",
        "node",
    )
    for bad in (
        {"query": "up"},
        {"name": "a"},
        {"name": "a", "query": "up", "warn_if": "high"},
        {"name": "a", "query": "up", "crit_if": 5},
        {"name": "a", "query": "x" * 700},
    ):
        with pytest.raises(ValueError):
            make_proposal(bad, "node")


# --------------------------------------------------------------------------- catalog


async def test_catalog_filters_ranks_and_hashes() -> None:
    prom = FakeProm()
    big = PromQL(PROM, max_series=5000, transport=prom.transport())
    client = PromQL(PROM, transport=prom.transport())
    catalog = await build_catalog(client, big)
    assert [j.job for j in catalog.jobs] == ["node", "windows-pc"]
    node = catalog.job("node")
    assert node is not None
    assert "go_gc_duration_seconds" not in node.metrics  # exporter internals
    assert "node_disk_io_time_seconds_bucket" not in node.metrics  # histogram part
    assert "node_scrape_collector_duration_seconds" not in node.metrics
    assert node.metrics["node_cpu_seconds_total"] == 96
    lines = node.text.splitlines()
    assert lines[0].startswith("JOB node: 1 target(s) [node-exporter:9100 up]")
    names = [ln.split()[0] for ln in lines[3:]]
    # an error counter outranks what the monitor already collects itself
    assert names.index("node_network_receive_errs_total") < names.index("node_cpu_seconds_total")
    assert "Network device statistic receive_errs." in node.text
    assert node.other_host == ""
    win = catalog.job("windows-pc")
    assert win is not None and win.other_host == "192.168.88.30"
    assert "aggregate!" in win.text  # 2268 series
    first = catalog.hash

    prom.metrics["node"]["node_new_errors_total"] = 1
    again = await build_catalog(client, big)
    assert again.hash != first
    prom.metrics["node"].pop("node_new_errors_total")
    assert (await build_catalog(client, big)).hash == first
    await client.close()
    await big.close()


def test_metric_helpers() -> None:
    assert keep_metric("node_cpu_seconds_total") and not keep_metric("go_threads")
    assert not keep_metric("x_bucket") and not keep_metric("process_cpu_seconds_total")
    assert mentions_known_metric("rate(a_b[5m]) > 0", {"a_b": 1})
    assert not mentions_known_metric("rate(invented[5m])", {"a_b": 1})


async def test_catalog_slice_stays_small_for_a_huge_job() -> None:
    prom = FakeProm()
    prom.metrics["node"] = {f"node_thing{i}_errors_total": 1 for i in range(400)}
    big = PromQL(PROM, max_series=5000, transport=prom.transport())
    catalog = await build_catalog(PromQL(PROM, transport=prom.transport()), big)
    node = catalog.job("node")
    assert node is not None and len(node.text) <= 3000
    assert len(node.text.splitlines()) <= 3 + 28
    await big.close()


# --------------------------------------------------------------------------- planner


async def test_good_plan_is_validated_and_stored(db: Database) -> None:
    prom, llm = prom_with_answers(), FakeLLM(good_reply)
    outcome = await make_planner(db, prom, llm).run()
    assert outcome.status == "ok" and outcome.counts["added"] == 4
    rows = {q["name"]: q for q in await QueryStore(db).fetch()}
    assert set(rows) == {"net_errors", "cpu_pressure", "gpu_temp", "disk_queue"}
    r = rows["net_errors"]
    assert (r["source"], r["job"], r["enabled"], r["warn_if"], r["unit"]) == (
        "auto",
        "node",
        True,
        "> 1",
        "per_s",
    )
    assert r["last_value"]["series"][0]["value"] == 0.0 and r["last_ok_ts"]
    assert rows["gpu_temp"]["crit_if"] == ">= 90"
    assert len(llm.calls) == 2  # one small call per job
    # a thinking model must not spend the plan on reasoning
    assert all(b["reasoning_effort"] == "none" for b in llm.bodies)
    system = llm.calls[0][0]["content"]
    assert "ALREADY collects" in system and "Docker containers" in system
    # the windows job is announced as another machine
    windows_call = next(c for c in llm.calls if "JOB windows-pc" in c[1]["content"])
    assert "ANOTHER machine (192.168.88.30)" in windows_call[1]["content"]
    plan_info = await db.kv_get("prom_plan")
    assert plan_info["status"] == "ok" and plan_info["source"] == "llm" and plan_info["hash"]
    enabled = await list_enabled_queries(db)
    assert {q["name"] for q in enabled} == set(rows) and "query" in enabled[0]


async def test_broken_json_is_repaired_with_one_retry(db: Database) -> None:
    prom = prom_with_answers()

    def reply(messages: list[dict[str, Any]], n: int) -> str:
        if n == 1:
            return "I would suggest looking at errors. Here: [{name: net_errors, query: broken"
        return good_reply(messages, n)

    llm = FakeLLM(reply)
    outcome = await make_planner(db, prom, llm).run()
    assert outcome.status == "ok"
    assert "not a valid JSON array" in llm.calls[1][-1]["content"]
    assert len(await QueryStore(db).fetch()) == 4


async def test_invalid_queries_get_the_prometheus_error_once_then_are_dropped(
    db: Database,
) -> None:
    prom = prom_with_answers()
    prom.answers['rate(node_network_receive_errs_total{job="node"}[5m]'] = (
        "error",
        "unclosed left bracket",
    )
    prom.answers[GPU_T] = vector(*[({"instance": f"h{i}"}, 1) for i in range(40)])  # too many

    def reply(messages: list[dict[str, Any]], n: int) -> str:
        job = job_of(messages)
        repaired = len(messages) > 2
        if job == "node":
            if not repaired:
                return plan(
                    {
                        "name": "bad_syntax",
                        "query": 'rate(node_network_receive_errs_total{job="node"}[5m]',
                    },
                    {"name": "invented", "query": "rate(made_up_metric_total[5m])"},
                    {"name": "bad_limit", "query": ERRS, "warn_if": "very high"},
                    {"name": "cpu_pressure", "query": PRESSURE},
                )
            return plan({"name": "bad_syntax", "query": ERRS, "description": "fixed"})
        return plan({"name": "gpu_temp", "query": GPU_T})

    llm = FakeLLM(reply)
    outcome = await make_planner(db, prom, llm).run()
    assert outcome.status == "ok"
    rows = {q["name"] for q in await QueryStore(db).fetch()}
    # fixed after the error came back; the good one kept; the unfixable and invented ones gone
    assert rows == {"bad_syntax", "cpu_pressure"}
    node_repair = next(c for c in llm.calls if job_of(c) == "node" and len(c) > 2)
    feedback = node_repair[-1]["content"]
    assert "unclosed left bracket" in feedback  # Prometheus's own message
    assert "invented metric names" in feedback
    assert "warn_if" in feedback and "operator and a number" in feedback
    gpu_repair = next(c for c in llm.calls if job_of(c) == "windows-pc" and len(c) > 2)
    assert "returns 40 series" in gpu_repair[-1]["content"]
    info = await db.kv_get("prom_plan")
    assert any(r["name"] == "gpu_temp" and "40 series" in r["reason"] for r in info["rejected"])


async def test_alert_queries_may_be_empty_value_queries_may_not(db: Database) -> None:
    prom = FakeProm()
    prom.answers = {ERRS: vector(), PRESSURE: vector()}

    def reply(messages: list[dict[str, Any]], n: int) -> str:
        if job_of(messages) != "node":
            return "[]"
        return plan(
            {"name": "net_errors_now", "query": f"{ERRS} > 0", "kind": "alert"},
            {"name": "pressure", "query": PRESSURE, "kind": "value"},
        )

    llm = FakeLLM(reply)
    await make_planner(db, prom, llm).run()
    assert {q["name"] for q in await QueryStore(db).fetch()} == {"net_errors_now"}


async def test_total_llm_failure_uses_the_fallback_set(db: Database) -> None:
    prom = FakeProm()
    prom.answers = {
        "max(scrape_duration_seconds)": vector(({}, 0.4)),
        "sum(increase(prometheus_rule_evaluation_failures_total[1h]))": vector(),
        "sum(scrape_samples_scraped)": vector(({}, 5000)),
    }
    llm = FakeLLM(lambda m, n: httpx.Response(500, text="model crashed"))
    outcome = await make_planner(db, prom, llm).run()
    assert outcome.status == "fallback"
    rows = {q["name"]: q for q in await QueryStore(db).fetch()}
    # Prometheus does not answer the self-health candidate here → dropped by validation
    assert set(rows) == {"scrape_slowest", "samples_scraped"}
    assert all(q["source"] == "fallback" for q in rows.values())
    assert rows["scrape_slowest"]["warn_if"] == "> 10"
    assert (await db.kv_get("prom_plan"))["source"] == "fallback"


async def test_garbage_replies_also_fall_back(db: Database) -> None:
    prom = FakeProm()
    prom.answers = {"max(scrape_duration_seconds)": vector(({}, 1))}
    llm = FakeLLM(lambda m, n: "I cannot help with that.")
    outcome = await make_planner(db, prom, llm).run()
    assert outcome.status == "fallback"
    assert {q["name"] for q in await QueryStore(db).fetch()} == {"scrape_slowest"}


async def test_no_llm_configured_uses_fallback_and_later_upgrades(db: Database) -> None:
    prom = prom_with_answers()
    prom.answers["max(scrape_duration_seconds)"] = vector(({}, 1))
    no_llm = make_planner(db, prom, FakeLLM(lambda m, n: ""), settings(llm=False))
    assert (await no_llm.run()).status == "fallback"
    assert {q["name"] for q in await QueryStore(db).fetch()} == {"scrape_slowest"}
    # the model gets configured: same catalog, but the plan is redone with it
    llm = FakeLLM(good_reply)
    assert (await make_planner(db, prom, llm).run()).status == "ok"
    names = {q["name"] for q in await QueryStore(db).fetch()}
    assert "net_errors" in names and "scrape_slowest" not in names  # fallback replaced


async def test_planning_that_fails_keeps_the_existing_queries(db: Database) -> None:
    prom = prom_with_answers()
    assert (await make_planner(db, prom, FakeLLM(good_reply)).run()).status == "ok"
    dead = FakeLLM(lambda m, n: httpx.Response(500))
    outcome = await make_planner(db, prom, dead).run(force=True)
    assert outcome.status == "failed" and "kept the current queries" in outcome.message
    assert len(await QueryStore(db).fetch()) == 4
    unconfigured = make_planner(db, prom, FakeLLM(lambda m, n: ""), settings(llm=False))
    assert (await unconfigured.run(force=True)).status == "skipped"
    assert len(await QueryStore(db).fetch()) == 4
    info = await db.kv_get("prom_plan")
    assert info["source"] == "llm" and info["hash"]  # a failed attempt hides nothing


async def test_prometheus_unreachable_changes_nothing(db: Database) -> None:
    prom = FakeProm()
    prom.unreachable = True
    llm = FakeLLM(good_reply)
    outcome = await make_planner(db, prom, llm).run()
    assert outcome.status == "failed" and "cannot read Prometheus" in outcome.message
    assert not llm.calls and await QueryStore(db).fetch() == []


async def test_replan_only_when_the_catalog_changed_or_forced(db: Database) -> None:
    prom, llm = prom_with_answers(), FakeLLM(good_reply)
    planner = make_planner(db, prom, llm)
    assert (await planner.run()).status == "ok"
    calls = len(llm.calls)
    again = await planner.run()
    assert again.status == "skipped" and len(llm.calls) == calls  # same catalog, not due
    prom.metrics["node"]["node_new_errors_total"] = 1
    assert (await planner.run()).status == "ok" and len(llm.calls) > calls
    calls = len(llm.calls)
    assert (await planner.run()).status == "skipped"
    assert (await planner.run(force=True)).status == "ok" and len(llm.calls) > calls
    # an expired interval also re-plans
    info = await db.kv_get("prom_plan")
    await db.kv_set("prom_plan", {**info, "ts": info["ts"] - 8 * 86400})
    calls = len(llm.calls)
    assert (await planner.run()).status == "ok" and len(llm.calls) > calls


async def test_replan_keeps_pinned_and_edited_and_respects_deletes(db: Database) -> None:
    prom, llm = prom_with_answers(), FakeLLM(good_reply)
    planner = make_planner(db, prom, llm)
    await planner.run()
    store = QueryStore(db)
    by_name = {q["name"]: q for q in await store.fetch()}
    await store.update(by_name["net_errors"]["id"], {"pinned": True})
    edited = await store.update(
        by_name["gpu_temp"]["id"], {"query": GPU_T + " + 0", "warn_if": "> 80"}
    )
    assert edited is not None and edited["edited"] and edited["warn_if"] == "> 80"
    await store.update(by_name["cpu_pressure"]["id"], {"enabled": False})
    await store.delete(by_name["disk_queue"]["id"])  # the user does not want it

    def different(messages: list[dict[str, Any]], n: int) -> str:
        if job_of(messages) == "node":
            return plan(
                {"name": "net_errors", "query": ERRS, "description": "NEW TEXT"},  # pinned
                {"name": "cpu_pressure", "query": PRESSURE, "description": "new"},
            )
        return plan(
            {"name": "gpu_temp", "query": GPU_T, "description": "model's version"},  # edited
            {"name": "disk_queue", "query": QUEUE},  # was deleted
            {"name": "gpu_fan", "query": GPU_T, "description": "x"},  # same query as gpu_temp
        )

    outcome = await make_planner(db, prom, FakeLLM(different)).run(force=True)
    assert outcome.status == "ok"
    now = {q["name"]: q for q in await store.fetch()}
    assert now["net_errors"]["description"] == "Receive errors per second"  # pinned: untouched
    assert now["net_errors"]["pinned"]
    assert now["gpu_temp"]["query"].endswith("+ 0") and now["gpu_temp"]["warn_if"] == "> 80"
    assert "disk_queue" not in now  # deleted stays deleted
    assert now["cpu_pressure"]["description"] == "new"  # refreshed
    assert not now["cpu_pressure"]["enabled"]  # but still switched off by the user


async def test_user_queries_and_cap_are_respected(db: Database) -> None:
    prom = prom_with_answers()
    cfg = settings(max_auto_queries=2, queries=[{"name": "net_errors", "query": "up"}])
    llm = FakeLLM(good_reply)
    await make_planner(db, prom, llm, cfg).run()
    names = [q["name"] for q in await QueryStore(db).fetch()]
    assert "net_errors" not in names  # the user's own query of that name wins
    assert len(names) == 2
    assert {q["job"] for q in await QueryStore(db).fetch()} == {"node", "windows-pc"}  # round-robin


async def test_disabled_automatic_queries_do_nothing(db: Database) -> None:
    prom, llm = prom_with_answers(), FakeLLM(good_reply)
    planner = make_planner(db, prom, llm, settings(auto_queries=False))
    assert not planner.enabled()
    assert (await planner.run()).status == "skipped" and not prom.calls


# --------------------------------------------------------------------------- store


async def test_store_disables_chronic_failures_and_recovers(db: Database) -> None:
    store = QueryStore(db)
    await store.apply_plan([Proposal("q", "up", job="node")], source="auto")
    (q,) = await store.fetch()
    for i in range(MAX_FAILS - 1):
        await store.record_results([(q["id"], False, None, f"boom {i}")], 100 + i)
    assert (await store.get(q["id"]))["enabled"]  # type: ignore[index]
    await store.record_results([(q["id"], False, None, "boom last")], 200)
    got = await store.get(q["id"])
    assert got is not None and not got["enabled"]
    assert got["disabled_reason"] == "failed 5 runs in a row: boom last"
    assert got["last_error"] == "boom last"
    assert await store.fetch(enabled_only=True) == []
    again = await store.update(q["id"], {"enabled": True})
    assert again is not None and again["enabled"] and again["fail_count"] == 0
    assert again["disabled_reason"] is None
    await store.record_results([(q["id"], True, {"series": [], "total": 0}, None)], 300)
    assert (await store.get(q["id"]))["last_ok_ts"] == 300  # type: ignore[index]


async def test_store_update_validates_thresholds_and_flags_edits(db: Database) -> None:
    store = QueryStore(db)
    await store.apply_plan([Proposal("q", "up", job="j")], source="auto")
    (q,) = await store.fetch()
    assert not q["edited"]
    pinned = await store.update(q["id"], {"pinned": True})
    assert pinned is not None and pinned["pinned"] and not pinned["edited"]
    with pytest.raises(ValueError):
        await store.update(q["id"], {"warn_if": "lots"})
    changed = await store.update(q["id"], {"warn_if": "> 3"})
    assert changed is not None and changed["edited"]
    assert await store.update(999, {"pinned": True}) is None


# --------------------------------------------------------------------------- collector


def collector(db: Database, prom: FakeProm, **cfg: Any) -> PrometheusCollector:
    s = settings(**cfg).collectors.prometheus
    return PrometheusCollector(s, HostEnv(), transport=prom.transport(), store=QueryStore(db))


async def test_collector_runs_user_and_planned_queries_and_reports_back(db: Database) -> None:
    prom = prom_with_answers()
    await make_planner(db, prom, FakeLLM(good_reply)).run()
    prom.answers["up == 0"] = vector(({"job": "node"}, 0))
    prom.targets[1]["health"] = "down"
    prom.targets[1]["lastError"] = "connection refused"
    prom.alerts = [
        {
            "state": "firing",
            "labels": {"alertname": "DiskFull", "severity": "critical", "instance": "node:9100"},
            "annotations": {"summary": "Disk is full"},
            "activeAt": "2026-11-15T10:00:00Z",
        },
        {"state": "pending", "labels": {"alertname": "Soon"}},
    ]
    c = collector(
        db,
        prom,
        queries=[
            {"name": "mine", "query": "up == 0", "warn_if": "> 0", "unit": "count"},
            {"name": "net_errors", "query": "different"},  # same name as a planned one
        ],
    )
    data = await c.collect()
    await c.close()
    by_name = {q["name"]: q for q in data["queries"]}
    assert set(by_name) == {"mine", "net_errors", "cpu_pressure", "gpu_temp", "disk_queue"}
    assert by_name["net_errors"]["source"] == "user"  # the user's wins, planned one skipped
    assert by_name["gpu_temp"]["source"] == "auto" and by_name["gpu_temp"]["crit_if"] == ">= 90"
    assert data["targets"]["total"] == 2 and data["targets"]["up"] == 1
    assert data["targets"]["down"][0]["error"] == "connection refused"
    assert data["targets"]["other_hosts"] == {"windows-pc": "192.168.88.30"}
    assert data["alerts"]["firing_count"] == 1 and data["alerts"]["pending_count"] == 1
    assert data["alerts"]["firing"][0]["name"] == "DiskFull"
    row = {q["name"]: q for q in await QueryStore(db).fetch()}["gpu_temp"]
    assert row["last_value"]["series"][0]["value"] == 61.0  # written back for the UI


async def test_collector_disables_a_query_that_keeps_failing(db: Database) -> None:
    prom = prom_with_answers()
    await make_planner(db, prom, FakeLLM(good_reply)).run()
    prom.answers[ERRS] = ("error", "many-to-many matching not allowed")
    c = collector(db, prom)
    for _ in range(MAX_FAILS):
        data = await c.collect()
    await c.close()
    assert "net_errors" in data["summary"]["failed"]
    row = {q["name"]: q for q in await QueryStore(db).fetch()}["net_errors"]
    assert not row["enabled"] and "many-to-many" in row["disabled_reason"]
    c2 = collector(db, prom)
    data = await c2.collect()
    await c2.close()
    assert "net_errors" not in {q["name"] for q in data["queries"]}  # no longer run


async def test_collector_without_auto_queries_is_the_old_collector(db: Database) -> None:
    prom = prom_with_answers()
    await make_planner(db, prom, FakeLLM(good_reply)).run()
    prom.calls.clear()
    c = collector(db, prom, auto_queries=False, queries=[{"name": "x", "query": "up == 0"}])
    data = await c.collect()
    await c.close()
    assert [q["name"] for q in data["queries"]] == ["x"] and "targets" not in data
    assert "/api/v1/targets" not in prom.calls and "/api/v1/alerts" not in prom.calls


async def test_collector_unreachable_is_unavailable(db: Database) -> None:
    from ai_server_monitor.collectors.base import CollectorUnavailable

    prom = FakeProm()
    prom.unreachable = True
    c = collector(db, prom)  # no queries at all, only targets/alerts
    with pytest.raises(CollectorUnavailable, match="cannot reach Prometheus"):
        await c.collect()
    await c.close()


async def test_query_without_the_job_selector_is_sent_back(db: Database) -> None:
    prom = prom_with_answers()
    unscoped = "sum(rate(node_network_receive_errs_total[5m]))"
    prom.answers[unscoped] = vector(({}, 0))

    def reply(messages: list[dict[str, Any]], n: int) -> str:
        if job_of(messages) != "node":
            return "[]"
        if len(messages) == 2:
            return plan({"name": "net_errors", "query": unscoped})
        return plan({"name": "net_errors", "query": ERRS})

    llm = FakeLLM(reply)
    await make_planner(db, prom, llm).run()
    repair = next(c for c in llm.calls if job_of(c) == "node" and len(c) > 2)
    assert 'no {job="node"} selector' in repair[-1]["content"]
    (row,) = await QueryStore(db).fetch()
    assert row["query"] == ERRS


async def test_thresholds_the_current_value_already_crosses_are_dropped(db: Database) -> None:
    prom = prom_with_answers()  # net errors 0, cpu pressure 0.01

    def reply(messages: list[dict[str, Any]], n: int) -> str:
        if job_of(messages) != "node":
            return "[]"
        return plan(
            {"name": "net_errors", "query": ERRS, "warn_if": "> 1", "crit_if": "== 0"},
            {"name": "cpu_pressure", "query": PRESSURE, "warn_if": "> 0.005", "crit_if": "> 0.5"},
        )

    await make_planner(db, prom, FakeLLM(reply)).run()
    rows = {q["name"]: q for q in await QueryStore(db).fetch()}
    assert (rows["net_errors"]["warn_if"], rows["net_errors"]["crit_if"]) == ("> 1", None)
    assert (rows["cpu_pressure"]["warn_if"], rows["cpu_pressure"]["crit_if"]) == (None, "> 0.5")


def test_alert_kind_needs_a_filter_in_the_query() -> None:
    plain = make_proposal({"name": "a", "query": "rate(x[5m])", "kind": "alert"}, "j")
    assert plain.kind == "value"
    filtered = make_proposal({"name": "a", "query": 'up{job=~"a"} == 0', "kind": "alert"}, "j")
    assert filtered.kind == "alert"
    ranged = make_proposal({"name": "a", "query": "rate(x[5m]) > 0.1", "kind": "alert"}, "j")
    assert ranged.kind == "alert"


# --------------------------------------------------------------------------- counters

THERMAL = "nvidia_smi_clocks_event_reasons_counters_hw_thermal_slowdown_seconds"


def prom_with_cumulative_gauge() -> FakeProm:
    """The shape that went wrong live: a cumulative total that the exporter declares a gauge."""
    prom = prom_with_answers()
    prom.metrics["node"][THERMAL] = 1
    prom.meta[THERMAL] = [{"type": "gauge", "help": "clocks_event_reasons_counters [us]"}]
    prom.answers[f'max by (instance) ({THERMAL}{{job="node"}})'] = vector(({}, 7))
    prom.answers[f'max by (instance) (rate({THERMAL}{{job="node"}}[5m]))'] = vector(({}, 0))
    return prom


def test_counter_detection_uses_type_name_and_help() -> None:
    from ai_server_monitor.promauto.catalog import drop_unusable, is_counter

    assert is_counter("x_total", {"type": "counter"})
    assert is_counter("node_netstat_Tcp_RetransSegs", {"type": "unknown"})  # untyped netstat
    assert not is_counter("node_netstat_Tcp_CurrEstab", {"type": "unknown"})
    assert is_counter(THERMAL, {"type": "gauge", "help": "x"})  # declared gauge, cumulative name
    assert is_counter("nvidia_smi_ecc_errors_uncorrected_aggregate_sram_l2", {"type": "gauge"})
    assert is_counter("some_metric", {"type": "gauge", "help": "Cumulative bytes sent"})
    assert is_counter("windows_net_errors_total", None)  # no metadata at all
    assert not is_counter("container_fs_inodes_total", {"type": "gauge"})  # a real gauge
    assert not is_counter("node_load5", {"type": "gauge"})
    # ECC error counters are left out unless the GPU reports an ECC mode
    ecc = {"a_ecc_errors_uncorrected": 1, "b_temperature": 1}
    assert drop_unusable(ecc) == {"b_temperature": 1}
    assert drop_unusable({**ecc, "a_ecc_mode_current": 1}) == {**ecc, "a_ecc_mode_current": 1}


def test_a_bare_counter_is_found_and_wrapped_ones_pass() -> None:
    from ai_server_monitor.promauto.planner import unwrapped_counter

    c = {"a_total", "b_seconds"}
    assert unwrapped_counter('max by (instance) (b_seconds{job="x"})', c) == "b_seconds"
    assert unwrapped_counter("a_total > 5", c) == "a_total"
    assert unwrapped_counter('sum(rate(a_total{job="x"}[5m]))', c) is None
    assert unwrapped_counter("increase(a_total[1h]) + rate(b_seconds[5m])", c) is None
    assert unwrapped_counter("max_over_time(a_total[1h])", c) == "a_total"  # not a rate
    assert unwrapped_counter("rate(a_total[5m]) / a_total", c) == "a_total"  # one bare use
    assert unwrapped_counter('up{job="a_total"}', c) is None  # inside a label value


async def test_the_catalog_marks_cumulative_gauges_as_counters() -> None:
    prom = prom_with_cumulative_gauge()
    catalog = await build_catalog(
        PromQL(PROM, transport=prom.transport()),
        PromQL(PROM, max_series=5000, transport=prom.transport()),
    )
    node = catalog.job("node")
    assert node is not None and THERMAL in node.counters
    line = next(ln for ln in node.text.splitlines() if ln.startswith(THERMAL))
    assert f"{THERMAL} [c]" in line
    assert "COUNTER" in node.text and "node_cpu_seconds_total" in node.counters


async def test_a_bare_counter_with_a_limit_is_repaired_with_rate(db: Database) -> None:
    prom = prom_with_cumulative_gauge()
    bare = f'max by (instance) ({THERMAL}{{job="node"}})'
    wrapped = f'max by (instance) (rate({THERMAL}{{job="node"}}[5m]))'

    def reply(messages: list[dict[str, Any]], n: int) -> str:
        if job_of(messages) != "node":
            return "[]"
        if len(messages) == 2:
            return plan(
                {"name": "thermal_throttling", "query": bare, "warn_if": "> 0.1", "crit_if": "> 1"}
            )
        return plan({"name": "thermal_throttling", "query": wrapped, "warn_if": "> 0.1"})

    llm = FakeLLM(reply)
    await make_planner(db, prom, llm).run()
    system = llm.calls[0][0]["content"]
    assert "COUNTERS" in system and "rate(x[5m])" in system
    repair = next(c for c in llm.calls if job_of(c) == "node" and len(c) > 2)
    assert f"{THERMAL} is a counter" in repair[-1]["content"]
    assert f"rate({THERMAL}[5m])" in repair[-1]["content"]
    (row,) = await QueryStore(db).fetch()
    assert row["query"] == wrapped and row["warn_if"] == "> 0.1"  # a rate: 0 now, limit sane


async def test_a_counter_that_stays_bare_is_dropped(db: Database) -> None:
    prom = prom_with_cumulative_gauge()
    bare = f'max by (instance) ({THERMAL}{{job="node"}})'
    llm = FakeLLM(
        lambda m, n: (
            plan({"name": "thermal_throttling", "query": bare, "crit_if": "> 1"})
            if job_of(m) == "node"
            else "[]"
        )
    )
    outcome = await make_planner(db, prom, llm).run()
    assert outcome.status == "fallback"  # nothing else usable
    assert "thermal_throttling" not in {q["name"] for q in await QueryStore(db).fetch()}
    info = await db.kv_get("prom_plan")
    assert any("is a counter" in r["reason"] for r in info["rejected"])
