"""Compression, size caps and secret scrubbing of what is stored."""

from __future__ import annotations

import json
import zlib
from typing import Any

TRUNCATED_MARK = "…[{n} chars cut]"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def pack(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 6)


def unpack(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return zlib.decompress(blob).decode("utf-8", "replace")


def cap_text(text: str, limit: int) -> tuple[str, bool]:
    """Cut `text` to at most `limit` bytes of UTF-8, keeping the head and saying what was cut."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    head = raw[: max(limit - 40, 0)].decode("utf-8", "ignore")
    return head + TRUNCATED_MARK.format(n=len(text) - len(head)), True


def _shorten(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        if len(value) <= cap:
            return value
        return value[:cap] + TRUNCATED_MARK.format(n=len(value) - cap)
    if isinstance(value, list):
        return [_shorten(v, cap) for v in value]
    if isinstance(value, dict):
        return {k: _shorten(v, cap) for k, v in value.items()}
    return value


def cap_json(value: Any, limit: int) -> tuple[str, bool]:
    """JSON text of `value` within `limit` bytes. Long strings are shortened first (largest
    cap that fits), so the result stays valid JSON with its structure intact; only a structure
    that is too big even then is cut as text."""
    text = dumps(value)
    if len(text.encode("utf-8")) <= limit:
        return text, False
    cap = limit
    while cap > 200:
        cap //= 2
        text = dumps(_shorten(value, cap))
        if len(text.encode("utf-8")) <= limit:
            return text, True
    cut, _ = cap_text(text, limit)
    return dumps({"_truncated_json": cut}), True


def scrub_value(value: Any, scrubber: Any | None) -> Any:
    return value if scrubber is None else scrubber.clean(value)


def scrub_str(text: str, scrubber: Any | None) -> str:
    return text if scrubber is None else scrubber.text(text)
