"""Shared pieces of the automations.

An automation decides *what to propose*; it never talks to the *arr/qBit write APIs
itself. Everything it wants done goes through `ActionService.request`, so policy,
approvals, caps, dry-run and the audit log apply to it exactly as to any other source.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...actions import ActionService
from ...collectors.base import CollectorResult
from ...config import Settings

log = logging.getLogger(__name__)

SOURCE = "scheduler"
DEDUPE_WINDOW = 24 * 3600.0
"""An item that already has a request this recent (any outcome but 'expired' or 'deferred',
which did not happen) is not requested again. Pending approvals always count, however old."""


@dataclass
class AutomationEnv:
    service: ActionService
    settings: Settings
    collector_results: Callable[[], dict[str, CollectorResult]] = field(default=dict)
    clock: Callable[[], float] = time.time


async def recent_args(
    env: AutomationEnv, action: str, window: float = DEDUPE_WINDOW
) -> list[dict[str, Any]]:
    """Arguments of earlier requests for `action` that should not be repeated."""
    rows = await env.service.db.fetchall(
        "SELECT args FROM actions WHERE action = ? AND status NOT IN ('expired', 'deferred') "
        "AND (status = 'pending' OR ts >= ?)",
        (action, env.clock() - window),
    )
    return [json.loads(r["args"]) for r in rows if r["args"]]


def short(text: str, limit: int = 200) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tally(records: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for record in records:
        out[record["status"]] = out.get(record["status"], 0) + 1
    return out


def run_key(name: str, env: AutomationEnv) -> str:
    """Identifies one run of an automation: its approvals form one batch."""
    return f"{name}:{int(env.clock())}"
