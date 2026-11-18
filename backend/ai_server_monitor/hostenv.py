"""Where the host is, seen from inside the container."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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

    def timezone(self) -> ZoneInfo:
        """The host's time zone: /etc/timezone, else the /etc/localtime link or file; UTC when
        neither can be read."""
        with contextlib.suppress(OSError, ValueError, ZoneInfoNotFoundError):
            name = self.path("/etc/timezone").read_text(errors="replace").strip()
            if name:
                return ZoneInfo(name)
        localtime = self.path("/etc/localtime")
        with contextlib.suppress(OSError, ValueError, ZoneInfoNotFoundError):
            target = os.readlink(localtime)
            if "zoneinfo/" in target:
                return ZoneInfo(target.split("zoneinfo/", 1)[1])
        with contextlib.suppress(OSError, ValueError), localtime.open("rb") as fh:
            return ZoneInfo.from_file(fh, key="localtime")
        return ZoneInfo("UTC")


def resolve_timezone(name: str, env: HostEnv) -> ZoneInfo:
    """`general.timezone`: a zone name, or empty for the host's own."""
    return ZoneInfo(name) if name.strip() else env.timezone()
