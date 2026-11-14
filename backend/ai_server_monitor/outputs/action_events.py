"""Bridge from the action layer's notifiers to the event bus and the metrics.

`ActionService` calls `notifier(event, record)` for suggested/pending/expired actions and
for every finished one. This publishes them on the bus (so they reach GET /api/stream) and
counts them in `asm_actions_total`:

    action.suggested   an action was only suggested (policy "suggest")
    approval.pending   an action waits for a human decision
    approval.decided   a human (or the timeout) decided: approved | rejected | expired
    approval.expired   a pending approval timed out (also announced as approval.decided)
    action.result      an action finished: executed | failed | dry_run | rejected

Payloads are compact (no arguments or results: fetch those from /api/actions).
"""

from __future__ import annotations

from typing import Any

from ..events import EventBus
from .metrics import MetricsHub

_KEEP = ("id", "ts", "action", "target", "policy", "status", "source", "reason", "decided_by")
_APPROVED = ("approved", "executed", "failed", "dry_run")


def compact_action(record: dict[str, Any]) -> dict[str, Any]:
    out = {k: record.get(k) for k in _KEEP}
    out["expires_at"] = record.get("expires_at")
    return out


class ActionEventBridge:
    def __init__(self, bus: EventBus, metrics: MetricsHub):
        self.bus = bus
        self.metrics = metrics

    def __call__(self, event: str, record: dict[str, Any]) -> None:
        data = compact_action(record)
        status = str(record.get("status") or "")
        self.metrics.inc("asm_actions_total", {"action": str(data["action"]), "result": status})
        decided_by = str(record.get("decided_by") or "")
        human_decision = decided_by and not decided_by.startswith("policy")
        if human_decision and event in ("action.result", "approval.expired"):
            decision = "approved" if status in _APPROVED else status  # rejected | expired
            self.bus.publish("approval.decided", {**data, "decision": decision})
        self.bus.publish(event, data)
