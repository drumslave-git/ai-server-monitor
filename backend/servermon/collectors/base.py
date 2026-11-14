"""Collector interface and shared helpers."""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar

from ..config import CollectorConfig


class CollectorUnavailable(Exception):
    """The data source is not present here (missing mount/socket/device/binary)."""


class CommandError(Exception):
    def __init__(self, args: list[str], returncode: int | None, stderr: str):
        super().__init__(f"{args[0]} exited with {returncode}: {stderr.strip()[:300]}")
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class HostEnv:
    """Where to find host resources from inside the container.

    With `pid: host`, /proc and /sys already describe the host. The host filesystem is
    expected read-only at `root` (default /host); when that does not exist the monitor
    is assumed to run directly on the host and `root` is '/'.
    """

    root: Path = Path("/")
    proc: Path = Path("/proc")
    sys: Path = Path("/sys")

    @classmethod
    def detect(cls, host_root: Path) -> HostEnv:
        root = host_root if host_root.is_dir() else Path("/")
        return cls(root=root)

    @property
    def in_container(self) -> bool:
        return self.root != Path("/")

    def path(self, host_path: str) -> Path:
        """Translate an absolute host path to where it is visible to us."""
        return self.root / host_path.lstrip("/")


@dataclass
class CollectorResult:
    name: str
    ts: float
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    unavailable: bool = False
    duration_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Collector(ABC):
    name: ClassVar[str]
    timeout: ClassVar[timedelta] = timedelta(seconds=30)

    def __init__(self, cfg: CollectorConfig, env: HostEnv):
        self.cfg = cfg
        self.env = env

    async def check(self) -> None:
        """Raise CollectorUnavailable if this collector cannot work on this host."""

    @abstractmethod
    async def collect(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot."""

    def persist_view(self, data: dict[str, Any]) -> dict[str, Any]:
        """The part of `data` worth keeping in history (default: everything)."""
        return data

    async def close(self) -> None:
        """Release resources (connections, library handles)."""


# --------------------------------------------------------------------------- helpers


async def run_command(
    args: list[str],
    *,
    timeout: float = 60.0,
    env: Mapping[str, str] | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> str:
    """Run a command without a shell; return stdout. Raises CommandError on failure."""
    full_env = {**os.environ, "LC_ALL": "C", "LANG": "C", **(env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=full_env,
        )
    except FileNotFoundError as exc:
        raise CollectorUnavailable(f"{args[0]} not found") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise CommandError(args, None, f"timed out after {timeout:.0f}s") from None
    if proc.returncode not in ok_codes:
        raise CommandError(args, proc.returncode, stderr.decode("utf-8", "replace"))
    return stdout.decode("utf-8", "replace")


class RateTracker:
    """Turns monotonically increasing counters into per-second rates."""

    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, float]] = {}

    def rate(self, key: str, value: float, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        prev = self._prev.get(key)
        self._prev[key] = (now, value)
        if prev is None:
            return None
        dt = now - prev[0]
        if dt <= 0 or value < prev[1]:  # counter reset (reboot, container restart)
            return None
        return (value - prev[1]) / dt

    def forget_except(self, keep: set[str]) -> None:
        for key in list(self._prev):
            if key not in keep:
                del self._prev[key]


def round_or_none(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)
