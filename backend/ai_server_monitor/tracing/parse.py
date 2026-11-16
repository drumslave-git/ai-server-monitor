"""Reads what the model answered, for the trace only. Never raises; the client has its own
parsing and its own error handling, this just watches the same bytes.

Handles both shapes: a plain JSON completion and an SSE stream of `chat.completion.chunk`s.
Reasoning arrives as `reasoning_content` / `reasoning` fields or inline as `<think>…</think>`;
it is kept apart from the answer.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

_THINK = re.compile(r"<think>(.*?)</think>", re.DOTALL)
_REASONING_KEYS = ("reasoning_content", "reasoning")


@dataclass
class Parsed:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    model: str | None = None
    error: str | None = None


STREAM_DROPPED = "The model's reply stopped before any answer: the server closed the stream early."


def empty_answer_message(limit: Any) -> str:
    """The plain-language failure for a reply that was cut off by the output limit before the
    model wrote any answer (a thinking model that spent everything on reasoning)."""
    where = f" ({limit} tokens)" if limit else ""
    return (
        f"The model used the whole output limit{where} on thinking and wrote no answer. "
        "Raise the output limit or lower the thinking level in Settings → LLM."
    )


def split_think(content: str, reasoning: str) -> tuple[str, str]:
    """Move inline `<think>` blocks out of the answer. An unclosed block (cut off by the token
    limit) is all reasoning."""
    if "<think>" not in content and "</think>" not in content:
        return content, reasoning
    found = [m.group(1).strip() for m in _THINK.finditer(content)]
    rest = _THINK.sub("", content)
    if "<think>" in rest:
        before, _, tail = rest.partition("<think>")
        found.append(tail.strip())
        rest = before
    elif "</think>" in rest:  # the opening tag was part of the prompt template
        head, _, rest = rest.partition("</think>")
        found.insert(0, head.strip())
    parts = [p for p in [reasoning.strip(), *found] if p]
    return rest.strip(), "\n\n".join(parts)


def _usage(raw: Any) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or 0),
    }


def _args_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False) if value is not None else ""


def _message_tool_calls(raw: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return calls
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = fn.get("name") or item.get("name")
        if name:
            args = fn.get("arguments", item.get("arguments", ""))
            calls.append(
                {"id": item.get("id") or f"call_{i}", "name": name, "arguments": _args_text(args)}
            )
    return calls


def _first_text(message: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def parse_json_response(raw: bytes | str) -> Parsed:
    out = Parsed()
    try:
        data = json.loads(raw)
    except ValueError:
        out.error = "response is not JSON"
        return out
    if not isinstance(data, dict):
        out.error = "response is not a JSON object"
        return out
    out.model = data.get("model") if isinstance(data.get("model"), str) else None
    out.usage = _usage(data.get("usage"))
    if data.get("error") and not data.get("choices"):
        out.error = _args_text(data["error"])
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = message.get("content")
        out.content = content if isinstance(content, str) else _args_text(content)
        out.reasoning = _first_text(message, _REASONING_KEYS)
        out.tool_calls = _message_tool_calls(message.get("tool_calls"))
        out.finish_reason = choice.get("finish_reason")
    out.content, out.reasoning = split_think(out.content, out.reasoning)
    return out


class _CallAssembler:
    """OpenAI `delta.tool_calls` fragments → calls (same heuristics as the client's)."""

    def __init__(self) -> None:
        self.calls: dict[int, dict[str, Any]] = {}
        self.last = -1

    def feed(self, fragments: Any) -> None:
        if not isinstance(fragments, list):
            return
        for item in fragments:
            if not isinstance(item, dict):
                continue
            fn = item.get("function") if isinstance(item.get("function"), dict) else {}
            index = item.get("index")
            if not isinstance(index, int):
                last = self.calls.get(self.last)
                new = last is None or bool(
                    (item.get("id") and last["id"] and item["id"] != last["id"])
                    or (fn.get("name") and last["name"])
                )
                index = self.last + 1 if new else self.last
            call = self.calls.setdefault(index, {"id": None, "name": "", "arguments": ""})
            self.last = index
            if item.get("id"):
                call["id"] = item["id"]
            if fn.get("name"):
                call["name"] = fn["name"]
            args = fn.get("arguments", item.get("arguments"))
            if isinstance(args, str):
                call["arguments"] += args
            elif args is not None:
                call["arguments"] = json.dumps(args, ensure_ascii=False)

    def result(self) -> list[dict[str, Any]]:
        return [
            {"id": c["id"] or f"call_{i}", "name": c["name"], "arguments": c["arguments"]}
            for i, c in sorted(self.calls.items())
            if c["name"]
        ]


class StreamParser:
    """Feed it the response bytes as they arrive; `first_token_at` is the `time.perf_counter()`
    reading when the first piece of content, reasoning or a tool call showed up."""

    def __init__(self) -> None:
        self._buf = b""
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._calls = _CallAssembler()
        self.finish_reason: str | None = None
        self.usage: dict[str, int] | None = None
        self.model: str | None = None
        self.error: str | None = None
        self.done = False
        self.first_token_at: float | None = None
        self.events = 0

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while True:
            newline = self._buf.find(b"\n")
            if newline < 0:
                break
            line, self._buf = self._buf[:newline], self._buf[newline + 1 :]
            self._line(line.decode("utf-8", "replace").strip())

    def _line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            self.done = True
            return
        try:
            data = json.loads(data_str)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        self.events += 1
        if isinstance(data.get("model"), str):
            self.model = data["model"]
        if data.get("error"):
            self.error = _args_text(data["error"])
        self.usage = _usage(data.get("usage")) or self.usage
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            seen = False
            content = delta.get("content")
            if isinstance(content, str) and content:
                self._content.append(content)
                seen = True
            reasoning = _first_text(delta, _REASONING_KEYS)
            if reasoning:
                self._reasoning.append(reasoning)
                seen = True
            if delta.get("tool_calls"):
                self._calls.feed(delta["tool_calls"])
                seen = True
            message = choice.get("message")
            if isinstance(message, dict) and message.get("tool_calls"):
                for call in _message_tool_calls(message["tool_calls"]):
                    self._calls.calls[len(self._calls.calls)] = call
                seen = True
            if seen and self.first_token_at is None:
                self.first_token_at = time.perf_counter()
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

    def result(self) -> Parsed:
        content, reasoning = split_think("".join(self._content), "".join(self._reasoning))
        return Parsed(
            content=content,
            reasoning=reasoning,
            tool_calls=self._calls.result(),
            finish_reason=self.finish_reason,
            usage=self.usage,
            model=self.model,
            error=self.error,
        )
