"""Run the real app with demo data (for screenshots and UI work). Development only.

    cd backend && python ../scripts/dev/demo_server.py [--port 8099] [--llm-url URL --llm-model NAME]

* Host collectors replay `docs/samples/collectors-live.json` (a snapshot of a real server,
  with a little jitter on CPU/network so the live charts move).
* The *arr collector and the action layer run against the in-memory fake *arr/qBittorrent
  world from the tests, with "approve" policies, and one pending approval is seeded.
  Nothing here ever talks to a real *arr, qBittorrent or the production instance.
* Reports are real: they go to the LLM given by --llm-url/--llm-model (none = unconfigured).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from ai_server_monitor.collectors.arr import ArrCollector  # noqa: E402
from ai_server_monitor.collectors.base import Collector  # noqa: E402
from ai_server_monitor.config import Bootstrap  # noqa: E402
from ai_server_monitor.main import create_app  # noqa: E402
from tests.arrfakes import HASH_A, HASH_B, FakeArrWorld, make_settings, torrent  # noqa: E402

FIXTURE = ROOT / "docs" / "samples" / "collectors-live.json"

IMAGE_UPDATES = {
    "summary": {"total": 36, "up_to_date": 31, "update_available": 2, "unknown": 3},
    "updates_available": ["ghcr.io/ggml-org/llama.cpp:server-cuda", "lscr.io/linuxserver/sonarr:latest"],
    "images": [
        {"image": "ghcr.io/ggml-org/llama.cpp:server-cuda", "containers": ["llamacpp-embedding"],
         "status": "update_available", "reason": None},
        {"image": "lscr.io/linuxserver/sonarr:latest", "containers": ["sonarr"],
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


def make_registry(world: FakeArrWorld) -> dict[str, type[Collector]]:
    live = json.loads(FIXTURE.read_text(encoding="utf-8"))
    registry = {
        name: replay(name, entry["result"]["data"])
        for name, entry in live.items()
        if entry.get("result") and entry["result"].get("ok")
    }
    registry["image_updates"] = replay("image_updates", IMAGE_UPDATES)

    class DemoArr(ArrCollector):
        def __init__(self, cfg, env, **kw):  # type: ignore[no-untyped-def]
            super().__init__(cfg, env, transport=world.transport())

    registry["arr"] = DemoArr
    return registry


async def seed(app, world: FakeArrWorld) -> None:  # type: ignore[no-untyped-def]
    ctx = app.state.ctx
    now = time.time()
    rows = [
        ("qbit_delete_torrent", "qbittorrent: Old.Show.S01E02", "executed", "scheduler", "stalled for 9h", "ui", 5 * 3600),
        ("qbit_delete_torrent", "qbittorrent: Broken.Release.2025", "failed", "scheduler", "stalled for 14h", "ui", 9 * 3600),
        ("qbit_delete_torrent", "qbittorrent: Dubious.Movie.2024", "rejected", "scheduler", "metadata never arrived", "ui", 26 * 3600),
    ]  # fmt: skip
    for action, target, status, source, reason, by, age in rows:
        result = {"error": "qBittorrent returned HTTP 500"} if status == "failed" else {"detail": "removed"}
        await ctx.db.execute(
            "INSERT INTO actions (ts, updated, action, target, args, policy, status, source, reason, "
            "result, decided_by) VALUES (?, ?, ?, ?, '{}', 'approve', ?, ?, ?, ?, ?)",
            (now - age, now - age, action, target, status, source, reason, json.dumps(result), by),
        )
    await ctx.actions.request(
        "qbit_delete_torrent",
        {"hash": HASH_A, "delete_files": True, "title": "Stuck.Movie.2026.2160p"},
        "scheduler",
        reason="No progress for 9h and no seeds available",
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--llm-url", default="")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--tool-mode", default="none", choices=["auto", "native", "json", "none"])
    parser.add_argument("--static", default=str(ROOT / "frontend" / "dist"))
    args = parser.parse_args()

    world = FakeArrWorld()
    world.torrents = {
        HASH_A: torrent(HASH_A, "Stuck.Movie.2026.2160p", "stalledDL"),
        HASH_B: torrent(HASH_B, "Other.Movie.2026.1080p", "downloading"),
    }
    settings = make_settings(
        {"stalled_cleanup": {"policy": "approve"}, "missing_search": {"policy": "suggest", "batch": 5}},
        dry_run=False,
    )
    settings.llm.base_url = args.llm_url
    settings.llm.model = args.llm_model
    settings.llm.tool_mode = args.tool_mode
    settings.report.interval = timedelta(hours=1)
    settings.collectors.arr.interval = timedelta(seconds=30)
    for name in ("system", "docker", "gpu", "systemd", "journal", "apt"):
        getattr(settings.collectors, name).interval = timedelta(seconds=15 if name == "system" else 60)

    tmp = Path(tempfile.mkdtemp(prefix="asm-demo-"))
    app = create_app(
        Bootstrap(data_dir=tmp / "data", host_root=tmp / "none", static_dir=Path(args.static)),
        collector_registry=make_registry(world),
        seed_settings=settings,
        arr_transport=world.transport(),
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.1)
    await seed(app, world)
    print(f"demo ready on http://127.0.0.1:{args.port}  (data in {tmp})", flush=True)
    await task


if __name__ == "__main__":
    asyncio.run(main())
