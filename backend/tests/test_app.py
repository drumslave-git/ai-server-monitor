from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from servermon.config import Settings
from servermon.main import create_app


def test_healthz_and_info(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        info = client.get("/api/info").json()
    assert info["schema_version"] >= 1
    assert info["llm"]["model"] == "test-model"
    assert {job["name"] for job in info["jobs"]} == {"prune", "llm_probe"}
    assert "api_key" not in str(info)


def test_serves_spa_when_static_dir_exists(settings: Settings, tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>spa</h1>")
    settings.server.static_dir = static
    with TestClient(create_app(settings)) as client:
        assert "spa" in client.get("/").text
        assert client.get("/healthz").status_code == 200  # API routes win over the mount
