from __future__ import annotations

import re
import time
from typing import Any

from fastapi.testclient import TestClient

from ai_server_monitor import __version__
from ai_server_monitor.config import SECRET_MASK, Bootstrap
from ai_server_monitor.main import create_app
from ai_server_monitor.outputs.metrics import (
    CONTENT_TYPE,
    Family,
    MetricsHub,
    Sample,
    build_info_families,
    render_families,
)

from .test_chat import FakeLLM, ask, make_app, say


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
    assert series['asm_llm_requests_total{result="ok"}'] == 0
    assert not [k for k in series if "collector" in k or "report" in k]
    assert series['asm_llm_tokens_total{direction="prompt"}'] == 0
    assert series['asm_llm_request_duration_seconds_bucket{le="+Inf"}'] == 0
    assert series["asm_llm_request_duration_seconds_count"] == 0


async def test_llm_call_events_update_instruments() -> None:
    hub = MetricsHub()
    hub.on_llm_call(
        {"status": "ok", "total_ms": 1500, "prompt_tokens": 100, "completion_tokens": 40}
    )
    hub.on_llm_call(
        {"status": "ok", "total_ms": 70000, "prompt_tokens": 10, "completion_tokens": 5}
    )
    hub.on_llm_call({"status": "error", "total_ms": 20, "error_type": "ValueError"})
    hub.on_llm_call({"status": "error", "error_type": None})
    s = parse(await hub.render())

    assert s["asm_llm_request_duration_seconds_count"] == 2
    assert s['asm_llm_request_duration_seconds_bucket{le="2"}'] == 1  # cumulative
    assert s['asm_llm_request_duration_seconds_bucket{le="120"}'] == 2
    assert s["asm_llm_request_duration_seconds_sum"] == 71.5
    assert s['asm_llm_tokens_total{direction="prompt"}'] == 110
    assert s['asm_llm_tokens_total{direction="completion"}'] == 45
    assert s['asm_llm_errors_total{type="ValueError"}'] == 1
    assert s['asm_llm_errors_total{type="error"}'] == 1
    assert s['asm_llm_requests_total{result="ok"}'] == 2
    assert s['asm_llm_requests_total{result="error"}'] == 2


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
    assert "asm_llm_requests_total{" in "".join(s)  # one bad provider does not hide the rest


def test_build_info_family() -> None:
    s = parse(render_families(build_info_families("1.2.3")))
    assert s == {'asm_build_info{version="1.2.3"}': 1}


# --------------------------------------------------------------------------- endpoint


def test_scrape_after_a_chat_counts_its_llm_calls(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM(say("All good.")))
    with TestClient(app) as client:
        before = client.get("/metrics")
        assert before.headers["content-type"] == CONTENT_TYPE
        assert parse(before.text)['asm_llm_requests_total{result="ok"}'] == 0
        ask(client, "how is it?")
        time.sleep(0.3)  # the metrics consumer reads the bus asynchronously
        s = parse(client.get("/metrics").text)
    assert s['asm_llm_requests_total{result="ok"}'] >= 1
    assert s['asm_llm_tokens_total{direction="prompt"}'] >= 10
    assert s["asm_llm_request_duration_seconds_count"] >= 1
    assert s['asm_build_info{version="' + __version__ + '"}'] == 1
    assert not [k for k in s if "collector" in k or "report" in k or "health" in k]


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


def test_metric_names_follow_prometheus_conventions(bootstrap: Bootstrap) -> None:
    """Cheap stand-in for promtool: counters end in _total, nothing else does."""
    hub = MetricsHub()
    for fam in hub.families():
        assert re.fullmatch(r"asm_[a-z_]+", fam.name)
        assert fam.name.endswith("_total") == (fam.type == "counter")
