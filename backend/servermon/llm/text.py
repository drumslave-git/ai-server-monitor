"""Helpers for coping with free-form model output (reasoning blocks, embedded JSON)."""

from __future__ import annotations

import json
import re
from typing import Any

_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK = re.compile(r"^\s*<(think|thinking|reasoning)>.*", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def strip_reasoning(text: str) -> str:
    """Remove <think>…</think>-style reasoning that some local models emit inline.

    An unclosed leading <think> (output cut off mid-reasoning) removes everything.
    Some templates omit the opening tag, so a lone leading '…</think>' is also stripped.
    """
    text = _THINK_BLOCK.sub("", text)
    text = _UNCLOSED_THINK.sub("", text)
    for closing in ("</think>", "</thinking>", "</reasoning>"):
        idx = text.find(closing)
        if idx != -1 and "<" + closing[2:] not in text[:idx]:
            text = text[idx + len(closing) :]
    return text.strip()


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Find the first JSON object in model output.

    Tries, in order: the whole text, ```json fenced blocks, then the first decodable
    '{…}' anywhere in the text.
    """
    text = strip_reasoning(text)
    candidates = [text, *(m.group(1).strip() for m in _FENCE.finditer(text))]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for servers that don't report usage."""
    return max(1, len(text) // 4) if text else 0
