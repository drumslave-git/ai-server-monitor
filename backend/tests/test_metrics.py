from __future__ import annotations

import re
import time
from typing import Any

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from ai_server_monitor import __version__
from ai_server_monitor.config import SECRET_MASK, Bootstrap
from ai_server_monitor.main import create_app
from ai_server_monitor.outputs.metrics import CONTENT_TYPE, MetricsHub

from .jobfakes import ScriptedModels, finish
from .test_chat import FakeLLM, ask, make_app, say
from .test_jobs import make_app as make_jobs_app


def parse(text: str) -> dict[str, float]:
    """'name{a="b"} 1' lines -> {'name{a="b"}': 1.0} (skips comments)."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            series, _, value = line.rpartition(" ")
            out[series] = float(value.replace("+Inf", "inf"))
    return out


def scrape(client: TestClient, wait: float = 0.3) -> dict[str, float]:
    time.sleep(wait)  # the metrics consumer reads the event bus asynchronously
    return parse(client.get("/metrics").text)


def test_fresh_hub_exposes_zeroed_counters_and_nothing_of_the_process() -> None:
    s = parse(MetricsHub("1.2.3").render().decode())
    assert s['asm_llm_requests_total{result="ok"}'] == 0
    assert s['asm_llm_tokens_total{direction="completion"}'] == 0
    assert s['asm_build_info{version="1.2.3"}'] == 1
    assert s["asm_pending_approvals"] == 0
    assert not [k for k in s if k.startswith(("process_", "python_")) or "_created" in k]


def test_llm_call_events_update_the_series() -> None:
    hub = MetricsHub()
    hub.on_llm_call({"status": "ok", "total_ms": 1500, "prompt_tokens": 7, "completion_tokens": 3})
    hub.on_llm_call({"status": "error", "error_type": "timeout"})
    s = parse(hub.render().decode())
    assert s['asm_llm_requests_total{result="ok"}'] == 1
    assert s['asm_llm_requests_total{result="error"}'] == 1
    assert s['asm_llm_errors_total{type="timeout"}'] == 1
    assert s['asm_llm_tokens_total{direction="prompt"}'] == 7
    assert s['asm_llm_request_duration_seconds_bucket{le="2.0"}'] == 1
    assert s["asm_llm_request_duration_seconds_sum"] == 1.5


def test_scrape_after_a_chat_counts_its_llm_calls(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM(say("All good.")))
    with TestClient(app) as client:
        before = client.get("/metrics")
        assert before.headers["content-type"] == CONTENT_TYPE
        assert parse(before.text)['asm_llm_requests_total{result="ok"}'] == 0
        ask(client, "how is it?")
        s = scrape(client)
    assert s['asm_llm_requests_total{result="ok"}'] >= 1
    assert s['asm_llm_tokens_total{direction="prompt"}'] >= 10
    assert s["asm_llm_request_duration_seconds_count"] >= 1
    assert s['asm_build_info{version="' + __version__ + '"}'] == 1


def test_job_runs_show_up_as_series(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    findings = [
        {"severity": "crit", "title": "/ is 97% full", "detail": ""},
        {"severity": "warn", "title": "/srv is 90% full", "detail": ""},
        {"severity": "warn", "title": "inodes", "detail": ""},
    ]
    app = make_jobs_app(bootstrap)
    with TestClient(app) as client:
        models.script = [finish("crit", "Disks are full.", findings)]
        storage = client.portal.call(app.state.ctx.jobs.run, "storage", "manual")  # type: ignore[union-attr]
        models.script = [finish("ok", "Fine.")]
        client.portal.call(app.state.ctx.jobs.run, "docker", "manual")  # type: ignore[union-attr]
        s = scrape(client)
        assert s['asm_job_status{job="storage"}'] == 2 and s['asm_job_status{job="docker"}'] == 0
        assert s['asm_job_findings{job="storage",severity="crit"}'] == 1
        assert s['asm_job_findings{job="storage",severity="warn"}'] == 2
        assert s['asm_job_findings{job="docker",severity="warn"}'] == 0
        assert s['asm_job_runs_total{job="storage",status="crit"}'] == 1
        assert s['asm_job_last_run_timestamp_seconds{job="storage"}'] == storage["finished"]
        assert s['asm_job_last_run_duration_seconds{job="storage"}'] >= 0
        assert 'asm_job_status{job="journal"}' not in s  # never ran
    # after a restart the latest runs are there again (the run counters start from zero)
    with TestClient(make_jobs_app(bootstrap)) as client:
        s = scrape(client, 0)
        assert s['asm_job_status{job="storage"}'] == 2
        assert 'asm_job_runs_total{job="storage",status="crit"}' not in s


def test_metrics_can_be_disabled_and_protected_by_a_token(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap)
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


def test_the_exposition_parses_and_names_follow_conventions() -> None:
    hub = MetricsHub("1")
    hub.on_action("mcp_call", "pending")
    hub.set_job_run({"job_slug": "a", "status": "warn", "findings": []}, count=True)
    families = list(text_string_to_metric_families(hub.render().decode()))
    assert {f.name for f in families} >= {
        "asm_llm_requests",
        "asm_actions",
        "asm_job_status",
        "asm_job_runs",
        "asm_pending_approvals",
    }
    for family in families:
        assert re.fullmatch(r"asm_[a-z_]+", family.name)
        for sample in family.samples:
            assert sample.name.endswith("_total") == (family.type == "counter")
