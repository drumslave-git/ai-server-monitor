"""Tracing settings. Constants for now; `# TODO setting` marks what becomes a Settings field."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TraceConfig:
    enabled: bool = True  # TODO setting: logs.llm_trace
    store_bodies: bool = True  # TODO setting: logs.store_bodies (False = metadata only)
    retention_days: float = 7  # TODO setting: logs.retention_days
    max_total_mb: int = 200  # TODO setting: logs.max_total_mb
    field_cap_bytes: int = 512 * 1024  # per stored field (messages, tools, content...)
    ring_size: int = 5000  # in-memory app log records
    store_level: str = "WARNING"  # app log records at or above this go to SQLite
    app_log_max_rows: int = 50_000
