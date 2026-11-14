"""A fake streaming LLM for report tests (shared by the tests and the sample generator)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .conftest import sse

CANNED_REPORT = """\
```json
{"health": "warn", "score": 74, "summary": "Two disks are over 85% full and two systemd units \
have failed; everything else looks healthy.", "findings": [
  {"severity": "warn", "title": "Disk / filling up", "detail": "88% used, 28.4GB free."},
  {"severity": "warn", "title": "Failed units", "detail": "logrotate.service and \
snap.remmina.ssh-agent.service failed with exit-code."},
  {"severity": "info", "title": "130 zombie processes", "detail": "Parents are not reaping them."}
]}
```

## Overview
The server is up and mostly healthy, but needs attention soon: the root disk is 88% full
and two services have failed.

## Findings
- **Disk / filling up** — 88% used, 28.4GB free.
- **Failed units** — `logrotate.service` and `snap.remmina.ssh-agent.service` (exit-code).
- 130 zombie processes (info).

## Resources
CPU 8.9% busy, load 0.49 on 12 threads. Memory 8.8% used. GPU idle (RTX 3090, 25°C).

## Changes
No previous report.

## Recommendations
Free space on `/`, check `journalctl -u logrotate.service`.
"""


def stream_body(
    text: str, *, pieces: int = 7, prompt: int = 900, completion: int = 250, finish: str = "stop"
) -> bytes:
    size = max(1, len(text) // pieces)
    events: list[dict[str, Any] | str] = [
        {"choices": [{"delta": {"content": text[i : i + size]}}]} for i in range(0, len(text), size)
    ]
    events.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    events.append(
        {"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion}}
    )
    events.append("[DONE]")
    return sse(*events)


def stream_response(text: str = CANNED_REPORT, **kw: Any) -> httpx.Response:
    return httpx.Response(
        200, content=stream_body(text, **kw), headers={"content-type": "text/event-stream"}
    )


def sent_messages(route: Any, index: int = -1) -> list[dict[str, Any]]:
    return json.loads(route.calls[index].request.content)["messages"]
