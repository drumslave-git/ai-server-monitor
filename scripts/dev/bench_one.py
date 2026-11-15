"""Run chosen benchmark scenarios on a scratch app (own temp database) against a real model.

    cd backend && python ../scripts/dev/bench_one.py LIVE_URL SCENARIO_ID [REPEATS]

The model settings (endpoint, model, temperature, context ...) are read from the live
instance's GET /api/settings (nothing is written there); the benchmark itself only talks to
the scratch app's fake world and to that model's endpoint. Prints each attempt's score.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.context import AppContext


class Stub(Collector):
    name = "system"

    async def collect(self) -> dict[str, object]:
        return {"hostname": "box"}


async def main(live: str, scenario: str, repeats: int) -> None:
    stored = httpx.get(f"{live.rstrip('/')}/api/settings", timeout=20).json()
    llm = stored["values"]["llm"]
    llm = {k: v for k, v in llm.items() if k != "api_key"}
    settings = Settings.model_validate({"llm": llm, "bench": {"repeats": repeats}})
    app = AppContext(
        Bootstrap(data_dir=Path(tempfile.mkdtemp(prefix="asm-bench-one-")), host_root=Path("/")),
        collector_registry={"system": Stub},
        seed_settings=settings,
    )
    await app.start()
    try:
        run_id = await app.bench.start(group=None, ids=[scenario], repeats=repeats)
        print(f"run {run_id}: {scenario} x{repeats} on {settings.llm.model}", flush=True)
        while app.bench.running:
            await asyncio.sleep(2)
        run = await app.bench.store.get_run(run_id)
        attempts = await app.bench.store.full_attempts(run_id)
        print("run status:", run.get("status"), "score:", run.get("score"))
        print("attempt keys:", sorted(attempts[0]) if attempts else None)
        for a in attempts:
            print("--- attempt", a.get("attempt"), "score", a.get("score"), "status", a.get("status"))
            for c in a.get("checks") or []:
                print("   ", {k: c.get(k) for k in ("type", "weight", "passed", "ok", "reason", "got") if k in c})
            print("   tool calls:", a.get("tool_calls"))
            print("   answer:", " ".join(str(a.get("answer") or "").split())[:700])
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 3))
