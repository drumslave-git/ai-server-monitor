"""The real app with only the Prometheus collector, a live Prometheus and a live LLM, to look at
Settings → Collectors → Automatic queries. Development only; Prometheus is only read.

    cd backend && python ../scripts/dev/prom_auto_demo.py --prom https://prometheus.example \
        --llm https://ollama.example/v1 --model gemma4:e4b [--port 8098] [--replan]

State lives in a throw-away data directory. --replan starts a plan right away.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from ai_server_monitor.collectors.prometheus import PrometheusCollector  # noqa: E402
from ai_server_monitor.config import Bootstrap, Settings  # noqa: E402
from ai_server_monitor.main import create_app  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prom", required=True)
    ap.add_argument("--llm", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--static", default=str(ROOT / "frontend" / "dist"))
    ap.add_argument("--replan", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="asm-promauto-"))
    seed = Settings(
        llm={"base_url": args.llm, "model": args.model, "timeout": "5m", "tool_mode": "none"},
        collectors={"prometheus": {"url": args.prom, "queries": []}},
    )
    app = create_app(
        Bootstrap(data_dir=tmp / "data", host_root=tmp / "none", static_dir=Path(args.static)),
        collector_registry={"prometheus": PrometheusCollector},
        seed_settings=seed,
    )

    async def serve() -> None:
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")
        )
        task = asyncio.create_task(server.serve())
        if args.replan:
            import httpx

            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{args.port}") as client:
                for _ in range(100):
                    try:
                        if (await client.get("/healthz")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        await asyncio.sleep(0.2)
                print((await client.post("/api/prometheus/replan")).status_code, flush=True)
        print(f"http://127.0.0.1:{args.port}/#/settings/collectors", flush=True)
        await task

    asyncio.run(serve())


if __name__ == "__main__":
    main()
