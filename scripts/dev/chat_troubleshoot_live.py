"""Replay the owner's real conversation about qBittorrent against a real LLM, over a fake home
server where qBittorrent answers the login with 401 Unauthorized (Host-header validation) and the
fake qBittorrent container log shows the matching lines. No real instance is touched.

    cd backend && python ../scripts/dev/chat_troubleshoot_live.py BASE_URL MODEL

One conversation (one chat session), the questions in order. Prints a Markdown transcript.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.context import AppContext
from tests.apifakes import FakeServices, api_settings

QUESTIONS = [
    "why are you not able to connect to qbit?",
    "how to fix?",
    "why do you think creds are wrong?",
    "yeah, but Im using same creds to login via browser - it works",
]
# `python chat_troubleshoot_live.py BASE MODEL "question" ...` asks these instead.

LOG_LINES = [
    "2026-11-15T09:58:01Z (N) Peer ID: -qB5010-",
    "2026-11-15T10:00:12Z (W) WebUI: Invalid Host header, port mismatch. Request target: "
    "qbit.test:8080, allowed: localhost:8080, qbittorrent:8080",
    "2026-11-15T10:02:12Z (W) WebUI: Invalid Host header, port mismatch. Request target: "
    "qbit.test:8080, allowed: localhost:8080, qbittorrent:8080",
    "2026-11-15T10:04:12Z (W) WebUI: Invalid Host header, port mismatch. Request target: "
    "qbit.test:8080, allowed: localhost:8080, qbittorrent:8080",
]


class World(FakeServices):
    """qBittorrent rejects every request from the monitor with 401 (host validation)."""

    def _qbit(self, request: httpx.Request, body: str) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized", headers={"content-type": "text/plain"})

    def _docker(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/containers/json":
            return httpx.Response(
                200,
                json=[
                    {
                        "Id": "q" * 64,
                        "Names": ["/qbittorrent"],
                        "Image": "lscr.io/linuxserver/qbittorrent:latest",
                        "State": "running",
                        "Status": "Up 3 days (healthy)",
                        "Mounts": [
                            {
                                "Type": "bind",
                                "Source": "/srv/qbit/config",
                                "Destination": "/config",
                                "RW": True,
                            }
                        ],
                    },
                    {"Id": "r" * 64, "Names": ["/radarr"], "State": "running", "Image": "radarr"},
                ],
            )
        if path in ("/containers/qbittorrent/json", f"/containers/{'q' * 64}/json"):
            return httpx.Response(
                200,
                json={
                    "Name": "/qbittorrent",
                    "Id": "q" * 64,
                    "State": {
                        "Status": "running",
                        "Running": True,
                        "Health": {"Status": "healthy"},
                    },
                    "Config": {
                        "Image": "lscr.io/linuxserver/qbittorrent:latest",
                        "Env": ["WEBUI_PORT=8080"],
                    },
                    "HostConfig": {},
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": "/srv/qbit/config",
                            "Destination": "/config",
                            "RW": True,
                        }
                    ],
                    "NetworkSettings": {},
                },
            )
        if path == "/containers/qbittorrent/logs":
            from tests.apifakes import docker_log_frames

            return httpx.Response(
                200,
                content=docker_log_frames(LOG_LINES),
                headers={"content-type": "application/vnd.docker.multiplexed-stream"},
            )
        return super()._docker(request)


class Stub(Collector):
    name = "system"

    async def collect(self) -> dict[str, Any]:
        return {"hostname": "box"}


async def fake_run_command(ctx: Any, args: Any) -> Any:
    """`run_command` as on the owner's server, without a Linux sandbox: the real parser and
    its refusals, canned output for what the owner's chat ran (systemctl, ss)."""
    from ai_server_monitor.chat.tools import ToolError
    from ai_server_monitor.hostinspect.commands import Refused, parse_pipeline

    try:
        parse_pipeline(args.cmd)
    except Refused as exc:
        raise ToolError(f"refused: {exc}") from exc
    word = args.cmd.split()[0]
    if word == "systemctl":
        return {"exit_code": 4, "output": "", "stderr": "Unit qbittorrent.service could not be found."}
    if word == "ss":
        return {
            "exit_code": 0,
            "output": "LISTEN 0 4096 0.0.0.0:8080 0.0.0.0:* users:((\"docker-proxy\",pid=812,fd=4))",
        }
    return {"exit_code": 1, "output": "", "stderr": f"{word}: nothing to show in this replay"}


async def main(base_url: str, model: str, questions: list[str] | None = None) -> None:
    world = World()
    base = api_settings()
    settings: Settings = base.model_copy(
        update={"llm": base.llm.model_copy(update={"base_url": base_url, "model": model})}
    )
    app = AppContext(
        Bootstrap(data_dir=Path(tempfile.mkdtemp(prefix="asm-ts-live-")), host_root=Path("/")),
        collector_registry={"system": Stub},
        arr_transport=world.transport(),
        seed_settings=settings,
    )
    await app.start()
    import functools

    from ai_server_monitor.chat import host_tools
    from ai_server_monitor.chat.tools import build_tools

    host_tools._run_command = fake_run_command  # (the tool list is built per question)
    app.chat._tools_builder = functools.partial(build_tools, assume_linux=True)
    # The connection test builds its own HTTP client: route it to the fake world as well, and
    # give the chat the Docker API (the fake home server's) it would get from the collector.
    from types import SimpleNamespace

    from ai_server_monitor import conntest

    real_client = conntest._client
    conntest._client = lambda **kw: real_client(transport=world.transport(), **kw)

    class DockerApi:
        async def get(self, path: str, **params: Any) -> Any:
            async with httpx.AsyncClient(transport=world.transport()) as client:
                response = await client.get("http://docker.test" + path, params=params)
                response.raise_for_status()
                if "json" in response.headers.get("content-type", ""):
                    return response.json()
                return response.content

    app.collectors.collectors["docker"] = SimpleNamespace(
        api=DockerApi(), name="docker", close=lambda: asyncio.sleep(0)
    )
    print(
        "# Chat troubleshooting qBittorrent, live run\n\n"
        f"Model `{model}` at `{base_url}`; the real engine, all tools, the real system prompt, "
        "one chat session. The world is fake: qBittorrent answers every login with "
        "`401 Unauthorized` (Host-header validation), its container log shows the matching "
        "lines. The questions are the owner's own, verbatim.\n"
    )
    try:
        session: str | None = None
        for question in questions or QUESTIONS:
            print(f"## Q: {question}\n")
            async for event in app.chat.run(question, session):
                kind = event["type"]
                if kind == "session":
                    session = event["session_id"]
                elif kind == "tool_call":
                    print(f"- **tool call** `{event['name']}` `{json.dumps(event['args'])}`")
                elif kind == "tool_result":
                    ok = "ok" if event["ok"] else "ERROR"
                    print(f"  - result ({ok}, {event['size']} chars): `{event['preview'][:300]!r}`")
                elif kind == "done":
                    print(
                        f"\n**Answer** ({event['steps']} steps, "
                        f"{event['usage']['prompt_tokens']} prompt tokens):\n\n{event['text']}\n"
                    )
                elif kind == "error":
                    print(f"\n**ERROR**: {event['message']}\n")
            sys.stdout.flush()
        print("## What the fake services saw\n")
        for r in world.requests:
            print(f"- `{r['method']} {r['host']}{r['path']}` {r['params']}")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:] or None))
