"""The global "pause automation" switch.

While it is on, the monitor does nothing nobody asked for: every scheduler job (collectors,
reports, the Prometheus planner, the LLM probe, approval sweeps, pruning) is held back and the
runs in flight are cancelled. What a person starts keeps working: chat, the Settings test
buttons, approving or rejecting, the Telegram bot, and the "run a report now" buttons. The
point is a quiet host and a quiet LLM.

The state is stored in the database (kv), so a restart while paused stays paused.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

KV_KEY = "automation.pause"


@dataclass
class AutomationState:
    paused: bool = False
    since: float | None = None
    by: str | None = None
    """Who paused it: "ui", "api" or "telegram"."""

    def to_dict(self) -> dict[str, Any]:
        return {"paused": self.paused, "since": self.since, "by": self.by}

    @classmethod
    def from_stored(cls, raw: Any) -> AutomationState:
        if not isinstance(raw, dict) or not raw.get("paused"):
            return cls()
        since = raw.get("since")
        by = raw.get("by")
        return cls(
            paused=True,
            since=float(since) if isinstance(since, (int, float)) else time.time(),
            by=by if isinstance(by, str) else None,
        )
