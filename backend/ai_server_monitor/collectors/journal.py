"""systemd journal: warning-and-worse entries since the last run, grouped and flagged.

Runs `journalctl -D <host journal dir>` (read-only) and continues from the last cursor.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..config import JournalCollectorSettings
from .base import Collector, CollectorUnavailable, HostEnv, run_command

PRIORITY_NAMES = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]

# Kernel/system events worth calling out regardless of volume.
NOTABLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "oom": re.compile(r"Out of memory|oom-kill|oom_reaper|invoked oom-killer", re.I),
    "disk_io": re.compile(
        r"I/O error|blk_update_request|Buffer I/O error|critical medium error|"
        r"ata\d+.*(failed command|exception)",
        re.I,
    ),
    "filesystem": re.compile(
        r"EXT4-fs error|XFS \(.*\): .*(error|corrupt)|BTRFS (error|critical)|"
        r"Remounting filesystem read-only|ZFS.*(fault|degraded)",
        re.I,
    ),
    "segfault": re.compile(r"segfault at|general protection fault", re.I),
    "hardware": re.compile(r"Hardware Error|Machine check|\bmce:|EDAC .*error", re.I),
    "thermal": re.compile(r"temperature above threshold|thermal.*(critical|throttl)", re.I),
    "gpu": re.compile(r"NVRM: Xid|amdgpu.*(timeout|fault)", re.I),
}
_NORMALIZE = [
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b", re.I), "<hex>"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\d+"), "#"),
    (re.compile(r"\s+"), " "),
]


def _message(value: Any) -> str:
    if isinstance(value, list):  # binary-safe form: list of byte values
        try:
            return bytes(value).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return str(value)
    return "" if value is None else str(value)


def normalize_message(message: str) -> str:
    for pattern, repl in _NORMALIZE:
        message = pattern.sub(repl, message)
    return message.strip()[:200]


def entry_source(entry: dict[str, Any]) -> str:
    if entry.get("_TRANSPORT") == "kernel":
        return "kernel"
    return str(
        entry.get("CONTAINER_NAME")
        or entry.get("_SYSTEMD_UNIT")
        or entry.get("SYSLOG_IDENTIFIER")
        or entry.get("_COMM")
        or "unknown"
    )


def summarize_journal(entries: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    by_priority: dict[str, int] = {}
    by_source: dict[str, int] = {}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    notable: dict[str, dict[str, Any]] = {}
    for entry in entries:
        try:
            prio = int(entry.get("PRIORITY", 6))
        except (TypeError, ValueError):
            prio = 6
        prio_name = PRIORITY_NAMES[prio] if 0 <= prio < len(PRIORITY_NAMES) else str(prio)
        message = _message(entry.get("MESSAGE"))
        source = entry_source(entry)
        try:
            ts = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1e6
        except (TypeError, ValueError):
            ts = 0.0
        by_priority[prio_name] = by_priority.get(prio_name, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        key = (source, normalize_message(message))
        group = groups.get(key)
        if group is None:
            groups[key] = {"source": source, "priority": prio_name, "prio": prio, "count": 1,
                           "sample": message[:300], "first_ts": ts, "last_ts": ts}  # fmt: skip
        else:
            group["count"] += 1
            group["last_ts"] = max(group["last_ts"], ts)
            if prio < group["prio"]:
                group["prio"], group["priority"] = prio, prio_name
        for name, pattern in NOTABLE_PATTERNS.items():
            if pattern.search(message):
                hit = notable.setdefault(name, {"count": 0, "samples": []})
                hit["count"] += 1
                if len(hit["samples"]) < 3:
                    hit["samples"].append(f"{source}: {message[:300]}")
    top_messages = sorted(groups.values(), key=lambda g: (g["prio"], -g["count"]))[:15]
    for group in top_messages:
        group.pop("prio")
    return {
        "entries": len(entries),
        "truncated": len(entries) >= limit,
        "by_priority": by_priority,
        "top_sources": [
            {"source": s, "count": c}
            for s, c in sorted(by_source.items(), key=lambda kv: -kv[1])[:10]
        ],
        "top_messages": top_messages,
        "notable": notable,
    }


class JournalCollector(Collector):
    name = "journal"
    timeout = timedelta(seconds=60)
    cfg: JournalCollectorSettings

    def __init__(self, cfg: JournalCollectorSettings, env: HostEnv):
        super().__init__(cfg, env)
        self._cursor: str | None = None
        self._last_run: float | None = None

    def _directory(self) -> Path | None:
        if self.cfg.directory:
            return Path(self.cfg.directory)
        for candidate in ("/var/log/journal", "/run/log/journal"):
            path = self.env.path(candidate)
            if path.is_dir() and any(path.iterdir()):
                return path
        return None

    async def check(self) -> None:
        if shutil.which("journalctl") is None:
            raise CollectorUnavailable("journalctl not available in this environment")
        if self.env.in_container and self._directory() is None:
            raise CollectorUnavailable("host journal directory not found under host root")

    def _args(self, since: float) -> list[str]:
        args = ["journalctl", "--no-pager", "--quiet", "--output=json",
                f"--priority={self.cfg.priority}", f"--lines={self.cfg.max_entries}"]  # fmt: skip
        directory = self._directory()
        if directory is not None:
            args.append(f"--directory={directory}")
        if self._cursor:
            args.append(f"--after-cursor={self._cursor}")
        else:
            args.append(f"--since=@{int(since)}")
        return args

    async def collect(self) -> dict[str, Any]:
        if self.env.in_container and self._directory() is None:
            # Never fall back to the container's own (empty) journal.
            raise CollectorUnavailable("host journal directory not found under host root")
        now = time.time()
        since = self._last_run or now - self.cfg.initial_lookback.total_seconds()
        output = await run_command(self._args(since), timeout=50)
        entries: list[dict[str, Any]] = []
        for line in output.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        if entries and entries[-1].get("__CURSOR"):
            self._cursor = entries[-1]["__CURSOR"]
        self._last_run = now
        return {
            "window_start": since,
            "window_end": now,
            "min_priority": self.cfg.priority,
            **summarize_journal(entries, self.cfg.max_entries),
        }
