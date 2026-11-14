"""Compact status for a dashboard header: latest report + key numbers, one call."""

from __future__ import annotations

from typing import Any

from .digest.models import num
from .digest.snapshot import Snapshot


def resource_summary(snap: Snapshot) -> dict[str, Any]:
    """Key resource numbers from the latest collector results (None where unknown)."""
    system = snap.data("system")
    cpu = system.get("cpu") or {}
    mem = system.get("memory") or {}
    filesystems = system.get("filesystems") or []
    fullest = max(filesystems, key=lambda f: num(f.get("percent")), default=None)
    docker = snap.data("docker").get("summary") or {}
    systemd = snap.data("systemd")
    apt = snap.data("apt")
    gpus = snap.data("gpu").get("gpus") or []
    load = cpu.get("load") or []
    images = snap.data("image_updates").get("images") or []
    return {
        "cpu_percent": cpu.get("percent"),
        "load_1m": load[0] if load else None,
        "cores": cpu.get("cores_logical"),
        "memory_percent": mem.get("percent"),
        "swap_percent": mem.get("swap_percent"),
        "uptime_s": system.get("uptime_s"),
        "fullest_disk": (
            {"mount": fullest.get("mount"), "percent": fullest.get("percent")} if fullest else None
        ),
        "gpu": (
            {
                "count": len(gpus),
                "max_util_percent": max(g.get("util_percent") or 0 for g in gpus),
                "max_vram_percent": max(g.get("mem_percent") or 0 for g in gpus),
            }
            if gpus
            else None
        ),
        "containers": (
            {
                "running": docker.get("running"),
                "total": docker.get("total"),
                "unhealthy": len(docker.get("unhealthy") or []),
            }
            if docker
            else None
        ),
        "failed_units": len(systemd.get("failed") or []) if systemd else None,
        "pending_updates": (
            {
                "apt": apt.get("upgradable_count"),
                "security": apt.get("security_count"),
                "reboot_required": bool(apt.get("reboot_required")),
                "images": sum(1 for i in images if i.get("status") == "update_available"),
            }
            if apt or images
            else None
        ),
    }
