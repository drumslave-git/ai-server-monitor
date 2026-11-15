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
from .title import TITLE_TIMEOUT_S, clean_title, title_messages
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
    streamed: str = ""
    """Text of the current model turn that was already sent to the client."""


class ChatEngine:
    def __init__(self, app: AppContext, *, promql_transport: Any = None):
        self.app = app
        self.store = ChatStore(app.db)
        self._promql_transport = promql_transport
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ entry point

    async def run(
        self,
        message: str,
        session_id: str | None = None,
        *,
        channel: str = "web",
        source: str | None = None,
    ) -> AsyncIterator[Event]:
        """Answer `message` in a new or existing session. Yields events until `done`/`error`.

        `source` names who asks in the audit log of write actions (default `chat:<session>`);
        `channel` is stored with a new session (web | telegram)."""
        message = message.strip()[:MAX_MESSAGE]
        is_new = session_id is None
        if session_id is None:
            session_id = await self.store.create_session(message, channel)
        elif not await self.store.session_exists(session_id):
            yield {"type": "error", "message": f"unknown session {session_id}"}
            return
        yield {"type": "session", "session_id": session_id}
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        title_task: asyncio.Task[str | None] | None = None
        async with lock:
            try:
                async for event in self._answer(session_id, message, source):
                    if event["type"] == "done" and is_new:
                        # Started before `done` goes out, so the model works while the client
                        # reads the answer; nothing below waits for it.
                        title_task = self._start_title(session_id, message, event["text"])
                    yield event
            except LLMError as exc:
                log.warning("chat: LLM failed: %s", exc)
                yield {"type": "error", "message": f"The language model failed: {exc}"}
        if title_task is not None:
            # Outside the session lock (the next question is not held up) and after `done`
            # (the answer is already out). The stream stays open for the title, at most
            # TITLE_TIMEOUT_S; if the client left, the task still finishes and stores it.
            title = await title_task
            if title:
                yield {"type": "title", "session_id": session_id, "title": title}

    def _mcp_notes(self, tools: dict[str, Tool]) -> list[str]:
        """Prompt lines about MCP: servers that are down and, with the generic list/call
        pair, which servers and tools exist."""
        from ..mcp_client.tools import overview_lines

        notes = self.app.mcp.unavailable()
        if "mcp_call" in tools:
            notes = [*overview_lines(self.app.mcp), *notes]
        return notes

    async def _prom_queries(self, tools: dict[str, Tool]) -> list[dict[str, Any]]:
        """The auto-planned Prometheus queries, so the model knows what already exists."""
        if "promql_query" not in tools:
            return []
        from ..promauto.store import list_enabled_queries

        try:
            return await list_enabled_queries(self.app.db)
        except Exception:
            log.exception("chat: could not list the auto Prometheus queries")
            return []

    # ------------------------------------------------------------------ titles

    def _start_title(self, session_id: str, question: str, answer: str) -> asyncio.Task[str | None]:
        task = asyncio.create_task(self._make_title(session_id, question, answer))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _make_title(self, session_id: str, question: str, answer: str) -> str | None:
        """Ask the LLM for a short title and store it. Any failure, timeout or unusable reply
        leaves the title cut from the first message in place (returns None)."""
        try:
            async with asyncio.timeout(TITLE_TIMEOUT_S):
                completion = await self.app.llm.complete(
                    title_messages(question, answer), temperature=0.2, max_tokens=40
                )
            title = clean_title(completion.content)
            if title is None:
                log.info("chat: unusable title reply %r", completion.content[:80])
                return None
            await self.store.set_title(session_id, title)
            return title
        except Exception as exc:  # a title is a nicety: never let it break a chat
            log.info("chat: no LLM title (%s: %s)", type(exc).__name__, exc)
            return None

    # ------------------------------------------------------------------ one question

    async def _answer(
        self, session_id: str, message: str, source: str | None
    ) -> AsyncIterator[Event]:
        app = self.app
        settings = app.settings
        if not settings.llm.configured:
            yield {"type": "error", "message": "The LLM is not configured (Settings → LLM)."}
            return
        mode = await app.tool_mode.resolve()
        if mode != "none":
            await app.mcp.prepare()  # lazy connect; never blocks long, dead servers just stay out
        tools = build_tools(settings, app.mcp) if mode != "none" else {}
        history = await self.store.history(session_id, settings.chat.history_messages)
        await self.store.add_message(session_id, "user", message)

        digest = (await app.reports.digest()).text if mode == "none" else None
        system = system_prompt(
            settings.report.language,
            digest=digest,
            tools=bool(tools),
            tool_names=tools.keys(),
            prom_queries=await self._prom_queries(tools),
            notes=self._mcp_notes(tools) if mode != "none" else (),
        )
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
            ctx=ToolContext(app, session_id, self._promql_transport, source),
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
                async for event in self._llm_turn(turn, messages):
                    if event["type"] == "_end":
                        final = strip_reasoning(event["text"]) or (
                            "I could not finish: the tool-call limit was reached."
                        )
                    else:
                        yield event
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

        # Whatever of the answer was not streamed already (text held back as possible tool
        # JSON, an empty-answer fallback) goes out now.
        rest = final[len(turn.streamed) :] if final.startswith(turn.streamed) else ""
        for piece in _chunks(rest) if rest else []:
            yield {"type": "token", "text": piece, "step": turn.steps}
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
        yield {
            "type": "done",
            "session_id": session_id,
            "message_id": message_id,
            **meta,
            "text": final,
        }

    # ------------------------------------------------------------------ protocols

    async def _llm_turn(
        self,
        turn: _Turn,
        messages: list[dict[str, Any]],
        *,
        defs: list[dict[str, Any]] | None = None,
        hold_json: bool = False,
    ) -> AsyncIterator[Event]:
        """One streamed model call. Yields `token` events as text arrives, then a final
        internal `_end` event with the full text and the assembled tool calls.

        With `hold_json` (JSON-in-text protocol) text from the first `{` or code fence on is
        not sent: it is probably a tool request, and the caller decides at the end whether it
        was (nothing of it leaks) or not (the rest is flushed as the answer).
        Reasoning blocks (<think>…</think>) are never sent."""
        turn.streamed = ""
        text = ""
        tool_calls: list[Any] = []
        async for chunk in self.app.llm.stream(
            messages, tools=defs, tool_choice="auto" if defs else None
        ):
            if chunk.done:
                if chunk.usage:
                    turn.usage.prompt_tokens += chunk.usage.prompt_tokens
                    turn.usage.completion_tokens += chunk.usage.completion_tokens
                tool_calls = chunk.tool_calls
                continue
            text += chunk.text
            shown = _visible(text)
            if hold_json:
                starts = [i for i in (shown.find("{"), shown.find("```")) if i != -1]
                # (an incomplete fence at the end, "`" or "``", is held back too)
                shown = (shown[: min(starts)] if starts else shown.rstrip("`")).rstrip()
            if len(shown) > len(turn.streamed) and shown.startswith(turn.streamed):
                piece = shown[len(turn.streamed) :]
                turn.streamed = shown
                yield {"type": "token", "text": piece, "step": turn.steps}
        turn.model = self.app.llm.model
        yield {"type": "_end", "text": text, "tool_calls": tool_calls}

    async def _native_step(
        self, turn: _Turn, messages: list[dict[str, Any]], definitions: list[dict[str, Any]]
    ) -> AsyncIterator[Event]:
        text, tool_calls = "", []
        async for event in self._llm_turn(turn, messages, defs=definitions):
            if event["type"] == "_end":
                text, tool_calls = event["text"], event["tool_calls"]
            else:
                yield event
        if not tool_calls:
            yield {"type": "_final", "text": strip_reasoning(text) or "(no answer)"}
            return
        calls = [
            {
                "id": c.id or new_tool_call_id(),
                "type": "function",
                "function": {"name": c.name, "arguments": c.arguments or "{}"},
            }
            for c in tool_calls
        ]
        messages.append({"role": "assistant", "content": text or None, "tool_calls": calls})
        await self.store.add_message(
            turn.session_id,
            "assistant",
            text or None,
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
        for call, raw in zip(calls, tool_calls, strict=True):
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
        text = ""
        async for event in self._llm_turn(turn, messages, hold_json=not plain):
            if event["type"] == "_end":
                text = event["text"]
            else:
                yield event
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


def _visible(text: str) -> str:
    """The part of streamed text that may be shown so far: reasoning blocks removed and a
    trailing, still incomplete tag (`<thi`) held back."""
    shown = strip_reasoning(text)
    cut = shown.rfind("<")
    if cut != -1 and ">" not in shown[cut:] and len(shown) - cut < 12:
        shown = shown[:cut]
    return shown


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
