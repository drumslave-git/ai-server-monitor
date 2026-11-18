"""Where the host is, seen from inside the container."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
