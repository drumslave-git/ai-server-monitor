"""Shared bits for the tracing tests."""

from __future__ import annotations

import time
from typing import Any

from ai_server_monitor.chat.safety import Scrubber
from ai_server_monitor.config import Settings
from ai_server_monitor.db import Database
from ai_server_monitor.tracing import TraceConfig, Tracing
from ai_server_monitor.tracing.schema import SQL
from ai_server_monitor.tracing.store import CallRecord

API_KEY = "sk-live-0123456789abcdefABCDEF"


async def make_tracing(db: Database, **cfg: Any) -> Tracing:
    """A Tracing on a test database (the tracing tables are created from schema.SQL until the
    real migration exists)."""
    await db.conn.executescript(SQL)
    settings = Settings(llm={"base_url": "http://llm.test/v1", "model": "m", "api_key": API_KEY})
    return Tracing(db, TraceConfig(**cfg), scrubber=lambda: Scrubber(settings))


def call(**kw: Any) -> CallRecord:
    base: dict[str, Any] = {
        "ts": time.time(),
        "purpose": "report",
        "status": "ok",
        "model": "m",
        "messages": [
            {"role": "system", "content": "You are a monitor."},
            {"role": "user", "content": "Disk is 88% full"},
        ],
        "content": "Free some space.",
        "prompt_tokens": 20,
        "completion_tokens": 5,
        "total_ms": 1500,
        "ttft_ms": 500,
    }
    base.update(kw)
    return CallRecord(**base)
