from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from servermon.collectors.base import Collector
from servermon.config import Settings
from servermon.main import create_app


class StubSystem(Collector):
    name = "system"

    async def collect(self) -> dict[str, Any]:
        return {"cpu": {"percent": 12.5}}


def test_healthz_and_info(settings: Settings) -> None:
    with TestClient(create_app(settings, collector_registry={})) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        info = client.get("/api/info").json()
    assert info["schema_version"] >= 1
    assert info["llm"]["model"] == "test-model"
    assert {job["name"] for job in info["jobs"]} == {"prune", "llm_probe"}
    assert "api_key" not in str(info)


def test_collectors_endpoints(settings: Settings) -> None:
    app = create_app(settings, collector_registry={"system": StubSystem})
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


def test_serves_spa_when_static_dir_exists(settings: Settings, tmp_path: Path) -> None:
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<h1>spa</h1>")
    settings.server.static_dir = static
    with TestClient(create_app(settings, collector_registry={})) as client:
        assert "spa" in client.get("/").text
        assert client.get("/healthz").status_code == 200  # API routes win over the mount
