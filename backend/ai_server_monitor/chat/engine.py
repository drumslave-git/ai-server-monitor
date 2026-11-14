"""The chat loop: question → (tool calls → results)* → answer.

Two tool protocols, chosen by the LLM tool-mode probe (or the setting):
- native: OpenAI-style `tools` / `tool_calls`;
- json: the model replies `{"tool": ..., "args": {...}}` as text (see `llm/jsontools.py`),
  with validation and up to two repair prompts for malformed replies;
- none: no tools; the digest is put into the prompt instead.

The engine yields plain event dicts (session, tool_call, tool_result, token, done, error);
the HTTP layer turns them into SSE. Nothing chat-private is published on the event bus.
Writes only ever go through `ActionService.request` (see tools.py), so policy decides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ..llm import LLMError
from ..llm.client import Usage, new_tool_call_id
from ..llm.jsontools import json_tool_system_prompt, parse_tool_request
from ..llm.text import strip_reasoning
from .prompts import STEP_LIMIT_NOTE, system_prompt
from .safety import Scrubber, shrink, wrap_untrusted
from .store import ChatStore
from .tools import Tool, ToolContext, ToolError, build_tools

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger(__name__)

MAX_REPAIRS = 2
TOKEN_CHUNK = 24
PREVIEW = 400
MAX_MESSAGE = 4000
_TOOL_ATTEMPT = re.compile(r"\{\s*[\"']?(tool|name|function)[\"']?\s*:", re.IGNORECASE)

Event = dict[str, Any]


@dataclass
class _Turn:
    """State of one question."""

    session_id: str
    tools: dict[str, Tool]
    ctx: ToolContext
    scrub: Scrubber
    limit_chars: int
    timeout: float
    usage: Usage = field(default_factory=Usage)
    steps: int = 0
    repairs: int = 0
    model: str | None = None


class ChatEngine:
    def __init__(self, app: AppContext, *, promql_transport: Any = None):
        self.app = app
        self.store = ChatStore(app.db)
        self._promql_transport = promql_transport
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ entry point

    async def run(self, message: str, session_id: str | None = None) -> AsyncIterator[Event]:
        """Answer `message` in a new or existing session. Yields events until `done`/`error`."""
        message = message.strip()[:MAX_MESSAGE]
        if session_id is None:
            session_id = await self.store.create_session(message)
        elif not await self.store.session_exists(session_id):
            yield {"type": "error", "message": f"unknown session {session_id}"}
            return
        yield {"type": "session", "session_id": session_id}
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                async for event in self._answer(session_id, message):
                    yield event
            except LLMError as exc:
                log.warning("chat: LLM failed: %s", exc)
                yield {"type": "error", "message": f"The language model failed: {exc}"}

    # ------------------------------------------------------------------ one question

    async def _answer(self, session_id: str, message: str) -> AsyncIterator[Event]:
        app = self.app
        settings = app.settings
        if not settings.llm.configured:
            yield {"type": "error", "message": "The LLM is not configured (Settings → LLM)."}
            return
        mode = await app.tool_mode.resolve()
        tools = build_tools(settings) if mode != "none" else {}
        history = await self.store.history(session_id, settings.chat.history_messages)
        await self.store.add_message(session_id, "user", message)

        digest = (await app.reports.digest()).text if mode == "none" else None
        system = system_prompt(settings.report.language, digest=digest, tools=bool(tools))
        definitions = [t.definition() for t in tools.values()]
        if mode == "json" and tools:
            system = json_tool_system_prompt(definitions, system)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            *history,
            {"role": "user", "content": message},
        ]
        turn = _Turn(
            session_id=session_id,
            tools=tools,
            ctx=ToolContext(app, session_id, self._promql_transport),
            scrub=Scrubber(settings),
            limit_chars=settings.chat.max_tool_result_chars,
            timeout=settings.chat.tool_timeout.total_seconds(),
        )

        final: str | None = None
        stopped: str | None = None
        while final is None:
            if turn.steps >= settings.chat.max_steps and tools:
                stopped = "step_limit"
                messages.append({"role": "user", "content": STEP_LIMIT_NOTE})
                completion = await app.llm.complete(messages)
                self._count(turn, completion)
                final = strip_reasoning(completion.content) or (
                    "I could not finish: the tool-call limit was reached."
                )
                break
            turn.steps += 1
            if mode == "native" and tools:
                step = self._native_step(turn, messages, definitions)
            else:
                step = self._json_step(turn, messages, plain=not tools)
            async for event in step:
                if event["type"] == "_final":
                    final = event["text"]
                else:
                    yield event

        for piece in _chunks(final):
            yield {"type": "token", "text": piece}
            await asyncio.sleep(0)
        meta = {
            "tool_mode": mode,
            "steps": turn.steps,
            "model": turn.model,
            "usage": {
                "prompt_tokens": turn.usage.prompt_tokens,
                "completion_tokens": turn.usage.completion_tokens,
            },
            "stopped": stopped,
        }
        message_id = await self.store.add_message(session_id, "assistant", final, meta=meta)
        yield {"type": "done", "session_id": session_id, "message_id": message_id, **meta}

    # ------------------------------------------------------------------ protocols

    def _count(self, turn: _Turn, completion: Any) -> None:
        turn.usage.prompt_tokens += completion.usage.prompt_tokens
        turn.usage.completion_tokens += completion.usage.completion_tokens
        turn.model = completion.model or turn.model

    async def _native_step(
        self, turn: _Turn, messages: list[dict[str, Any]], definitions: list[dict[str, Any]]
    ) -> AsyncIterator[Event]:
        completion = await self.app.llm.complete(messages, tools=definitions, tool_choice="auto")
        self._count(turn, completion)
        if not completion.tool_calls:
            yield {"type": "_final", "text": strip_reasoning(completion.content) or "(no answer)"}
            return
        calls = [
            {
                "id": c.id or new_tool_call_id(),
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments or "{}"},
            }
            for c in completion.tool_calls
        ]
        messages.append(
            {"role": "assistant", "content": completion.content or None, "tool_calls": calls}
        )
        await self.store.add_message(
            turn.session_id,
            "assistant",
            completion.content or None,
            tool_calls=[
                {
                    "id": c["id"],
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"],
                }
                for c in calls
            ],
            meta={"step": turn.steps},
        )
        for call, raw in zip(calls, completion.tool_calls, strict=True):
            try:
                args = raw.parsed_arguments()
            except ValueError as exc:
                args, problem = {}, f"arguments are not a valid JSON object ({exc})"
            else:
                problem = None
            async for event in self._run_tool(
                turn, call["id"], raw.name, args, problem, messages, native=True
            ):
                yield event

    async def _json_step(
        self, turn: _Turn, messages: list[dict[str, Any]], *, plain: bool
    ) -> AsyncIterator[Event]:
        completion = await self.app.llm.complete(messages)
        self._count(turn, completion)
        text = completion.content
        if plain:
            yield {"type": "_final", "text": strip_reasoning(text) or "(no answer)"}
            return
        parsed = parse_tool_request(text, set(turn.tools))
        if parsed is None:
            if _TOOL_ATTEMPT.search(strip_reasoning(text)) and turn.repairs < MAX_REPAIRS:
                turn.repairs += 1
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That looked like a tool call but I could not use it (invalid JSON, "
                            f"unknown tool or wrong shape). Tools: {', '.join(turn.tools)}. "
                            'Reply again with ONLY {"tool": "<name>", "args": {...}}, or answer '
                            "in plain text if you need no tool."
                        ),
                    }
                )
                return
            yield {"type": "_final", "text": strip_reasoning(text) or "(no answer)"}
            return
        name, args = parsed
        call_id = new_tool_call_id()
        messages.append({"role": "assistant", "content": text})
        await self.store.add_message(
            turn.session_id,
            "assistant",
            None,
            tool_calls=[{"id": call_id, "name": name, "arguments": json.dumps(args)}],
            meta={"step": turn.steps, "protocol": "json"},
        )
        async for event in self._run_tool(turn, call_id, name, args, None, messages, native=False):
            yield event

    # ------------------------------------------------------------------ tools

    async def _run_tool(
        self,
        turn: _Turn,
        call_id: str,
        name: str,
        args: dict[str, Any],
        problem: str | None,
        messages: list[dict[str, Any]],
        *,
        native: bool,
    ) -> AsyncIterator[Event]:
        tool = turn.tools.get(name)
        yield {
            "type": "tool_call",
            "id": call_id,
            "name": name,
            "args": turn.scrub.clean(args),
            "kind": tool.kind if tool else None,
        }
        ok, text, truncated = await self._execute(turn, tool, name, args, problem)
        await self.store.add_message(
            turn.session_id,
            "tool",
            text,
            tool_call_id=call_id,
            name=name,
            meta={"ok": ok, "truncated": truncated, "step": turn.steps},
        )
        wrapped = wrap_untrusted(name, text)
        if native:
            messages.append({"role": "tool", "tool_call_id": call_id, "content": wrapped})
        else:
            messages.append({"role": "user", "content": f"Result of {name}:\n{wrapped}"})
        yield {
            "type": "tool_result",
            "id": call_id,
            "name": name,
            "ok": ok,
            "truncated": truncated,
            "size": len(text),
            "preview": text[:PREVIEW],
        }

    async def _execute(
        self, turn: _Turn, tool: Tool | None, name: str, args: dict[str, Any], problem: str | None
    ) -> tuple[bool, str, bool]:
        """Run one tool call → (ok, text for the model, trimmed?). Never raises: every
        failure becomes a message the model can react to."""
        if tool is None:
            return False, f"unknown tool {name!r}; available: {', '.join(turn.tools)}", False
        if problem:
            return False, f"{name}: {problem}", False
        try:
            parsed = tool.args_model.model_validate(args)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}"
                for e in exc.errors(include_url=False, include_input=False, include_context=False)
            )
            return False, f"invalid arguments for {name}: {details}", False
        try:
            async with asyncio.timeout(turn.timeout):
                result = await tool.handler(turn.ctx, parsed)
        except ToolError as exc:
            return False, turn.scrub.text(str(exc)), False
        except TimeoutError:
            return False, f"{name} timed out after {turn.timeout:.0f}s", False
        except Exception as exc:
            log.exception("chat tool %s crashed", name)
            return False, f"{name} failed: {type(exc).__name__}", False
        text, truncated = shrink(turn.scrub.clean(result), turn.limit_chars)
        return True, text, truncated


def _chunks(text: str) -> list[str]:
    """Split the answer into small pieces for the token stream (on whitespace)."""
    pieces: list[str] = []
    current = ""
    for word in re.findall(r"\S+\s*|\s+", text):
        current += word
        if len(current) >= TOKEN_CHUNK:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces or [""]
