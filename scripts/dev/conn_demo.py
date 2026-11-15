"""The real app + fake services, to look at the Settings → Test buttons. Development only.

    cd backend && python ../scripts/dev/conn_demo.py [--port 8097] [--fake-port 9197]

Everything the Test buttons call is answered by a tiny fake server on 127.0.0.1 (a Radarr that
works, a "Sonarr" that is really Radarr, a Whisparr that rejects the key, a qBittorrent that
refuses the password, Prometheus, Docker, a Telegram Bot API with one chat that was never
started). Nothing here talks to a real service.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from ai_server_monitor.config import Bootstrap, Settings  # noqa: E402
from ai_server_monitor.main import create_app  # noqa: E402

TOKEN = "123456:DEMO-fake-token-not-real"


def fake_services() -> FastAPI:
    app = FastAPI()

    @app.get("/radarr/api/v3/system/status")
    def radarr() -> dict[str, str]:
        return {"appName": "Radarr", "version": "5.14.0.9383"}

    @app.get("/sonarr/api/v3/system/status")
    def not_sonarr() -> dict[str, str]:  # a Radarr URL pasted into the Sonarr slot
        return {"appName": "Radarr", "version": "5.14.0.9383"}

    @app.get("/whisparr/api/v3/system/status")
    def whisparr() -> JSONResponse:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    @app.get("/prowlarr/api/v1/system/status")
    def prowlarr() -> dict[str, str]:
        return {"appName": "Prowlarr", "version": "1.28.2.4885"}

    @app.post("/qbit/api/v2/auth/login")
    def qbit_login() -> PlainTextResponse:
        return PlainTextResponse("Fails.")

    @app.get("/prom/api/v1/status/buildinfo")
    def buildinfo() -> dict[str, Any]:
        return {"status": "success", "data": {"version": "2.53.0"}}

    @app.get("/prom/api/v1/query")
    def query() -> dict[str, Any]:
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    @app.get("/docker/version")
    def docker() -> dict[str, str]:
        return {"Version": "27.3.1", "ApiVersion": "1.47", "Os": "linux"}

    @app.post("/tg/bot{token}/{method}")
    async def telegram(token: str, method: str, request: Request) -> JSONResponse:
        if token != TOKEN:
            return JSONResponse(
                {"ok": False, "error_code": 401, "description": "Unauthorized"}, status_code=401
            )
        body = await request.json() if request.headers.get("content-length") else {}
        if method == "getMe":
            return JSONResponse({"ok": True, "result": {"id": 1, "username": "demo_asm_bot"}})
        if body.get("chat_id") == 222:
            return JSONResponse(
                {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
                status_code=400,
            )
        return JSONResponse({"ok": True, "result": {"message_id": 1}})

    return app


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8097)
    parser.add_argument("--fake-port", type=int, default=9197)
    parser.add_argument("--static", default=str(ROOT / "frontend" / "dist"))
    args = parser.parse_args()
    fake = f"http://127.0.0.1:{args.fake_port}"

    settings = Settings.model_validate(
        {
            "arr": {
                "radarr": [
                    {"name": "radarr", "url": f"{fake}/radarr", "api_key": "demo-key-radarr"}
                ],
                "sonarr": [{"name": "sonarr", "url": f"{fake}/sonarr", "api_key": "demo-key-sonarr"}],
                "whisparr": [
                    {"name": "whisparr", "url": f"{fake}/whisparr", "api_key": "demo-key-whisparr"}
                ],
                "prowlarr": {
                    "name": "prowlarr", "url": f"{fake}/prowlarr", "api_key": "demo-key-prowlarr"
                },
                "qbittorrent": {"url": f"{fake}/qbit", "username": "admin", "password": "wrong"},
            },
            "collectors": {
                "prometheus": {"url": f"{fake}/prom"},
                "docker": {"url": f"{fake}/docker"},
            },
            "outputs": {
                "telegram": {
                    "token": TOKEN,
                    "api_url": f"{fake}/tg",
                    "allowed_chat_ids": [111, 222],
                }
            },
        }
    )
    tmp = Path(tempfile.mkdtemp(prefix="asm-conn-demo-"))
    app = create_app(
        Bootstrap(data_dir=tmp / "data", host_root=tmp / "none", static_dir=Path(args.static)),
        collector_registry={},
        seed_settings=settings,
    )
    servers = [
        uvicorn.Server(uvicorn.Config(fake_services(), host="127.0.0.1", port=args.fake_port, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")),
    ]  # fmt: skip
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers):
        await asyncio.sleep(0.1)
    print(f"ready: http://127.0.0.1:{args.port}/#/settings/arr  (fakes on {fake})", flush=True)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
