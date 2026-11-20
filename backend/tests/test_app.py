from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from ai_server_monitor.config import SECRET_MASK, Bootstrap, Settings
from ai_server_monitor.jobs.builtins import BUILTINS
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .test_chat import call, streamed


def test_healthz_and_info(bootstrap: Bootstrap, settings: Settings) -> None:
    app = create_app(bootstrap, seed_settings=settings)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        info = client.get("/api/info").json()
    assert info["schema_version"] >= 1
    assert info["llm"]["model"] == "test-model" and info["llm"]["configured"] is True
    assert info["llm"]["worker_model"] == "test-model"
    assert "collectors" not in info and "tool_mode" not in info["llm"]
    assert {job["name"] for job in info["jobs"]} == {
        "prune",
        "trace_prune",
        "approvals:sweep",
        "approvals:drain",
        # only jobs with a schedule of their own: the summaries run their jobs
        *(f"job:{d.slug}" for d in BUILTINS if d.interval_s or d.cron),
    }
    assert (bootstrap.data_dir / "monitor.db").exists()


def test_first_start_uses_defaults_and_llm_unconfigured(bootstrap: Bootstrap) -> None:
    with TestClient(create_app(bootstrap)) as client:
        info = client.get("/api/info").json()
        values = client.get("/api/settings").json()["values"]
    assert info["llm"]["configured"] is False
    assert values["llm"]["base_url"] == "" and values["general"]["language"] == "en"
    assert "report" not in values and "collectors" not in values


def test_settings_roundtrip_masks_secrets_and_applies_live(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap)
    with TestClient(app) as client:
        doc = client.get("/api/settings").json()
        assert doc["sections"]["llm"] == "Main model"
        assert doc["sections"]["worker"] == "Worker model"
        schema = doc["schema"]
        assert "$defs" in schema and "llm" in schema["properties"]
        values = doc["values"]

        values["llm"].update(base_url="http://ollama:11434/v1", model="qwen3:8b", api_key="sk-1")
        values["general"]["language"] = "de"
        values["mcp"]["servers"] = [{"name": "ha", "url": "http://ha:8123/mcp", "token": "rk"}]
        saved = client.put("/api/settings", json=values)
        assert saved.status_code == 200, saved.text
        out = saved.json()["values"]
        assert out["llm"]["api_key"] == SECRET_MASK  # never returned
        assert out["mcp"]["servers"][0]["token"] == SECRET_MASK
        assert app.state.ctx.settings.general.language == "de"

        # Sending the mask back keeps the stored secret; renaming nothing else changes.
        out["llm"]["temperature"] = 0.5
        assert client.put("/api/settings", json=out).status_code == 200
        stored = app.state.ctx.settings
        assert stored.llm.api_key.get_secret_value() == "sk-1"
        assert stored.mcp.servers[0].token.get_secret_value() == "rk"
        assert stored.llm.temperature == 0.5

        bad = dict(out, storage={**out["storage"], "retention": "a while"})
        resp = client.put("/api/settings", json=bad)
        assert resp.status_code == 422
        assert resp.json()["detail"][0]["loc"] == ["storage", "retention"]
        assert app.state.ctx.settings.general.language == "de"  # unchanged

    # Persisted across restarts.
    with TestClient(create_app(bootstrap)) as client:
        again = client.get("/api/settings").json()["values"]
    assert again["llm"]["model"] == "qwen3:8b" and again["llm"]["api_key"] == SECRET_MASK


def test_settings_rejects_incomplete_instances_with_readable_errors(bootstrap: Bootstrap) -> None:
    with TestClient(create_app(bootstrap)) as client:
        values = client.get("/api/settings").json()["values"]
        values["mcp"]["servers"] = [
            {"name": "ha", "url": ""},
            {"name": "", "url": "http://ok/mcp"},
            {"name": "home_assistant", "url": "http://ok/mcp"},
        ]
        values["llm"]["base_url"] = "ollama:11434"
        resp = client.put("/api/settings", json=values)
        test = client.post("/api/settings/test/mcp", json={"name": " ", "url": "http://ok/mcp"})
    assert resp.status_code == 422
    errors = {".".join(e["loc"]): e["msg"] for e in resp.json()["detail"]}
    assert errors == {
        "llm.base_url": "Must be a URL starting with http:// or https://",
        "mcp.servers.0.url": "Must be a URL starting with http:// or https://",
        "mcp.servers.1.name": "Required",
        "mcp.servers.2.name": "May only use letters, digits and '-' (up to 24, starting with a "
        "letter or digit)",
    }
    # the connection test says what is wrong, not "invalid URL"
    assert test.json()["ok"] is False and "name is required" in test.json()["message"]


@respx.mock
def test_test_llm_endpoint(bootstrap: Bootstrap, shell: list[str]) -> None:
    respx.get(f"{LLM_URL}/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "test-model"}, {"id": "other"}]})
    )
    route = respx.post(f"{LLM_URL}/chat/completions").mock(
        side_effect=[
            streamed(call("bash", {"command": "echo check-1"})),
            streamed({"content": "OK"}),
        ]
    )
    with TestClient(create_app(bootstrap)) as client:
        result = client.post(
            "/api/settings/test-llm",
            json={"base_url": LLM_URL, "model": "test-model", "api_key": "sk-x"},
        ).json()
        unconfigured = client.post("/api/settings/test-llm", json={"base_url": ""}).json()
    assert result["ok"] is True and shell == ["echo check-1"], result
    assert result["model_listed"] is True and result["models"] == ["test-model", "other"]
    assert result["tool_call"] is True
    assert json.loads(route.calls.last.request.content)["model"] == "test-model"
    assert unconfigured["ok"] is False


def test_serves_spa_when_static_dir_exists(tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>spa</h1>")
    bootstrap = Bootstrap(data_dir=tmp_path / "d", host_root=tmp_path / "x", static_dir=static)
    with TestClient(create_app(bootstrap)) as client:
        assert "spa" in client.get("/").text
        assert client.get("/healthz").status_code == 200  # API routes win over the mount


def test_status_shape(bootstrap: Bootstrap, settings: Settings) -> None:
    with TestClient(create_app(bootstrap, seed_settings=settings)) as client:
        status = client.get("/api/status").json()
    assert set(status) == {
        "version",
        "uptime_s",
        "now",
        "llm_configured",
        "main_model",
        "worker_model",
        "pending_approvals",
        "automation",
        "overall",
        "jobs",
        "change_shell",
    }
    assert status["llm_configured"] is True and status["main_model"] == "test-model"
    assert status["worker_model"] == "test-model" and status["pending_approvals"] == 0
    assert status["automation"]["paused"] is False
    assert status["overall"] is None  # no summary has run yet
    assert {j["slug"] for j in status["jobs"]} >= {"summary:health", "storage:usage"}


def test_settings_responses_are_typed_and_still_mask_secrets(bootstrap: Bootstrap) -> None:
    app = create_app(bootstrap)
    with TestClient(app) as client:
        doc = client.get("/api/settings").json()
        assert set(doc) == {"values", "schema", "sections"}
        values = doc["values"]
        values["llm"]["api_key"] = "sk-secret"
        saved = client.put("/api/settings", json=values).json()
        assert saved["values"]["llm"]["api_key"] == SECRET_MASK
        assert client.get("/api/settings").json()["values"]["llm"]["api_key"] == SECRET_MASK
