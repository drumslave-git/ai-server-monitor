"""Run the real app with demo data (for screenshots and UI work). Development only.

    cd backend && python ../scripts/dev/demo_server.py [--port 8099] [--llm-url URL --llm-model NAME]

* Host collectors replay `docs/samples/collectors-live.json` (a snapshot of a real server,
  with a little jitter on CPU/network so the live charts move).
* Reports are real: they go to the LLM given by --llm-url/--llm-model (none = unconfigured).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from ai_server_monitor.collectors.base import Collector  # noqa: E402
from ai_server_monitor.config import Bootstrap, Settings  # noqa: E402
from ai_server_monitor.main import create_app  # noqa: E402

FIXTURE = ROOT / "docs" / "samples" / "collectors-live.json"

IMAGE_UPDATES = {
    "summary": {"total": 36, "up_to_date": 31, "update_available": 2, "unknown": 3},
    "updates_available": ["ghcr.io/ggml-org/llama.cpp:server-cuda", "grafana/grafana:latest"],
    "images": [
        {"image": "ghcr.io/ggml-org/llama.cpp:server-cuda", "containers": ["llamacpp-embedding"],
         "status": "update_available", "reason": None},
        {"image": "grafana/grafana:latest", "containers": ["grafana"],
         "status": "update_available", "reason": None},
        {"image": "traefik:latest", "containers": ["traefik"], "status": "up_to_date", "reason": None},
    ],
}  # fmt: skip


def jitter(data: dict, name: str) -> dict:
    if name != "system":
        return data
    out = json.loads(json.dumps(data))
    cpu = out["cpu"]
    cpu["percent"] = round(max(1.0, min(97.0, cpu["percent"] + random.uniform(-4, 14))), 1)
    cpu["load"] = [round(max(0.05, v + random.uniform(-0.15, 0.35)), 2) for v in cpu["load"]]
    out["memory"]["percent"] = round(out["memory"]["percent"] + random.uniform(-0.3, 0.6), 1)
    for nic in out.get("network", []):
        nic["rx_bps"] = round(nic["rx_bps"] * random.uniform(0.5, 4))
        nic["tx_bps"] = round(nic["tx_bps"] * random.uniform(0.5, 3))
    return out


def replay(name: str, data: dict) -> type[Collector]:
    async def collect(self) -> dict:  # type: ignore[no-untyped-def]
        return jitter(data, name)

    return type(f"Replay_{name}", (Collector,), {"name": name, "collect": collect})


def make_registry() -> dict[str, type[Collector]]:
    live = json.loads(FIXTURE.read_text(encoding="utf-8"))
    registry = {
        name: replay(name, entry["result"]["data"])
        for name, entry in live.items()
        if entry.get("result") and entry["result"].get("ok")
    }
    registry["image_updates"] = replay("image_updates", IMAGE_UPDATES)
    return registry


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--llm-url", default="")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--tool-mode", default="none", choices=["auto", "native", "json", "none"])
    parser.add_argument("--static", default=str(ROOT / "frontend" / "dist"))
    args = parser.parse_args()

    settings = Settings()
    settings.llm.base_url = args.llm_url
    settings.llm.model = args.llm_model
    settings.llm.tool_mode = args.tool_mode
    settings.report.interval = timedelta(hours=1)
    for name in ("system", "docker", "gpu", "systemd", "journal", "apt"):
        getattr(settings.collectors, name).interval = timedelta(seconds=15 if name == "system" else 60)

    tmp = Path(tempfile.mkdtemp(prefix="asm-demo-"))
    app = create_app(
        Bootstrap(data_dir=tmp / "data", host_root=tmp / "none", static_dir=Path(args.static)),
        collector_registry=make_registry(),
        seed_settings=settings,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.1)
    print(f"demo ready on http://127.0.0.1:{args.port}  (data in {tmp})", flush=True)
    await task


if __name__ == "__main__":
    asyncio.run(main())
