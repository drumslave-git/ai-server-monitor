"""Run the real chat engine against a real LLM endpoint on a scratch "host" (see
chat_tools_live.sh, which builds /fakehost and runs this inside a Linux container as root).

Docker is faked (no real instances are touched); the host commands run for
real in the sandbox, chrooted into /fakehost. Prints a Markdown transcript to stdout.

    python chat_tools_live.py BASE_URL MODEL "question 1" "question 2" ...
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.context import AppContext

CONTAINERS: list[dict[str, Any]] = [
    {
        "Id": "1" * 64,
        "Names": ["/nextcloud"],
        "Image": "nextcloud:latest",
        "State": "running",
        "Status": "Up 3 days",
        "Ports": [{"PublicPort": 8081, "PrivatePort": 80, "Type": "tcp"}],
        "Labels": {"com.docker.compose.service": "nextcloud"},
        "Mounts": [
            {
                "Type": "bind",
                "Source": "/mnt/disks/disk14TB/downloads",
                "Destination": "/data",
                "RW": True,
            }
        ],
    },
    {
        "Id": "4" * 64,
        "Names": ["/jellyfin"],
        "Image": "jellyfin/jellyfin:latest",
        "State": "running",
        "Status": "Up 3 days",
        "Ports": [{"PublicPort": 8096, "PrivatePort": 8096, "Type": "tcp"}],
        "Labels": {"com.docker.compose.service": "jellyfin"},
        "Mounts": [
            {
                "Type": "volume",
                "Name": "jellyfin_config",
                "Source": "/var/lib/docker/volumes/jellyfin_config/_data",
                "Destination": "/config",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/mnt/disks/disk14TB/media",
                "Destination": "/media",
                "RW": False,
            },
        ],
    },
]


class FakeDockerApi:
    async def get(self, path: str, **params: Any) -> Any:
        if path == "/containers/json":
            return CONTAINERS
        if path == "/info":
            return {"DockerRootDir": "/var/lib/docker", "ServerVersion": "27.0"}
        for c in CONTAINERS:
            if path in (f"/containers/{c['Names'][0].lstrip('/')}/json", f"/containers/{c['Id']}/json"):
                return {
                    "Id": c["Id"],
                    "Name": c["Names"][0],
                    "State": {"Status": "running", "Running": True, "Health": {"Status": "healthy"}},
                    "Config": {"Image": c["Image"], "Env": ["TZ=Europe/Berlin", "PASSWORD=hunter2"]},
                    "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}},
                    "RestartCount": 0,
                    "Mounts": c["Mounts"],
                    "NetworkSettings": {"Ports": {}, "Networks": {"media_default": {}}},
                }
        request = httpx.Request("GET", "http://docker" + path)
        raise httpx.HTTPStatusError("not found", request=request, response=httpx.Response(404))


class FakeDockerCollector(Collector):
    name = "docker"

    def __init__(self, cfg: Any, env: Any):
        super().__init__(cfg, env)
        self.api = FakeDockerApi()

    async def collect(self) -> dict[str, Any]:
        return {"containers": []}


async def main(base_url: str, model: str, questions: list[str]) -> None:
    data = Path(tempfile.mkdtemp(prefix="asm-live-"))
    bootstrap = Bootstrap(data_dir=data, host_root=Path("/fakehost"))
    settings = Settings(
        llm={"base_url": base_url, "model": model, "tool_mode": "auto"},
    )
    app = AppContext(
        bootstrap,
        collector_registry={"docker": FakeDockerCollector},
        seed_settings=settings,
    )
    await app.start()
    print(f"# Chat with host tools, live run\n\nModel `{model}` at `{base_url}`; scratch host "
          "root `/fakehost` (Docker faked, host commands real).\n")
    try:
        for question in questions:
            print(f"## Q: {question}\n")
            async for event in app.chat.run(question):
                kind = event["type"]
                if kind == "tool_call":
                    print(f"- **tool call** `{event['name']}` `{json.dumps(event['args'])}`")
                elif kind == "tool_result":
                    ok = "ok" if event["ok"] else "ERROR"
                    print(f"  - result ({ok}, {event['size']} chars): `{event['preview'][:300]!r}`")
                elif kind == "done":
                    print(f"\n**Answer** (tool mode {event['tool_mode']}, {event['steps']} steps, "
                          f"{event['usage']['prompt_tokens']} prompt tokens):\n\n{event['text']}\n")
                elif kind == "error":
                    print(f"\n**ERROR**: {event['message']}\n")
            sys.stdout.flush()
        audit = await app.db.kv_get("chat_shell_audit", [])
        print("## Audit log (kv `chat_shell_audit`)\n")
        for row in audit:
            print(f"- `{json.dumps(row)}`")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
