"""Shaping what goes back to the model: size caps (head + tail kept), secret scrubbing and
human-readable sizes."""

from __future__ import annotations

import re
from collections import deque

_KEYWORDS = r"(?:password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|credential)"
_ASSIGN = re.compile(
    rf"(?im)^(?P<head>[^\n]*?\b[\w.-]*{_KEYWORDS}[\w.-]*[\"']?\s*[=:]\s*[\"']?)(?P<value>[^\s\"'][^\n]*)$"
)
_URL_AUTH = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@", re.IGNORECASE)
_PEM = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)
REDACTED = "[redacted]"


def scrub_output(text: str) -> str:
    """Hide the values of `password = ...`-style lines, URL credentials and PEM private keys.
    (The chat engine additionally scrubs every configured secret and well-known token shapes.)"""
    text = _PEM.sub(f"[{REDACTED} private key]", text)
    text = _URL_AUTH.sub(rf"\1{REDACTED}@", text)

    def hide(match: re.Match[str]) -> str:
        value = match.group("value")
        return match.group(0) if value.startswith(REDACTED) else match.group("head") + REDACTED

    return _ASSIGN.sub(hide, text)


def human(n: float | int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover


class HeadTail:
    """Collects a byte stream keeping only the first `head` and the last `tail` bytes (cut
    on line boundaries when rendered) while counting what was dropped."""

    def __init__(self, head: int, tail: int):
        self.head_cap, self.tail_cap = head, tail
        self._head = bytearray()
        self._tail: deque[bytes] = deque()
        self._tail_size = 0
        self.total = 0
        self.cut_bytes = 0
        self.cut_lines = 0

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        room = self.head_cap - len(self._head)
        if room > 0:
            self._head += data[:room]
            data = data[room:]
        if not data:
            return
        self._tail.append(data)
        self._tail_size += len(data)
        while self._tail_size > self.tail_cap and self._tail:
            first = self._tail[0]
            excess = self._tail_size - self.tail_cap
            if len(first) <= excess:
                self._tail.popleft()
                dropped = first
            else:
                dropped = first[:excess]
                self._tail[0] = first[excess:]
            self._tail_size -= len(dropped)
            self.cut_bytes += len(dropped)
            self.cut_lines += dropped.count(b"\n")

    @property
    def truncated(self) -> bool:
        return self.cut_bytes > 0

    def render(self) -> str:
        head = bytes(self._head)
        tail = b"".join(self._tail)
        if not self.truncated:
            return (head + tail).decode("utf-8", "replace")
        # Cut both sides back to whole lines.
        nl = head.rfind(b"\n")
        if nl != -1:
            self.cut_bytes += len(head) - nl - 1
            head = head[: nl + 1]
        nl = tail.find(b"\n")
        if nl != -1:
            self.cut_bytes += nl + 1
            self.cut_lines += 1
            tail = tail[nl + 1 :]
        lines = max(self.cut_lines, 1)
        marker = f"\n… {lines} lines cut ({human(self.cut_bytes)}) …\n"
        return head.decode("utf-8", "replace") + marker + tail.decode("utf-8", "replace")
