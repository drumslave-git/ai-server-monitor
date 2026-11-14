"""Turns collector results into small live events for dashboards.

`metrics.tick` carries only the handful of numbers a live chart needs (a few hundred
bytes), not the collector data. `collector.status` fires when a collector's health
changes (ok <-> failing, error text changes), not on every run.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from ..collectors.base import CollectorResult
from ..events import EventBus

TICK_SOURCES = ("system", "gpu")
RECENT_TICKS = 120


def _round(value: Any, digits: int = 1) -> float | None:
    return round(float(value), digits) if isinstance(value, int | float) else None


def tick_payload(result: CollectorResult) -> dict[str, Any] | None:
    """The compact tick for a successful system/gpu run, else None."""
    if not result.ok or result.name not in TICK_SOURCES:
        return None
    data = result.data
    if result.name == "system":
        cpu = data.get("cpu") or {}
        mem = data.get("memory") or {}
        nics = [n for n in data.get("network") or [] if isinstance(n, dict)]
        load = cpu.get("load") or []
        return {
            "source": "system",
            "ts": result.ts,
            "cpu": _round(cpu.get("percent")),
            "mem": _round(mem.get("percent")),
            "swap": _round(mem.get("swap_percent")),
            "load": _round(load[0], 2) if load else None,
            "net_rx_bps": round(sum(n.get("rx_bps") or 0 for n in nics)),
            "net_tx_bps": round(sum(n.get("tx_bps") or 0 for n in nics)),
        }
    gpus = [
        {
            "index": g.get("index", i),
            "util": _round(g.get("util_percent")),
            "vram": _round(g.get("mem_percent")),
            "temp": _round(g.get("temperature_c"), 0),
            "power_w": _round(g.get("power_w")),
        }
        for i, g in enumerate(data.get("gpus") or [])
        if isinstance(g, dict)
    ]
    return {"source": "gpu", "ts": result.ts, "gpus": gpus}


class LiveFeed:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._last: dict[str, tuple[bool, str | None]] = {}
        self.recent: dict[str, deque[dict[str, Any]]] = {
            source: deque(maxlen=RECENT_TICKS) for source in TICK_SOURCES
        }
        """The latest ticks per source, so a freshly opened page can draw its charts at once."""

    def on_result(self, result: CollectorResult) -> None:
        tick = tick_payload(result)
        if tick is not None:
            self.recent[tick["source"]].append(tick)
            self.bus.publish("metrics.tick", tick)
        state = (result.ok, result.error)
        if self._last.get(result.name) != state:
            self._last[result.name] = state
            self.bus.publish(
                "collector.status",
                {
                    "name": result.name,
                    "ok": result.ok,
                    "error": result.error,
                    "unavailable": result.unavailable,
                    "ts": result.ts,
                },
            )
