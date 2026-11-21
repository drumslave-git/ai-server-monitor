"""Bridge from the action layer's notifiers to the event bus.

`ActionService` calls `notifier(event, record)` and this publishes the event on the bus (so
it reaches GET /api/stream). Item events carry one audit row:

    action.suggested   an action was only suggested (policy "suggest")
    action.result      an item finished: executed | failed | dry_run | rejected | deferred | expired

Batch events carry an approval batch (the items waiting for one decision):

    approval.pending   a batch was opened
    approval.updated   items were added to an open batch
    approval.decided   all or some of its items were approved / rejected (`decision`, `decided_by`)
    approval.expired   its pending items timed out
    approval.progress  approved items that waited for the hourly cap were executed

Payloads are compact (no arguments or results: fetch those from /api/actions and
/api/approvals).
"""

from __future__ import annotations

from typing import Any

from ..actions import BATCH_EVENTS
from ..events import EventBus

_KEEP = (
    "id",
    "batch_id",
    "ts",
    "action",
    "target",
    "policy",
    "status",
    "source",
    "reason",
    "decided_by",
)
_BATCH_KEEP = (
    "id",
    "ts",
    "updated",
    "action",
    "label",
    "title",
    "source",
    "expires_at",
    "total",
    "pending",
    "counts",
    "outcome",
    "queued",
    "next_in_s",
    "queue_note",
    "added",
    "decision",
    "decided_by",
)


def compact_action(record: dict[str, Any]) -> dict[str, Any]:
    out = {k: record.get(k) for k in _KEEP}
    out["expires_at"] = record.get("expires_at")
    return out


def compact_batch(batch: dict[str, Any]) -> dict[str, Any]:
    return {k: batch.get(k) for k in _BATCH_KEEP if k in batch}


class ActionEventBridge:
    def __init__(self, bus: EventBus):
        self.bus = bus

    def __call__(self, event: str, record: dict[str, Any]) -> None:
        if event in BATCH_EVENTS:
            self.bus.publish(event, compact_batch(record))
            return
        self.bus.publish(event, compact_action(record))
