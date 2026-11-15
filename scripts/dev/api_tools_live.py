"""Run the real, fully wired chat engine (all tools, the real system prompt) against a real
LLM over a fake home server (Radarr, Whisparr, Sonarr, Prowlarr, qBittorrent, Docker): no real
instance is touched.

    cd backend && python ../scripts/dev/api_tools_live.py BASE_URL MODEL "question" ...

Prints a Markdown transcript: every tool call with its arguments and result size, the answer,
the approvals the chat left pending, and the size of the tool schemas in model tokens.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from ai_server_monitor.chat.tools import Tool, build_tools
from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.context import AppContext
from tests.apifakes import FakeServices, api_settings


class Stub(Collector):
    name = "system"

    async def collect(self) -> dict[str, Any]:
        return {"hostname": "box"}


def build_world() -> FakeServices:
    world = FakeServices()
    world.missing_by_kind["radarr"] = [
        {"id": i, "title": f"Radarr Missing {i:02d}", "year": 1990 + i % 30, "monitored": True}
        for i in range(1, 55)
    ]
    world.missing_by_kind["whisparr"] = [
        {"id": i, "title": f"Whisparr Title {i:04d}", "year": 2000 + i % 25, "monitored": True}
        for i in range(1, 3764)
    ]
    world.sonarr_queue = [
        {
            "id": 1,
            "title": "Severance.S02E03.1080p.WEB",
            "status": "completed",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importBlocked",
            "statusMessages": [
                {"title": "Severance.S02E03", "messages": ["No files found to import"]}
            ],
        },
        {
            "id": 2,
            "title": "Andor.S01E05.2160p",
            "status": "warning",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "downloading",
            "statusMessages": [{"title": "Andor.S01E05", "messages": ["The download is stalled"]}],
        },
        {
            "id": 3,
            "title": "Slow.Horses.S04E01.1080p",
            "status": "downloading",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "downloading",
            "statusMessages": [],
        },
        {
            "id": 4,
            "title": "Foundation.S02E02.1080p",
            "status": "failed",
            "trackedDownloadStatus": "error",
            "trackedDownloadState": "failedPending",
            "statusMessages": [{"title": "Foundation.S02E02", "messages": ["Download failed"]}],
        },
    ]
    return world


async def measure_schema(app: AppContext, tools: list[Tool]) -> tuple[int, int]:
    """(prompt tokens of a trivial prompt with the tools, without them)."""
    messages = [{"role": "system", "content": "Reply with OK."}, {"role": "user", "content": "hi"}]
    without = await app.llm.complete(messages, max_tokens=5, temperature=0)
    defs = [t.definition() for t in tools]
    with_tools = await app.llm.complete(messages, tools=defs, max_tokens=5, temperature=0)
    return with_tools.usage.prompt_tokens, without.usage.prompt_tokens


async def main(base_url: str, model: str, questions: list[str]) -> None:
    world = build_world()
    settings: Settings = api_settings().model_copy(
        update={"llm": api_settings().llm.model_copy(update={"base_url": base_url, "model": model})}
    )
    app = AppContext(
        Bootstrap(data_dir=Path(tempfile.mkdtemp(prefix="asm-api-live-")), host_root=Path("/")),
        collector_registry={"system": Stub},
        arr_transport=world.transport(),
        seed_settings=settings,
    )
    await app.start()
    tools = list(build_tools(app.settings, app.mcp).values())
    print(
        "# Chat with the generic API tools, live run\n\n"
        f"Model `{model}` at `{base_url}`; the real engine with all tools and the real system "
        "prompt. The world is fake: Radarr 54 and Whisparr 3763 missing movies, a Sonarr queue "
        "with 3 problems, Prowlarr with one failing indexer.\n"
    )
    try:
        with_t, without_t = await measure_schema(app, tools)
        mine = [t for t in tools if t.name in ("api_call", "api_endpoints")]
        only, _ = await measure_schema(app, mine)
        print(
            f"Tool schemas: {len(tools)} tools = **{with_t - without_t} tokens**; "
            f"`api_call` + `api_endpoints` alone = {only - without_t} tokens.\n"
        )
        for question in questions:
            print(f"## Q: {question}\n")
            async for event in app.chat.run(question):
                kind = event["type"]
                if kind == "tool_call":
                    print(f"- **tool call** `{event['name']}` `{json.dumps(event['args'])}`")
                elif kind == "tool_result":
                    ok = "ok" if event["ok"] else "ERROR"
                    print(f"  - result ({ok}, {event['size']} chars): `{event['preview'][:260]!r}`")
                elif kind == "done":
                    print(
                        f"\n**Answer** (tool mode {event['tool_mode']}, {event['steps']} steps, "
                        f"{event['usage']['prompt_tokens']} prompt tokens):\n\n{event['text']}\n"
                    )
                elif kind == "error":
                    print(f"\n**ERROR**: {event['message']}\n")
            sys.stdout.flush()
        print("## Pending approvals left by the chat\n")
        for row in await app.actions.pending():
            print(f"- #{row['id']} `{row['action']}` {row['target']} (source {row['source']})")
        print("\n## What the fake services saw\n")
        for r in world.requests:
            print(f"- `{r['method']} {r['host']}{r['path']}` {r['params']}")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
