"""Pending apt updates on a Debian/Ubuntu host.

Runs the container's `apt` against the host's package state (`-o Dir=/host`) with the
binary caches and locking disabled, so nothing is written. The result is only as fresh
as the host's last `apt update` (usually the daily apt timer) — `lists_age_h` says how old.
"""

from __future__ import annotations

import re
import shutil
import time
from datetime import timedelta
from typing import Any

from .base import Collector, CollectorUnavailable, run_command

_UPGRADABLE = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<suites>\S+)\s+(?P<new>\S+)\s+(?P<arch>\S+)"
    r"\s+\[upgradable from:\s*(?P<old>[^\]]+)\]"
)


def parse_upgradable(text: str) -> list[dict[str, Any]]:
    packages: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _UPGRADABLE.match(line.strip())
        if not match:
            continue
        suites = match.group("suites").split(",")
        packages.append(
            {
                "name": match.group("name"),
                "current": match.group("old").strip(),
                "candidate": match.group("new"),
                "arch": match.group("arch"),
                "suites": suites,
                "security": any("security" in s for s in suites),
            }
        )
    packages.sort(key=lambda p: (not p["security"], p["name"]))
    return packages


def parse_os_release(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


class AptCollector(Collector):
    name = "apt"
    timeout = timedelta(minutes=3)

    async def check(self) -> None:
        if shutil.which("apt") is None:
            raise CollectorUnavailable("apt binary not available in this environment")
        if not self.env.path("/var/lib/dpkg/status").is_file():
            raise CollectorUnavailable(
                "host dpkg status not found (not Debian/Ubuntu, or host root not mounted)"
            )

    def _apt_args(self) -> list[str]:
        args = ["apt", "list", "--upgradable"]
        if self.env.in_container:
            args += ["-o", f"Dir={self.env.root}"]
        args += [
            "-o", "Dir::Cache::pkgcache=",
            "-o", "Dir::Cache::srcpkgcache=",
            "-o", "Debug::NoLocking=1",
            "-o", "APT::Get::Show-User-Simulation-Note=0",
        ]  # fmt: skip
        return args

    async def collect(self) -> dict[str, Any]:
        await self.check()
        output = await run_command(self._apt_args(), timeout=150)
        packages = parse_upgradable(output)
        return {
            "distro": self._distro(),
            "upgradable_count": len(packages),
            "security_count": sum(p["security"] for p in packages),
            "packages": packages[:300],
            "truncated": len(packages) > 300,
            "lists_age_h": self._lists_age_hours(),
            **self._reboot_required(),
        }

    def _distro(self) -> str | None:
        for path in ("/etc/os-release", "/usr/lib/os-release"):
            try:
                info = parse_os_release(self.env.path(path).read_text())
            except OSError:
                continue
            return info.get("PRETTY_NAME") or info.get("NAME")
        return None

    def _lists_age_hours(self) -> float | None:
        lists = self.env.path("/var/lib/apt/lists")
        try:
            newest = max(
                (p.stat().st_mtime for p in lists.iterdir() if p.name.endswith("Release")),
                default=None,
            )
        except OSError:
            return None
        return round((time.time() - newest) / 3600, 1) if newest else None

    def _reboot_required(self) -> dict[str, Any]:
        for base in ("/run", "/var/run"):
            flag = self.env.path(f"{base}/reboot-required")
            if flag.exists():
                try:
                    pkgs = self.env.path(f"{base}/reboot-required.pkgs").read_text().split()
                except OSError:
                    pkgs = []
                return {"reboot_required": True, "reboot_required_pkgs": sorted(set(pkgs))}
        return {"reboot_required": False, "reboot_required_pkgs": []}
