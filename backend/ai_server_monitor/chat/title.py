"""Chat session titles written by the LLM: the prompt and the cleanup of its reply.

A title is generated once, after the first answer of a conversation, in the background; until
(or unless) it arrives the session keeps the word-boundary cut of the first message
(`store.make_title`)."""

from __future__ import annotations

import re

from ..llm.text import strip_reasoning

TITLE_MAX_CHARS = 60
TITLE_MAX_WORDS = 10
TITLE_TIMEOUT_S = 10.0
"""Longest wait for the model; after that the fallback title stays."""

TITLE_SYSTEM = (
    "You name chat conversations. Write a short title of 3 to 6 words for the conversation "
    "below, in the same language as the user's question. Reply with the title only: no quotes, "
    "no punctuation at the end, no emoji, no prefix like 'Title:'."
)

_FENCE = re.compile(r"```[a-zA-Z0-9_-]*")
_PREFIX = re.compile(r"^(?:title|name|chat title|заголовок|название)\s*[:：-]\s*", re.IGNORECASE)
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f2ff\U0000fe0f\U0000200d]"
)
_QUOTES = " \t\"'`«»“”‘’„*#_"
_TRAILING = " .,;:!?-–—…"


def clean_title(raw: str | None) -> str | None:
    """The model's reply as a title, or None if it is unusable (empty, a whole paragraph)."""
    if not raw:
        return None
    text = strip_reasoning(raw)
    text = _FENCE.sub("", text).replace("```", "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    text = lines[0]
    text = _PREFIX.sub("", text.strip(_QUOTES))
    text = _EMOJI.sub("", text)
    text = " ".join(text.strip(_QUOTES).split()).rstrip(_TRAILING).strip(_QUOTES)
    if len(text) < 2 or len(text.split()) > TITLE_MAX_WORDS:
        return None
    if len(text) > TITLE_MAX_CHARS:
        cut = text[:TITLE_MAX_CHARS]
        if text[TITLE_MAX_CHARS] != " " and " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        text = cut.rstrip(_TRAILING)
    return text or None


def title_messages(question: str, answer: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": TITLE_SYSTEM},
        {
            "role": "user",
            "content": (
                f"User question:\n{question[:500]}\n\nAssistant answer (beginning):\n"
                f"{answer[:400]}\n\nTitle:"
            ),
        },
    ]
