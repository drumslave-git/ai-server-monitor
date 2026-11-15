"""Readable numbers in what the chat model sees.

A small model that is handed `"rss": 1569484800` answers "1,569,484,800 bytes". This pass runs
over every tool result before it is shown to the model and turns byte counts into `"1.5 GiB"`,
long second counts into `"3d 4h"` and epoch timestamps into UTC dates. It goes by key name
(a number without a unit is never guessed at), and the collectors' own raw data is untouched:
sorting and filtering happen before this.
"""

from __future__ import annotations

import time
from typing import Any

# Always bytes.
_BYTES = {
    "rss", "vms", "avail", "available", "cached", "buffers", "shared", "free_bytes",
    "mem_usage", "mem_limit", "mem_total", "mem_used", "vram", "vram_used", "vram_total",
    "swap_total", "swap_used", "sizeondisk", "freespace", "totalspace", "bytes",
    "rx_bytes", "tx_bytes", "disk_read", "disk_write",
}  # fmt: skip
# Bytes when large enough to be bytes; small values of these are as likely to be counts.
_MAYBE_BYTES = {"size", "used", "total", "free", "downloaded", "uploaded", "amount_left"}
_TOTAL_COMPANIONS = {"used", "avail", "available", "free", "percent", "used_percent"}
_RATES = {"net_rx_bps", "net_tx_bps", "rx_bps", "tx_bps", "dlspeed", "upspeed"}
_SECONDS = {"uptime_s": "uptime", "uptime": "uptime", "age_s": "age", "duration_s": "duration",
            "eta": "eta", "seconds": "seconds"}  # fmt: skip
_EPOCH_KEYS = {"ts", "since", "boot_time", "first_ts", "last_ts", "window_start", "window_end"}
_MIN_BYTES = 1024
_EPOCH = (946_684_800, 4_102_444_800)  # 2000 .. 2100


def size(value: float) -> str:
    n = float(value)
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{sign}{n:.0f} B" if unit == "B" else f"{sign}{n:.1f} {unit}"
        n /= 1024
    return f"{sign}{n:.1f} TiB"  # pragma: no cover


def span(seconds: float) -> str:
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(epoch))


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def humanize(value: Any) -> Any:
    """A copy of `value` with byte counts, rates, durations and timestamps spelled out."""
    if isinstance(value, list):
        return [humanize(v) for v in value]
    if not isinstance(value, dict):
        return value
    keys = {str(k).lower() for k in value}
    out: dict[Any, Any] = {}
    for key, item in value.items():
        name = str(key).lower()
        if not _number(item):
            out[key] = humanize(item)
        elif name in _BYTES or name.endswith("_bytes"):
            out[key] = size(item)
        elif name in _MAYBE_BYTES and abs(item) >= _MIN_BYTES:
            if name != "total" or keys & _TOTAL_COMPANIONS:
                out[key] = size(item)
            else:
                out[key] = item
        elif name in _RATES and item >= 0:
            out[key] = f"{size(item)}/s"
        elif name in _SECONDS and item >= 60:
            out[_SECONDS[name] if name.endswith("_s") else key] = span(item)
        elif name in _EPOCH_KEYS and _EPOCH[0] <= item <= _EPOCH[1]:
            out[key] = stamp(item)
        else:
            out[key] = item
    return out
