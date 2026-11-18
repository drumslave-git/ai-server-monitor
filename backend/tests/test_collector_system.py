from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ai_server_monitor.collectors import system as system_mod
from ai_server_monitor.collectors.base import HostEnv, RateTracker
from ai_server_monitor.collectors.system import (
    SystemCollector,
    container_id_from_ancestry,
    container_id_from_cgroup,
    container_id_from_shim_cmdline,
    parse_mounts,
    parse_net_dev,
)
from ai_server_monitor.config import SystemCollectorSettings

MOUNTS = """\
/dev/nvme0n1p2 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev 0 0
/dev/nvme0n1p1 /boot/efi vfat rw,relatime 0 0
/dev/sda1 /mnt/media\\040library ext4 rw,relatime 0 0
/dev/sda1 /srv/exports/media-library ext4 rw,relatime 0 0
overlay /var/lib/docker/overlay2/abc/merged overlay rw 0 0
/dev/nvme0n1p2 /var/lib/docker ext4 rw,relatime 0 0
tank/data /tank/data zfs rw,xattr 0 0
//nas/share /mnt/nas cifs rw 0 0
/dev/loop3 /snap/core/123 squashfs ro 0 0
cgroup2 /sys/fs/cgroup cgroup2 rw 0 0
"""

NET_DEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000 10 0 0 0 0 0 0 1000 10 0 0 0 0 0 0
  eth0: 5000000 4000 2 1 0 0 0 0 7000000 3000 0 3 0 0 0 0
vethab12: 10 1 0 0 0 0 0 0 10 1 0 0 0 0 0 0
br-106486da7062: 10 1 0 0 0 0 0 0 10 1 0 0 0 0 0 0
docker0: 10 1 0 0 0 0 0 0 10 1 0 0 0 0 0 0
"""


def test_parse_mounts_filters_and_dedupes() -> None:
    mounts = parse_mounts(MOUNTS)
    assert [m["mount"] for m in mounts] == [
        "/",
        "/boot/efi",
        "/mnt/media library",  # octal escape decoded
        "/mnt/nas",
        "/tank/data",
    ]
    by_mount = {m["mount"]: m for m in mounts}
    assert by_mount["/"]["device"] == "/dev/nvme0n1p2"  # /var/lib/docker bind of same device
    assert by_mount["/tank/data"]["fstype"] == "zfs"


def test_parse_mounts_keeps_shortest_mountpoint_for_device() -> None:
    mounts = parse_mounts("/dev/sdb1 /srv/a/b ext4 rw 0 0\n/dev/sdb1 /srv ext4 rw 0 0\n")
    assert [m["mount"] for m in mounts] == ["/srv"]


def test_parse_mounts_dedupes_non_path_sources() -> None:
    text = (
        "C:\\134 /mnt/host/c 9p rw 0 0\n"
        "C:\\134 /run/desktop/mnt/host/c 9p rw 0 0\n"
        "nas:/export /mnt/nas nfs4 rw 0 0\n"
        "nas:/export /srv/nas-again nfs4 rw 0 0\n"
        "tank/data /tank/data zfs rw 0 0\n"
        "tank/media /tank/media zfs rw 0 0\n"
        "none /parent-distro/init rootfs rw 0 0\n"
        "fuse /mnt/a fuse rw 0 0\n"
        "fuse /mnt/b fuse rw 0 0\n"
    )
    assert [m["mount"] for m in parse_mounts(text)] == [
        "/mnt/a",  # generic source: kept per mountpoint
        "/mnt/b",
        "/mnt/host/c",
        "/mnt/nas",
        "/tank/data",  # different datasets are different filesystems
        "/tank/media",
    ]


def test_parse_net_dev() -> None:
    nets = parse_net_dev(NET_DEV)
    assert nets["eth0"]["rx_bytes"] == 5_000_000
    assert nets["eth0"]["tx_bytes"] == 7_000_000
    assert nets["eth0"]["rx_errors"] == 2 and nets["eth0"]["tx_dropped"] == 3
    assert set(nets) == {"lo", "eth0", "vethab12", "br-106486da7062", "docker0"}


@pytest.mark.parametrize(
    ("cgroup", "expected"),
    [
        ("0::/system.slice/docker-" + "a" * 64 + ".scope\n", "a" * 12),
        ("12:memory:/docker/" + "b" * 64 + "\n", "b" * 12),
        ("0::/kubepods/burstable/pod1/cri-containerd-" + "c" * 64 + ".scope", "c" * 12),
        ("0::/user.slice/user-1000.slice/session-2.scope\n", None),
    ],
)
def test_container_id_from_cgroup(cgroup: str, expected: str | None) -> None:
    assert container_id_from_cgroup(cgroup) == expected


def test_container_id_from_shim_cmdline() -> None:
    cid = "f" * 64
    shim = ["/usr/bin/containerd-shim-runc-v2", "-namespace", "moby", "-id", cid,
            "-address", "/run/containerd/containerd.sock"]  # fmt: skip
    assert container_id_from_shim_cmdline(shim) == "f" * 12
    assert container_id_from_shim_cmdline(["containerd-shim-runc-v2", "-id", "short"]) is None
    assert container_id_from_shim_cmdline(["bash"]) is None


class FakeProc:
    TABLE: ClassVar[dict[int, tuple[str, list[str], int]]] = {
        # pid: (name, cmdline, ppid)
        1: ("systemd", ["/sbin/init"], 0),
        500: (
            "containerd-shim-runc-v2",
            ["containerd-shim-runc-v2", "-namespace", "moby", "-id", "e" * 64],
            1,
        ),
        501: ("s6-svscan", ["s6-svscan"], 500),
        502: ("Jellyfin", ["/app/bin/Jellyfin"], 501),
        900: ("llama-server", ["/usr/lib/ollama/llama-server"], 1),
    }

    def __init__(self, pid: int):
        self.pid = pid

    def name(self) -> str:
        return self.TABLE[self.pid][0]

    def cmdline(self) -> list[str]:
        return self.TABLE[self.pid][1]

    def parents(self) -> list[FakeProc]:
        out, ppid = [], self.TABLE[self.pid][2]
        while ppid:
            out.append(FakeProc(ppid))
            ppid = self.TABLE[ppid][2]
        return out


def test_container_id_from_ancestry() -> None:
    assert container_id_from_ancestry(502, FakeProc) == "e" * 12  # nested under the shim
    assert container_id_from_ancestry(900, FakeProc) is None  # host process


def test_persist_view_slims_processes_on_every_platform() -> None:
    """Regression: persist_view was once accidentally moved out of the class, so full
    process rows (with cmd) went into history. This test runs on Windows too."""
    collector = SystemCollector(SystemCollectorSettings(), HostEnv())
    row = {"pid": 1, "name": "x", "cpu_percent": 1.0, "rss": 1, "container_id": None,
           "cmd": "/bin/x --secret", "user": "root", "threads": 1}  # fmt: skip
    data = {"cpu": {}, "processes": {"total": 1, "zombies": 0, "top_cpu": [row],
                                      "top_memory": [row]}}  # fmt: skip
    slim = collector.persist_view(data)
    assert slim["processes"]["top_cpu"] == [
        {"pid": 1, "name": "x", "cpu_percent": 1.0, "rss": 1, "container_id": None}
    ]
    assert slim["cpu"] == {}


def test_rate_tracker() -> None:
    rates = RateTracker()
    assert rates.rate("x", 100, now=0.0) is None
    assert rates.rate("x", 300, now=2.0) == 100.0
    assert rates.rate("x", 50, now=3.0) is None  # counter reset


@pytest.fixture
def fake_host(tmp_path: Path) -> HostEnv:
    root = tmp_path / "host"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "hostname").write_text("myserver\n")
    proc = tmp_path / "proc"
    (proc / "1" / "net").mkdir(parents=True)
    (proc / "1" / "mounts").write_text(MOUNTS)
    (proc / "1" / "net" / "dev").write_text(NET_DEV)
    return HostEnv(root=root, proc=proc, sys=tmp_path / "sys")


def test_host_view_uses_pid1_mounts_and_net(
    fake_host: HostEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []

    def fake_statvfs(path: Path) -> dict[str, Any]:
        seen.append(path)
        return {"total": 100, "used": 91, "avail": 9, "percent": 91.0, "inodes_percent": 3.0}

    monkeypatch.setattr(system_mod, "statvfs_usage", fake_statvfs)
    collector = SystemCollector(SystemCollectorSettings(), fake_host)

    filesystems = collector._filesystems()
    assert filesystems[0]["mount"] == "/"
    assert filesystems[0]["percent"] == 91.0
    assert seen[0] == fake_host.root  # statvfs on the host-mounted path
    assert collector._hostname() == "myserver"

    first = collector._network(now=0.0)
    assert [n["name"] for n in first] == ["eth0"]  # lo, veth, bridges, docker0 filtered
    assert first[0]["rx_bps"] is None  # no previous sample yet
    (fake_host.proc / "1" / "net" / "dev").write_text(
        NET_DEV.replace("5000000 4000", "5100000 4100")
    )
    second = collector._network(now=10.0)
    assert second[0]["rx_bps"] == 10_000


@pytest.mark.skipif(
    sys.platform == "win32", reason="psutil process enumeration takes ~20s on Windows"
)
async def test_collect_runs_on_this_machine() -> None:
    """Smoke test against the real OS (Windows/Linux/macOS dev machines)."""
    collector = SystemCollector(SystemCollectorSettings(top_n=3), HostEnv())
    data = await collector.collect()
    assert 0 <= data["cpu"]["percent"] <= 100
    assert data["memory"]["total"] > 0
    assert data["processes"]["total"] > 0
    assert len(data["processes"]["top_memory"]) <= 3
    slim = collector.persist_view(data)
    assert all("cmd" not in p for p in slim["processes"]["top_memory"])  # slimmed for history
