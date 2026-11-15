"""Prometheus results in the digest (lines, flags, other-host labels) and the HTTP API."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap, ReportSettings, Settings
from ai_server_monitor.db import Database
from ai_server_monitor.digest import build_digest
from ai_server_monitor.digest.prom import flags as prom_flags
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .promfakes import PROM, FakeLLM
from .test_digest import NOW, entry, snapshot
from .test_prom_auto import good_reply, prom_with_answers


def q(name: str, series: list[tuple[dict[str, str], float]], **over: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": over.pop("description", ""),
        "query": "x",
        "ok": True,
        "series": [{"labels": lab, "value": v} for lab, v in series],
        "total": len(series),
        "truncated": False,
        **over,
    }


def prom_data(queries: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "url": PROM,
        "summary": {"queries": len(queries), "failed": []},
        "queries": queries,
        **extra,
    }


async def digest_with(db: Database, data: dict[str, Any]):  # type: ignore[no-untyped-def]
    snap = snapshot(prometheus=entry(data))
    return await build_digest(snap, db, ReportSettings(), now=NOW)


def section(digest, key: str = "prometheus") -> str:  # type: ignore[no-untyped-def]
    text = digest.text
    start = text.index("## Prometheus")
    return text[start:].split("\n\n## ")[0]


async def test_lines_label_the_other_host_and_show_units(db: Database) -> None:
    data = prom_data(
        [
            q(
                "gpu_temp",
                [({"instance": "192.168.88.30:9835", "job": "windows-gpu"}, 61.0)],
                unit="celsius",
                description="GPU temperature of the PC",
                job="windows-gpu",
            ),
            q(
                "net_errors",
                [({"instance": "node-exporter:9100"}, 0.4)],
                unit="per_s",
                description="NIC receive errors",
                job="node",
            ),
            q("mem_pressure", [({}, 12.345)], unit="%"),
            q("quiet_check", [], kind="alert"),
            q("gone", [], kind="value"),
        ],
        targets={"total": 6, "up": 6, "down": [], "other_hosts": {"windows-pc": "192.168.88.30"}},
        alerts={"firing": [], "firing_count": 0, "pending_count": 0},
    )
    d = await digest_with(db, data)
    text = section(d)
    assert "Scrape targets: 6 of 6 up; other hosts scraped: windows-pc (192.168.88.30)" in text
    assert "Metric gpu_temp: other host 192.168.88.30 61°C — GPU temperature of the PC" in text
    assert "Metric net_errors: node-exporter 0.4 per_s — NIC receive errors" in text
    assert "Metric mem_pressure: 12% " in text or "Metric mem_pressure: 12%" in text
    assert "Checks with nothing to report: quiet_check" in text
    assert "Metric gone: no data" in text
    assert d.flags == []  # nothing crossed a limit; healthy stays healthy


async def test_thresholds_alerts_and_down_targets_become_flags(db: Database) -> None:
    data = prom_data(
        [
            q(
                "gpu_temp",
                [({"instance": "192.168.88.30:9835"}, 93.0), ({"instance": "x:1"}, 50.0)],
                unit="celsius",
                description="GPU temperature",
                warn_if="> 80",
                crit_if=">= 90",
                job="windows-gpu",
            ),
            q("disk_queue", [({}, 9.0)], warn_if="> 4", description="Disk queue", unit="count"),
            q("fine", [({}, 1.0)], warn_if="> 4"),
            q("down_now", [({"job": "x"}, 0.0)], kind="alert", description="Services down"),
            q("user_q", [({}, 99.0)]),  # no thresholds: never a flag
        ],
        targets={
            "total": 3,
            "up": 2,
            "down": [{"job": "windows-pc", "instance": "192.168.88.30:9182", "error": "refused"}],
            "down_count": 1,
            "other_hosts": {},
        },
        alerts={
            "firing": [
                {
                    "name": "DiskFull",
                    "severity": "critical",
                    "instance": "node:9100",
                    "summary": "Full",
                },
                {"name": "Slow", "severity": "warning", "instance": "", "summary": ""},
                {"name": "FYI", "severity": "info", "instance": "", "summary": ""},
            ],
            "firing_count": 3,
            "pending_count": 0,
        },
    )
    d = await digest_with(db, data)
    flags = {f.title: (f.severity, f.detail) for f in prom_flags(data)}
    assert len(d.flags) == 6  # the rule folds a long list: 5 kept + "+N more"
    sev, detail = flags["GPU temperature"]
    assert sev == "crit" and "gpu_temp = 93°C" in detail
    assert "192.168.88.30" in detail and "(limit >= 90)" in detail
    assert flags["Disk queue"][0] == "warn"
    assert flags["Services down"][0] == "warn"
    assert flags["Prometheus target down: windows-pc (192.168.88.30:9182)"] == (
        "warn",
        "other host refused",
    )
    assert flags["Prometheus alert firing: DiskFull on node:9100"][0] == "crit"
    assert flags["Prometheus alert firing: Slow"][0] == "warn"
    assert flags["Prometheus alert firing: FYI"][0] == "info"
    assert not any("fine" in t or "user_q" in t for t in flags)
    assert "ALERT firing: DiskFull [critical] on node:9100 — Full" in section(d)
    assert "Target DOWN: windows-pc 192.168.88.30:9182 — refused" in section(d)


async def test_every_target_down_is_critical(db: Database) -> None:
    down = [{"job": "node", "instance": "a:1", "error": ""}]
    d = await digest_with(db, prom_data([], targets={"total": 1, "up": 0, "down": down}))
    assert [(f.severity) for f in d.flags] == ["crit"]


async def test_failing_queries_are_mentioned_not_flagged(db: Database) -> None:
    bad = {"name": "broken", "ok": False, "error": "parse error at char 5", "query": "x"}
    d = await digest_with(db, prom_data([bad, q("ok", [({}, 1.0)])]))
    assert "1 query(ies) failing: broken (parse error at char 5)" in section(d)
    assert d.flags == []


async def test_metric_values_do_not_change_the_report_fingerprint(db: Database) -> None:
    def digest_for(value: float):  # type: ignore[no-untyped-def]
        data = prom_data([q("m", [({}, value)], unit="count", warn_if="> 100")])
        return digest_with(db, data)

    a, b = await digest_for(41.0), await digest_for(58.0)
    assert a.hash == b.hash and a.text != b.text
    crossing = await digest_for(150.0)
    assert crossing.hash != a.hash  # a threshold breach is a flag, so the hash moves


async def test_no_prometheus_data_means_no_section(db: Database) -> None:
    d = await build_digest(snapshot(), db, ReportSettings(), now=NOW)
    assert "## Prometheus" not in d.text
    empty = await digest_with(db, prom_data([]))
    assert "## Prometheus" not in empty.text


async def test_many_queries_stay_compact(db: Database) -> None:
    queries = [
        q(f"m{i}", [({"instance": f"i{k}"}, float(k)) for k in range(30)], description="d" * 200)
        for i in range(25)
    ]
    d = await digest_with(db, prom_data(queries))
    body = section(d).splitlines()
    assert len([ln for ln in body if ln.startswith("Metric ")]) <= 14
    assert all(len(ln) < 260 for ln in body)
    assert "(+27 more)" in body[1]


# --------------------------------------------------------------------------- API


@pytest.fixture
def client(bootstrap: Bootstrap) -> Iterator[TestClient]:
    cfg = Settings(
        llm={"base_url": LLM_URL, "model": "m", "tool_mode": "none", "max_retries": 0},
        collectors={"prometheus": {"url": PROM, "queries": []}},
    )
    with respx.mock(assert_all_called=False) as router:
        router.route(host="prom.test").mock(side_effect=prom_with_answers())
        router.post(f"{LLM_URL}/chat/completions").mock(side_effect=FakeLLM(good_reply))
        with TestClient(create_app(bootstrap, collector_registry={}, seed_settings=cfg)) as c:
            yield c


def wait_plan(client: TestClient) -> dict[str, Any]:
    deadline = time.time() + 10
    while time.time() < deadline:
        body = client.get("/api/prometheus/queries").json()
        if not body["plan"]["running"] and body["plan"]["last"]:
            return body
        time.sleep(0.05)
    raise AssertionError("the plan did not finish")


def test_api_replan_list_edit_delete(client: TestClient) -> None:
    empty = client.get("/api/prometheus/queries").json()
    assert empty["queries"] == [] and empty["configured"] and empty["auto_queries"]
    assert client.post("/api/prometheus/replan").status_code == 202
    body = wait_plan(client)
    assert body["plan"]["last"]["status"] == "ok" and len(body["queries"]) == 4
    by_name = {x["name"]: x for x in body["queries"]}
    qid = by_name["net_errors"]["id"]

    patched = client.patch(
        f"/api/prometheus/queries/{qid}", json={"pinned": True, "enabled": False}
    )
    assert patched.status_code == 200
    assert patched.json()["pinned"] is True and patched.json()["enabled"] is False
    bad = client.patch(f"/api/prometheus/queries/{qid}", json={"warn_if": "lots"})
    assert bad.status_code == 422
    ok = client.patch(
        f"/api/prometheus/queries/{qid}", json={"warn_if": ">=  2", "name": "Net Err"}
    )
    assert ok.json()["warn_if"] == ">= 2" and ok.json()["name"] == "net_err" and ok.json()["edited"]
    clash = client.patch(f"/api/prometheus/queries/{qid}", json={"name": "gpu_temp"})
    assert clash.status_code == 409
    assert client.patch("/api/prometheus/queries/9999", json={"pinned": True}).status_code == 404

    assert client.delete(f"/api/prometheus/queries/{qid}").status_code == 204
    assert client.delete(f"/api/prometheus/queries/{qid}").status_code == 404
    left = client.get("/api/prometheus/queries").json()["queries"]
    assert len(left) == 3
    # a second replan does not bring the deleted one back
    assert client.post("/api/prometheus/replan").status_code == 202
    deadline = time.time() + 10
    while time.time() < deadline:
        state = client.get("/api/prometheus/queries").json()
        if (
            not state["plan"]["running"]
            and state["plan"]["last"]["ts"] > body["plan"]["last"]["ts"]
        ):
            break
        time.sleep(0.05)
    assert "net_err" not in {x["name"] for x in state["queries"]}


def test_replan_is_refused_when_auto_queries_are_off(bootstrap: Bootstrap) -> None:
    cfg = Settings(collectors={"prometheus": {"url": PROM, "auto_queries": False}})
    with TestClient(create_app(bootstrap, collector_registry={}, seed_settings=cfg)) as c:
        r = c.post("/api/prometheus/replan")
        assert r.status_code == 409 and "off" in r.json()["detail"]


async def test_long_descriptions_are_cut_at_a_word(db: Database) -> None:
    text = "Rate of failed things " * 10
    d = await digest_with(db, prom_data([q("m", [({}, 1.0)], description=text)]))
    line = next(ln for ln in section(d).splitlines() if ln.startswith("Metric m"))
    assert line.endswith(" of…") and len(line) < 140
