"""Read-only data collectors."""

from .apt import AptCollector
from .base import Collector, CollectorResult, CollectorUnavailable, HostEnv
from .docker import DockerCollector
from .gpu import GPUCollector
from .journal import JournalCollector
from .manager import CollectorManager
from .system import SystemCollector
from .systemd import SystemdCollector

# name -> class; names match the keys under `collectors:` in the config.
REGISTRY: dict[str, type[Collector]] = {
    cls.name: cls
    for cls in (
        SystemCollector,
        DockerCollector,
        GPUCollector,
        SystemdCollector,
        JournalCollector,
        AptCollector,
    )
}

__all__ = [
    "REGISTRY",
    "Collector",
    "CollectorManager",
    "CollectorResult",
    "CollectorUnavailable",
    "HostEnv",
]
