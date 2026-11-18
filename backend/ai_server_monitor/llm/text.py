"""Coping with free-form model output: reasoning blocks some local models emit inline."""

from __future__ import annotations

import re

_THINK_BLOCK = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_UNCLOSED_THINK = re.compile(r"^\s*<(think|thinking|reasoning)>.*", re.DOTALL | re.IGNORECASE)


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
