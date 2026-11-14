from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ai_server_monitor.collectors import apt as apt_mod
from ai_server_monitor.collectors.apt import AptCollector, parse_os_release, parse_upgradable
from ai_server_monitor.collectors.base import CollectorUnavailable, HostEnv
from ai_server_monitor.config import CollectorSettings

APT_OUTPUT = """\
Listing...
curl/noble-updates,noble-security 8.5.0-2ubuntu10.6 amd64 [upgradable from: 8.5.0-2ubuntu10.5]
docker-ce/noble 5:29.1.0-1~ubuntu.24.04~noble amd64 [upgradable from: 5:29.0.2-1~ubuntu.24.04~noble]
libssl3t64/noble-updates,noble-security 3.0.13-0ubuntu3.6 amd64 [upgradable from: 3.0.13-0ubuntu3.5]
tzdata/stable-updates 2026a-0+deb12u1 all [upgradable from: 2025b-0+deb12u1]
"""


def test_parse_upgradable() -> None:
    packages = parse_upgradable(APT_OUTPUT)
    assert [p["name"] for p in packages] == ["curl", "libssl3t64", "docker-ce", "tzdata"]
    curl = packages[0]
    assert curl["security"] is True
    assert curl["current"] == "8.5.0-2ubuntu10.5"
    assert curl["candidate"] == "8.5.0-2ubuntu10.6"
    assert packages[2]["candidate"] == "5:29.1.0-1~ubuntu.24.04~noble"
    assert packages[2]["security"] is False


def test_parse_os_release() -> None:
    info = parse_os_release('NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 24.04.3 LTS"\nID=ubuntu\n')
    assert info["PRETTY_NAME"] == "Ubuntu 24.04.3 LTS" and info["ID"] == "ubuntu"


@pytest.fixture
def host(tmp_path: Path) -> HostEnv:
    root = tmp_path / "host"
    (root / "var/lib/dpkg").mkdir(parents=True)
    (root / "var/lib/dpkg/status").write_text("Package: base-files\n")
    lists = root / "var/lib/apt/lists"
    lists.mkdir(parents=True)
    release = lists / "archive.ubuntu.com_ubuntu_dists_noble-updates_InRelease"
    release.write_text("x")
    two_hours_ago = time.time() - 7200
    os.utime(release, (two_hours_ago, two_hours_ago))
    (root / "etc").mkdir()
    (root / "etc/os-release").write_text('PRETTY_NAME="Ubuntu 24.04.3 LTS"\n')
    (root / "run").mkdir()
    (root / "run/reboot-required").write_text("*** System restart required ***\n")
    (root / "run/reboot-required.pkgs").write_text("linux-image-6.8.0-90-generic\nlibc6\nlibc6\n")
    return HostEnv(root=root)


async def test_collect(host: HostEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str], **kwargs: object) -> str:
        calls.append(args)
        return APT_OUTPUT

    monkeypatch.setattr(apt_mod.shutil, "which", lambda name: "/usr/bin/apt")
    monkeypatch.setattr(apt_mod, "run_command", fake_run)
    collector = AptCollector(CollectorSettings(), host)
    await collector.check()
    data = await collector.collect()

    args = calls[0]
    assert args[:3] == ["apt", "list", "--upgradable"]
    assert f"Dir={host.root}" in args  # reads host package state
    assert "Debug::NoLocking=1" in args and "Dir::Cache::pkgcache=" in args
    assert data["distro"] == "Ubuntu 24.04.3 LTS"
    assert data["upgradable_count"] == 4 and data["security_count"] == 2
    assert data["reboot_required"] is True
    assert data["reboot_required_pkgs"] == ["libc6", "linux-image-6.8.0-90-generic"]
    assert 1.9 <= data["lists_age_h"] <= 2.1


async def test_unavailable_without_host_dpkg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(apt_mod.shutil, "which", lambda name: "/usr/bin/apt")
    (tmp_path / "host").mkdir()
    collector = AptCollector(CollectorSettings(), HostEnv(root=tmp_path / "host"))
    with pytest.raises(CollectorUnavailable, match="dpkg"):
        await collector.check()
