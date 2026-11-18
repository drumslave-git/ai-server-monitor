from __future__ import annotations

import re
import time
from typing import Any

import respx
from fastapi.testclient import TestClient

from ai_server_monitor.collectors.base import CollectorResult
from ai_server_monitor.config import SECRET_MASK, Bootstrap, Settings
from ai_server_monitor.llm.client import LLMStats, Usage
from ai_server_monitor.main import create_app
from ai_server_monitor.outputs.metrics import (
    CONTENT_TYPE,
    Family,
    MetricsHub,
    Sample,
    render_families,
    report_families,
)

from .conftest import LLM_URL
from .fake_llm import stream_response
from .test_app import StubSystem, _wait_for


def parse(text: str) -> dict[str, float]:
    """'name{a="b"} 1' lines -> {'name{a="b"}': 1.0} (skips comments)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            series, _, value = line.rpartition(" ")
            out[series] = float(value.replace("+Inf", "inf"))
    return out


def test_text_format_escaping_and_numbers() -> None:
    text = render_families(
        [
            Family(
                "asm_x",
                "gauge",
                "Help with \\ and\nnewline.",
                [
                    Sample(1.5, {"l": 'a"b\\c\nd'}),
                    Sample(float("inf")),
                    Sample(float("nan"), {"k": "v"}),
                    Sample(3.0, {"k": "w"}),
                ],
            ),
            Family("asm_empty", "gauge", "no samples: not rendered", []),
        ]
    )
    assert text == (
        "# HELP asm_x Help with \\\\ and\\nnewline.\n"
        "# TYPE asm_x gauge\n"
        'asm_x{l="a\\"b\\\\c\\nd"} 1.5\n'
        "asm_x +Inf\n"
        'asm_x{k="v"} NaN\n'
        'asm_x{k="w"} 3\n'
    )


async def test_fresh_hub_exposes_zeroed_counters_and_an_empty_histogram() -> None:
    series = parse(await MetricsHub().render())
    assert series['asm_reports_total{result="ok"}'] == 0
    assert series['asm_llm_tokens_total{direction="prompt"}'] == 0
    assert series['asm_llm_request_duration_seconds_bucket{le="+Inf"}'] == 0
    assert series["asm_llm_request_duration_seconds_count"] == 0


async def test_hooks_update_instruments() -> None:
    hub = MetricsHub()
    stats = LLMStats(on_success=hub.on_llm_success, on_error=hub.on_llm_error)
    stats.record_success(Usage(100, 40), 1.5)
    stats.record_success(Usage(10, 5), 70.0)
    stats.record_error(ValueError("x"))
    hub.on_collector_result(CollectorResult("docker", 1000.0, ok=True, duration_s=0.25))
    hub.on_collector_result(CollectorResult("gpu", 1000.0, ok=False, error="boom"))
    hub.on_collector_result(CollectorResult("gpu", 1001.0, ok=False, error="boom"))
    hub.on_report_event("report.done", {})
    hub.on_report_event("report.failed", {"status": "skipped"})
    hub.on_report_event("report.token", {"text": "ignored"})
    s = parse(await hub.render())

    assert s["asm_llm_request_duration_seconds_count"] == 2
    assert s['asm_llm_request_duration_seconds_bucket{le="2"}'] == 1  # cumulative
    assert s['asm_llm_request_duration_seconds_bucket{le="120"}'] == 2
    assert s["asm_llm_request_duration_seconds_sum"] == 71.5
    assert s['asm_llm_tokens_total{direction="prompt"}'] == 110
    assert s['asm_llm_tokens_total{direction="completion"}'] == 45
    assert s['asm_llm_errors_total{type="ValueError"}'] == 1
    assert s['asm_llm_requests_total{result="ok"}'] == 2
    assert s['asm_llm_requests_total{result="error"}'] == 1
    assert s['asm_collector_up{collector="docker"}'] == 1
    assert s['asm_collector_up{collector="gpu"}'] == 0
    assert s['asm_collector_duration_seconds{collector="docker"}'] == 0.25
    assert s['asm_collector_errors_total{collector="gpu"}'] == 2
    assert s['asm_collector_errors_total{collector="docker"}'] == 0
    assert s['asm_collector_last_success_timestamp_seconds{collector="docker"}'] == 1000
    assert 'asm_collector_last_success_timestamp_seconds{collector="gpu"}' not in s
    assert s['asm_reports_total{result="ok"}'] == 1
    assert s['asm_reports_total{result="skipped"}'] == 1

    hub.reset_collectors()  # collectors rebuilt: up/duration series dropped, counters kept
    again = parse(await hub.render())
    assert not [k for k in again if k.startswith("asm_collector_up")]
    assert again['asm_collector_errors_total{collector="gpu"}'] == 2


async def test_providers_sync_async_and_broken() -> None:
    hub = MetricsHub()
    hub.add_provider(lambda: [Family("asm_pending_approvals", "gauge", "h", [Sample(2)])])

    async def extra() -> list[Family]:
        return [Family("asm_extra_gauge", "gauge", "h", [Sample(5, {"app": "x"})])]

    def broken() -> list[Family]:
        raise RuntimeError("provider bug")

    hub.add_provider(extra)
    hub.add_provider(broken)
    s = parse(await hub.render())
    assert s["asm_pending_approvals"] == 2 and s['asm_extra_gauge{app="x"}'] == 5
    assert "asm_reports_total{" in "".join(s)  # one bad provider does not hide the rest


def test_report_families() -> None:
    latest = {
        "ts": 1234.5,
        "health": "warn",
        "score": 74,
        "latency_ms": 2500,
        "findings": [{"severity": "warn"}, {"severity": "warn"}, {"severity": "info"}],
    }
    s = parse(render_families(report_families(latest, "1.2.3")))
    assert s['asm_build_info{version="1.2.3"}'] == 1
    assert s["asm_health_score"] == 74
    assert [s[f'asm_health_status{{status="{x}"}}'] for x in ("ok", "warn", "crit")] == [0, 1, 0]
    assert s['asm_findings{severity="warn"}'] == 2 and s['asm_findings{severity="crit"}'] == 0
    assert s["asm_report_last_success_timestamp_seconds"] == 1234.5
    assert s["asm_report_generation_seconds"] == 2.5
    only_info = parse(render_families(report_families(None, "1")))
    assert list(only_info) == ['asm_build_info{version="1"}']  # nothing invented before a report


# --------------------------------------------------------------------------- endpoint


@respx.mock
def test_scrape_after_a_report(bootstrap: Bootstrap, settings: Settings) -> None:
    respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response())
    app = create_app(bootstrap, collector_registry={"system": StubSystem}, seed_settings=settings)
    with TestClient(app) as client:
        client.portal.call(app.state.ctx.collectors.run, "system")  # type: ignore[union-attr]
        before = client.get("/metrics")
        assert before.headers["content-type"] == CONTENT_TYPE
        assert "asm_health_score" not in before.text  # no report yet: nothing made up
        assert parse(before.text)['asm_collector_up{collector="system"}'] == 1

        assert client.post("/api/reports/run").status_code == 202
        _wait_for(lambda: client.get("/api/reports").json()["total"])
        time.sleep(0.2)  # the metrics consumer reads the bus asynchronously
        s = parse(client.get("/metrics").text)

    assert s["asm_health_score"] == 74
    assert s['asm_health_status{status="warn"}'] == 1 and s['asm_health_status{status="ok"}'] == 0
    assert s['asm_findings{severity="warn"}'] == 2 and s['asm_findings{severity="info"}'] == 1
    assert s["asm_report_last_success_timestamp_seconds"] > 1e9
    assert s['asm_reports_total{result="ok"}'] == 1
    # The report was one streamed LLM call: tokens and duration are counted.
    assert s['asm_llm_tokens_total{direction="prompt"}'] == 900
    assert s['asm_llm_tokens_total{direction="completion"}'] == 250
    assert s["asm_llm_request_duration_seconds_count"] == 1
    assert s['asm_collector_last_success_timestamp_seconds{collector="system"}'] > 1e9


def test_metrics_can_be_disabled_and_protected_by_a_token(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, collector_registry={})
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 200  # on by default, no token

        doc: dict[str, Any] = client.get("/api/settings").json()["values"]
        doc["outputs"]["metrics"]["bearer_token"] = "s3cret"
        saved = client.put("/api/settings", json=doc).json()["values"]
        assert saved["outputs"]["metrics"]["bearer_token"] == SECRET_MASK
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
        ok = client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and "asm_build_info" in ok.text

        doc["outputs"]["metrics"]["enabled"] = False
        client.put("/api/settings", json=doc)
        assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 404


def test_metric_names_follow_prometheus_conventions(bootstrap: Bootstrap) -> None:
    """Cheap stand-in for promtool: counters end in _total, nothing else does."""
    hub = MetricsHub()
    for fam in hub.families():
        assert re.fullmatch(r"asm_[a-z_]+", fam.name)
        assert fam.name.endswith("_total") == (fam.type == "counter")
