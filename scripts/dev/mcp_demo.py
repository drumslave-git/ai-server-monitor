"""MCP end to end, development only: a local demo "notes" MCP server (one read tool, one write
tool, one more write tool that is not chosen) connected to the real app, optionally with
questions asked through the real chat engine and a real LLM.

    cd backend && .venv/Scripts/python.exe ../scripts/dev/mcp_demo.py \
        [--port 8098] [--mcp-port 8931] [--llm-url URL --model NAME] \
        [--ask "question" ...] [--generic-pair-for LAST_N] [--serve]

- `--ask` questions run in separate conversations; the last `--generic-pair-for` of them run
  with `mcp.max_inline_tool_tokens = 0` (the chat then only gets mcp_list_tools / mcp_call).
- Prints a Markdown transcript: tool calls with arguments, results, the answer, what is
  pending in the approvals, and the notes server's state. A pending write is approved at the
  very end (as the UI button would) so the effect on the server is visible.
- `--serve` keeps the app (UI on --port) and the notes server up afterwards, for screenshots.

Nothing here talks to a real service except the LLM endpoint you give.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from ai_server_monitor.chat import audit  # noqa: E402
from ai_server_monitor.chat.tools import build_tools  # noqa: E402
from ai_server_monitor.config import Bootstrap, Settings  # noqa: E402
from ai_server_monitor.main import create_app  # noqa: E402
from ai_server_monitor.mcp_client.tools import inline_tokens  # noqa: E402


def notes_server(port: int, notes: list[dict[str, Any]]) -> FastMCP:
    mcp = FastMCP("notes", host="127.0.0.1", port=port)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True),
        description="List the saved notes, optionally only those containing a word.",
    )
    def list_notes(contains: str = "") -> list[dict[str, Any]]:
        return [n for n in notes if contains.lower() in n["text"].lower()]

    @mcp.tool(description="Save a new note.")
    def add_note(text: str) -> str:
        notes.append({"id": len(notes) + 1, "text": text})
        return f"saved note {len(notes)}"

    @mcp.tool(description="Delete a note by id.")
    def delete_note(id: int) -> str:
        notes[:] = [n for n in notes if n["id"] != id]
        return f"deleted note {id}"

    return mcp


def settings_for(args: argparse.Namespace, generic: bool) -> Settings:
    return Settings.model_validate(
        {
            "llm": {
                "base_url": args.llm_url,
                "model": args.model,
            }
            if args.llm_url
            else {},
            "mcp": {
                "max_inline_tool_tokens": 0 if generic else 1200,
                "servers": [
                    {
                        "name": "notes",
                        "url": f"http://127.0.0.1:{args.mcp_port}/mcp",
                        "transport": "auto",
                        "timeout": "10s",
                        "tool_filter": ["list_notes", "add_note"],
                    }
                ],
            },
        }
    )


def show(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


async def ask(ctx: Any, question: str) -> None:
    print(f"\n### Q: {question}\n")
    await ctx.mcp.prepare()
    names = set(build_tools(ctx.settings, ctx.mcp))
    mcp_names = sorted(n for n in names if "__" in n or n.startswith("mcp_"))
    print(
        f"MCP tools offered: {', '.join(mcp_names)} (≈{inline_tokens(ctx.mcp)} tokens as single tools)\n"
    )
    async for event in ctx.chat.run(question):
        kind = event["type"]
        if kind == "tool_call":
            print(f"- **tool call** `{event['name']}` {show(event['args'])} ({event['kind']})")
        elif kind == "tool_result":
            flag = "ok" if event["ok"] else "ERROR"
            print(f"  - result [{flag}, {event['size']} chars]: `{event['preview'][:300]}`")
        elif kind == "done":
            print(
                f"\n**Answer** ({event['steps']} steps):\n\n> {event['text']}"
            )
        elif kind == "error":
            print(f"\n**Error**: {event['message']}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8098)
    parser.add_argument("--mcp-port", type=int, default=8931)
    parser.add_argument("--llm-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--ask", action="append", default=[])
    parser.add_argument("--generic-pair-for", type=int, default=0)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--static", default=str(ROOT / "frontend" / "dist"))
    args = parser.parse_args()

    notes: list[dict[str, Any]] = [
        {"id": 1, "text": "Server backup runs every night at 03:00 to the NAS"},
        {"id": 2, "text": "Renew the tcloud.monster domain in March"},
        {"id": 3, "text": "Replace the failing disk in the backup box"},
    ]
    mcp_app = notes_server(args.mcp_port, notes).streamable_http_app()
    tmp = Path(tempfile.mkdtemp(prefix="asm-mcp-demo-"))
    app = create_app(
        Bootstrap(data_dir=tmp / "data", host_root=tmp / "none", static_dir=Path(args.static)),
        seed_settings=settings_for(args, generic=False),
    )
    servers = [
        uvicorn.Server(uvicorn.Config(mcp_app, host="127.0.0.1", port=args.mcp_port, log_level="warning")),
        uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning")),
    ]  # fmt: skip
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers):
        await asyncio.sleep(0.1)
    ctx = app.state.ctx
    print(
        f"# MCP live run\n\nUI: http://127.0.0.1:{args.port}/#/settings/mcp  (notes MCP server on {args.mcp_port})"
    )
    if args.llm_url:
        print(f"LLM: {args.llm_url} / {args.model}")

    questions = args.ask
    first_generic = len(questions) - args.generic_pair_for
    for i, question in enumerate(questions):
        if i == first_generic and args.generic_pair_for:
            print(
                "\n---\n*(from here: `max_inline_tool_tokens = 0`, the chat gets only the generic pair)*"
            )
            await ctx.apply_settings(settings_for(args, generic=True))
        await ask(ctx, question)

    pending = await ctx.actions.pending()
    print("\n### Approvals pending after the conversations\n")
    for record in pending:
        print(
            f"- #{record['id']} `{record['action']}` → {record['target']} · policy {record['policy']} · source {record['source']} · args {show(record['args'])}"
        )
    if pending:
        print(f"\nNotes on the server before approving: {len(notes)}")
        done = await ctx.actions.decide(pending[0]["id"], "approve", "demo-ui")
        print(
            f"\nApproved #{pending[0]['id']} → status **{done['status']}**, result {show(done['result'])}"
        )
        print(f"Notes on the server after: {len(notes)} → {show(notes[-1])}")
    print(
        f"\nAudit log of MCP calls (kind=mcp, newest first): {show([(r['tool'], r['ok']) for r in await audit.recent(ctx.db, kind='mcp')])}",
        flush=True,
    )

    if args.serve:
        print("\nserving; Ctrl+C to stop", flush=True)
        await asyncio.gather(*tasks)
    for s in servers:
        s.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
