"""The whole app for a final look, development only. Runs inside the container that
`final_demo.sh` prepares (scratch host in /fakehost, real host commands, everything else faked):

* host collectors replay `docs/samples/collectors-live.json`; Docker has a fake API
  (`chat_tools_live.FakeDockerApi`) so the chat can name who owns a path;
* Radarr (54 missing movies), Whisparr (3,763 missing), Sonarr, Prowlarr, qBittorrent are the
  in-memory fakes of `tests/apifakes.py` (what `api_tools_live.py` uses);
* a "notes" MCP server (one read tool, one write tool) on :8931;
* the LLM is the real one given on the command line.

    python final_demo.py BASE_URL MODEL [PORT]

Nothing here talks to a real service except the LLM endpoint.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import uvicorn

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "backend")]

from api_tools_live import build_world  # noqa: E402
from chat_tools_live import FakeDockerApi  # noqa: E402
from mcp_demo import notes_server  # noqa: E402

from ai_server_monitor.collectors.base import Collector  # noqa: E402
from ai_server_monitor.config import Bootstrap, Settings  # noqa: E402
from ai_server_monitor.main import create_app  # noqa: E402
from tests.apifakes import api_settings  # noqa: E402

FIXTURE = ROOT / "docs" / "samples" / "collectors-live.json"
MCP_PORT = 8931


def replay(name: str, data: dict[str, Any], **attrs: Any) -> type[Collector]:
    async def collect(self: Any) -> dict[str, Any]:
        return json.loads(json.dumps(data))

    return type(f"Replay_{name}", (Collector,), {"name": name, "collect": collect, **attrs})


def registry() -> dict[str, type[Collector]]:
    live = json.loads(FIXTURE.read_text(encoding="utf-8"))
    reg: dict[str, type[Collector]] = {}
    for name, entry in live.items():
        if entry.get("result") and entry["result"].get("ok"):
            extra = {"api": FakeDockerApi()} if name == "docker" else {}
            reg[name] = replay(name, entry["result"]["data"], **extra)
    return reg


def settings(base_url: str, model: str) -> Settings:
    base = api_settings()
    values = base.model_dump(mode="json", context={"reveal_secrets": True})
    values["llm"].update(base_url=base_url, model=model, tool_mode="native")
    values["collectors"]["docker"]["url"] = ""
    values["collectors"]["prometheus"]["enabled"] = False
    values["mcp"] = {
        "servers": [
            {
                "name": "notes",
                "url": f"http://127.0.0.1:{MCP_PORT}/mcp",
                "transport": "auto",
                "timeout": "10s",
                "tool_filter": ["list_notes", "add_note"],
            }
        ]
    }
    return Settings.model_validate(values)


async def main(base_url: str, model: str, port: int) -> None:
    world = build_world()
    notes = [
        {"id": 1, "text": "Server backup runs every night at 03:00 to the NAS"},
        {"id": 2, "text": "Renew the tcloud.monster domain in March"},
    ]
    mcp_app = notes_server(MCP_PORT, notes).streamable_http_app()
    tmp = Path(tempfile.mkdtemp(prefix="asm-final-"))
    app = create_app(
        Bootstrap(
            data_dir=tmp / "data",
            host_root=Path("/fakehost"),
            static_dir=Path("/frontend-dist"),
        ),
        collector_registry=registry(),
        arr_transport=world.transport(),
        seed_settings=settings(base_url, model),
    )
    servers = [
        uvicorn.Server(uvicorn.Config(mcp_app, host="127.0.0.1", port=MCP_PORT, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")),  # noqa: S104
    ]
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers):
        await asyncio.sleep(0.1)
    print(f"final demo on http://127.0.0.1:{port}", flush=True)
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(
        main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 8096)
    )
