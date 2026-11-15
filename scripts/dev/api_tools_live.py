"""Run the real chat engine against a real LLM with ONLY the two generic API tools
(`api_call`, `api_endpoints`) over a fake *arr world (no real instance is touched).

    cd backend && .venv/Scripts/python.exe ../scripts/dev/api_tools_live.py \
        BASE_URL MODEL "question 1" "question 2" ...

Prints a Markdown transcript: every tool call with its full arguments and result size, the
answer, and the size of the tool schemas in model tokens. The system prompt is the chat's own
(`chat/prompts.py`) without the host-tool bullet plus `API_PROMPT`, the paragraph proposed for
the shared prompt.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from ai_server_monitor.apiaccess import ApiGateway, api_tool_specs
from ai_server_monitor.chat import engine as engine_module
from ai_server_monitor.chat import prompts
from ai_server_monitor.chat.tools import Tool, ToolError
from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.context import AppContext
from tests.apifakes import FakeServices, api_settings

API_PROMPT = (
    "- For data of Radarr, Sonarr, Whisparr, Prowlarr, qBittorrent, Docker or Prometheus "
    "(library, missing, queue, indexers, torrents, containers) use `api_endpoints` to see "
    "ready examples, then `api_call` with a `select` so you only fetch what you need. "
    "Radarr and Whisparr both hold movies: for a question about movies check every "
    "configured one. "
    "Never say you cannot list something before you looked at `api_endpoints`.\n"
)


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
    app.api_gateway = ApiGateway(app.settings, transport=world.transport())  # type: ignore[attr-defined]
    specs = api_tool_specs(app.settings, error_cls=ToolError)
    tools = [Tool(**spec.as_kwargs()) for spec in specs]
    engine_module.build_tools = lambda s: {t.name: t for t in tools}  # type: ignore[assignment]
    base_prompt = prompts.SYSTEM_PROMPT
    start = base_prompt.index("- The server is the live host.")
    end = base_prompt.index("- Prefer one or two precise")
    prompts.SYSTEM_PROMPT = base_prompt[:start] + API_PROMPT + base_prompt[end:]
    print(
        f"# Generic API tools, live run\n\nModel `{model}` at `{base_url}`; only `api_call` and "
        "`api_endpoints` are offered. The world is fake (Radarr 54 / Whisparr 3763 missing, a "
        "Sonarr queue with 3 problems, Prowlarr with one failing indexer).\n"
    )
    try:
        with_t, without_t = await measure_schema(app, tools)
        chars = len(json.dumps([t.definition() for t in tools]))
        print(
            f"Tool schemas: {chars} chars, **{with_t - without_t} tokens** "
            f"(prompt of a trivial request: {without_t} without tools, {with_t} with).\n"
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
        print("## What the fake services saw\n")
        for r in world.requests:
            print(f"- `{r['method']} {r['host']}{r['path']}` {r['params']}")
    finally:
        await app.api_gateway.close()  # type: ignore[attr-defined]
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2], sys.argv[3:]))
