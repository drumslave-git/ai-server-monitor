from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import respx
from fastapi.testclient import TestClient

from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import SECRET_MASK, Bootstrap, Settings
from ai_server_monitor.main import create_app

from .conftest import LLM_URL, chat_response


class StubSystem(Collector):
    name = "system"

    async def collect(self) -> dict[str, Any]:
        return {"cpu": {"percent": 12.5}}


def test_healthz_and_info(bootstrap: Bootstrap, settings: Settings) -> None:
    app = create_app(bootstrap, collector_registry={}, seed_settings=settings)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        info = client.get("/api/info").json()
    assert info["schema_version"] >= 1
    assert info["llm"]["model"] == "test-model" and info["llm"]["configured"] is True
    assert {job["name"] for job in info["jobs"]} == {
        "prune",
        "llm_probe",
        "digest_observe",
        "report",
        "approvals:sweep",
    }
    assert (bootstrap.data_dir / "monitor.db").exists()


def test_first_start_uses_defaults_and_llm_unconfigured(bootstrap: Bootstrap) -> None:
    with TestClient(create_app(bootstrap, collector_registry={})) as client:
        info = client.get("/api/info").json()
        values = client.get("/api/settings").json()["values"]
    assert info["llm"]["configured"] is False
    assert values["llm"]["base_url"] == "" and values["report"]["interval"] == "5m"


def test_collectors_endpoints(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, collector_registry={"system": StubSystem})
    with TestClient(app) as client:
        ctx = app.state.ctx
        client.portal.call(ctx.collectors.run, "system")  # type: ignore[union-attr]
        listing = client.get("/api/collectors").json()
        one = client.get("/api/collectors/system").json()
        missing = client.get("/api/collectors/nope")
    assert listing["system"]["state"] == "active"
    assert listing["system"]["result"]["data"] == {"cpu": {"percent": 12.5}}
    assert one["ok"] is True and one["result"]["ok"] is True
    assert missing.status_code == 404


def test_settings_roundtrip_masks_secrets_and_applies_live(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, collector_registry={"system": StubSystem})
    with TestClient(app) as client:
        doc = client.get("/api/settings").json()
        assert doc["sections"]["llm"] == "LLM"
        schema = doc["schema"]
        assert "$defs" in schema and "llm" in schema["properties"]
        values = doc["values"]

        values["llm"].update(base_url="http://ollama:11434/v1", model="qwen3:8b", api_key="sk-1")
        values["collectors"]["system"]["interval"] = "45s"
        values["arr"]["radarr"] = [{"name": "radarr", "url": "http://radarr:7878", "api_key": "rk"}]
        saved = client.put("/api/settings", json=values)
        assert saved.status_code == 200, saved.text
        out = saved.json()["values"]
        assert out["llm"]["api_key"] == SECRET_MASK  # never returned
        assert out["arr"]["radarr"][0]["api_key"] == SECRET_MASK
        # Live: the collector job was rebuilt with the new interval.
        assert app.state.ctx.scheduler.jobs["collect:system"].interval.total_seconds() == 45

        # Sending the mask back keeps the stored secret; renaming nothing else changes.
        out["llm"]["temperature"] = 0.5
        assert client.put("/api/settings", json=out).status_code == 200
        stored = app.state.ctx.settings
        assert stored.llm.api_key.get_secret_value() == "sk-1"
        assert stored.arr.radarr[0].api_key.get_secret_value() == "rk"
        assert stored.llm.temperature == 0.5

        bad = dict(out, report={**out["report"], "interval": "five minutes"})
        resp = client.put("/api/settings", json=bad)
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["report", "interval"]
        assert app.state.ctx.settings.report.interval.total_seconds() == 300  # unchanged

    # Persisted across restarts.
    with TestClient(create_app(bootstrap, collector_registry={})) as client:
        again = client.get("/api/settings").json()["values"]
    assert again["llm"]["model"] == "qwen3:8b" and again["llm"]["api_key"] == SECRET_MASK


def test_settings_rejects_incomplete_instances_with_readable_errors(bootstrap: Bootstrap) -> None:
    with TestClient(create_app(bootstrap, collector_registry={})) as client:
        values = client.get("/api/settings").json()["values"]
        values["arr"]["radarr"] = [{"name": "", "url": "", "api_key": ""}]
        values["llm"]["base_url"] = "ollama:11434"
        resp = client.put("/api/settings", json=values)
    assert resp.status_code == 422
    errors = {".".join(e["loc"]): e["msg"] for e in resp.json()["detail"]}
    assert errors == {
        "llm.base_url": "Must be a URL starting with http:// or https://",
        "arr.radarr.0.name": "Required",
        "arr.radarr.0.url": "Must be a URL starting with http:// or https://",
        "arr.radarr.0.api_key": "API key is required",
    }


@respx.mock
def test_test_llm_endpoint(bootstrap: Bootstrap) -> None:
    respx.get(f"{LLM_URL}/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "test-model"}, {"id": "other"}]})
    )
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=chat_response("<think>hmm</think>OK")
    )
    with TestClient(create_app(bootstrap, collector_registry={})) as client:
        result = client.post(
            "/api/settings/test-llm",
            json={"base_url": LLM_URL, "model": "test-model", "api_key": "sk-x"},
        ).json()
        unconfigured = client.post("/api/settings/test-llm", json={"base_url": ""}).json()
    assert result["ok"] is True and result["reply"] == "OK"
    assert result["model_listed"] is True and result["models"] == ["test-model", "other"]
    assert json.loads(route.calls.last.request.content)["model"] == "test-model"
    assert unconfigured["ok"] is False


def test_serves_spa_when_static_dir_exists(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>spa</h1>")
    bootstrap = Bootstrap(data_dir=tmp_path / "d", host_root=tmp_path / "x", static_dir=static)
    with TestClient(create_app(bootstrap, collector_registry={})) as client:
        assert "spa" in client.get("/").text
        assert client.get("/healthz").status_code == 200  # API routes win over the mount


# --------------------------------------------------------------------------- reports API


def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("timed out waiting")


@respx.mock
def test_reports_run_list_get_and_digest(bootstrap: Bootstrap, settings: Settings) -> None:
    from .fake_llm import stream_response

    respx.post(f"{LLM_URL}/chat/completions").mock(return_value=stream_response())
    app = create_app(bootstrap, collector_registry={"system": StubSystem}, seed_settings=settings)
    with TestClient(app) as client:
        client.portal.call(app.state.ctx.collectors.run, "system")  # type: ignore[union-attr]
        assert client.get("/api/reports").json()["total"] == 0

        digest = client.get("/api/reports/digest").json()
        assert "SERVER DIGEST" in digest["text"] and digest["tokens"] > 0
        assert "Data sources" in client.get("/api/reports/digest?format=text").text

        started = client.post("/api/reports/run")
        assert started.status_code == 202 and started.json() == {"status": "started"}
        listing = _wait_for(lambda: (r := client.get("/api/reports").json())["total"] and r)
        item = listing["items"][0]
        assert item["status"] == "ok" and item["health"] == "warn" and item["score"] == 74
        assert "markdown" not in item and "digest" not in item  # list is slim

        report = client.get(f"/api/reports/{item['id']}").json()
        assert report["markdown"].startswith("## Overview") and report["findings"][0]["title"]
        assert client.get("/api/reports/9999").status_code == 404
        assert client.get("/api/reports?status=failed").json()["total"] == 0
        assert client.get("/api/reports?limit=0").status_code == 422


def test_run_report_conflicts_while_one_is_running(
    bootstrap: Bootstrap, settings: Settings
) -> None:
    app = create_app(bootstrap, collector_registry={}, seed_settings=settings)
    with TestClient(app) as client:
        job = app.state.ctx.scheduler.jobs["report"]
        job.running = True
        busy = client.post("/api/reports/run")
        job.running = False
    assert busy.status_code == 409


def test_unconfigured_llm_report_run_records_a_skip(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, collector_registry={})
    with TestClient(app) as client:
        assert client.post("/api/reports/run").status_code == 202
        listing = _wait_for(lambda: (r := client.get("/api/reports").json())["total"] and r)
    item = listing["items"][0]
    assert item["status"] == "skipped" and "not configured" in item["error"]


def test_status_before_any_report_and_with_unconfigured_llm(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, collector_registry={"system": StubSystem})
    with TestClient(app) as client:
        client.portal.call(app.state.ctx.collectors.run, "system")  # type: ignore[union-attr]
        first = client.get("/api/status").json()
        assert client.post("/api/reports/run").status_code == 202
        _wait_for(lambda: client.get("/api/reports").json()["total"])
        second = client.get("/api/status").json()
    assert first["report"] is None and first["last_attempt"] is None
    assert first["llm_configured"] is False and first["version"]
    assert first["collectors"]["system"]["state"] == "active"
    assert first["resources"]["cpu_percent"] == 12.5 and first["resources"]["gpu"] is None
    assert first["next_report"]["interval_s"] == 300
    assert second["report"] is None  # a skip is not a report ...
    assert second["last_attempt"]["status"] == "skipped"  # ... but it is visible
    assert "not configured" in second["last_attempt"]["error"]


def test_settings_responses_are_typed_and_still_mask_secrets(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap, collector_registry={})
    with TestClient(app) as client:
        doc = client.get("/api/settings").json()
        assert set(doc) == {"values", "schema", "sections"}
        values = doc["values"]
        values["llm"]["api_key"] = "sk-secret"
        saved = client.put("/api/settings", json=values).json()
        assert saved["values"]["llm"]["api_key"] == SECRET_MASK
        assert client.get("/api/settings").json()["values"]["llm"]["api_key"] == SECRET_MASK
