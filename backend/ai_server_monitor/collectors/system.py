"""Host CPU, memory, load, filesystems, disk I/O, network, temperatures and top processes.

Requires `pid: host` in the container so /proc shows host processes. Host mounts and
network interfaces are read from the host's PID 1 (/proc/1/mounts, /proc/1/net/dev),
because the container's own view only shows its namespaces.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import psutil

from ..config import SystemCollectorSettings
from .base import Collector, HostEnv, RateTracker, round_or_none

# Filesystems that never represent user-visible storage capacity.
EXCLUDED_FS_TYPES = frozenset(
    {
        "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "configfs", "debugfs",
        "devpts", "devtmpfs", "efivarfs", "fusectl", "fuse.lxcfs", "fuse.portal",
        "hugetlbfs", "mqueue", "nsfs", "overlay", "proc", "pstore", "ramfs",
        "rpc_pipefs", "securityfs", "selinuxfs", "squashfs", "sysfs", "tmpfs",
        "tracefs", "nfsd", "fuse.gvfsd-fuse", "shm", "iso9660", "rootfs",
    }
)  # fmt: skip
EXCLUDED_MOUNT_PREFIXES = ("/var/lib/docker/", "/run/docker/", "/snap/", "/var/snap/",
                           "/var/lib/containers/", "/proc/", "/sys/", "/dev/")  # fmt: skip
EXCLUDED_NET_PREFIXES = ("lo", "veth", "docker_gwbridge", "virbr", "cali", "flannel", "cni")
_CONTAINER_ID = re.compile(r"(?:docker[-/]|cri-containerd-|libpod-|/containerd/)([0-9a-f]{64})")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


# --------------------------------------------------------------------------- parsers


def _unescape_mount(path: str) -> str:
    return _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), path)


def parse_mounts(text: str) -> list[dict[str, str]]:
    """Parse /proc/*/mounts into real, de-duplicated filesystems (shortest mountpoint per
    device wins, so bind mounts don't double count)."""
    by_device: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], _unescape_mount(parts[1]), parts[2]
        if fstype in EXCLUDED_FS_TYPES:
            continue
        if any((mountpoint + "/").startswith(p) for p in EXCLUDED_MOUNT_PREFIXES):
            continue
        # Same source mounted several times (bind mounts, NFS/SMB shares, ZFS datasets) is
        # one filesystem. Generic sources ("none", "fstype-as-device") are per mountpoint.
        generic = device in ("none", "", fstype) or device == fstype.split(".")[-1]
        key = f"{device}:{mountpoint}" if generic else f"{fstype}:{device}"
        current = by_device.get(key)
        if current is None or len(mountpoint) < len(current["mount"]):
            by_device[key] = {"device": device, "mount": mountpoint, "fstype": fstype}
    return sorted(by_device.values(), key=lambda m: m["mount"])


def parse_net_dev(text: str) -> dict[str, dict[str, int]]:
    """Parse /proc/net/dev into per-interface counters."""
    result: dict[str, dict[str, int]] = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        fields = rest.split()
        if len(fields) < 16:
            continue
        values = [int(v) for v in fields[:16]]
        result[name.strip()] = {
            "rx_bytes": values[0],
            "rx_packets": values[1],
            "rx_errors": values[2],
            "rx_dropped": values[3],
            "tx_bytes": values[8],
            "tx_packets": values[9],
            "tx_errors": values[10],
            "tx_dropped": values[11],
        }
    return result


def container_id_from_cgroup(text: str) -> str | None:
    match = _CONTAINER_ID.search(text)
    return match.group(1)[:12] if match else None


def statvfs_usage(path: Path) -> dict[str, Any] | None:
    """df-style usage for a path; None if not accessible (or not POSIX)."""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:
        return None
    try:
        st = statvfs(path)
    except OSError:
        return None
    if st.f_blocks == 0:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bfree * st.f_frsize
    avail = st.f_bavail * st.f_frsize
    used = total - free
    denom = used + avail
    inodes_pct = round((st.f_files - st.f_ffree) / st.f_files * 100, 1) if st.f_files else None
    return {
        "total": total,
        "used": used,
        "avail": avail,
        "percent": round(used / denom * 100, 1) if denom else 0.0,
        "inodes_percent": inodes_pct,
    }


# --------------------------------------------------------------------------- collector


class SystemCollector(Collector):
    name = "system"
    timeout = timedelta(seconds=20)
    cfg: SystemCollectorSettings

    def __init__(self, cfg: SystemCollectorSettings, env: HostEnv):
        super().__init__(cfg, env)
        self._rates = RateTracker()
        self._cpu_primed = False

    async def collect(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._collect_sync)

    # Everything below runs in a worker thread (psutil and file reads block).

    def _collect_sync(self) -> dict[str, Any]:
        if not self._cpu_primed:
            psutil.cpu_percent(None)
            psutil.cpu_times_percent(None)
            self._cpu_primed = True
            time.sleep(0.25)
        now = time.monotonic()
        return {
            "hostname": self._hostname(),
            "kernel": os.uname().release if hasattr(os, "uname") else None,
            "uptime_s": int(time.time() - psutil.boot_time()),
            "cpu": self._cpu(),
            "memory": self._memory(),
            "filesystems": self._filesystems(),
            "disks": self._disks(now),
            "network": self._network(now),
            "temperatures": self._temperatures(),
            "processes": self._processes(),
        }

    def _hostname(self) -> str | None:
        try:
            return self.env.path("/etc/hostname").read_text().strip() or None
        except OSError:
            return os.uname().nodename if hasattr(os, "uname") else None

    def _cpu(self) -> dict[str, Any]:
        times = psutil.cpu_times_percent(None)
        logical = psutil.cpu_count() or 1
        try:
            load = os.getloadavg()
        except (AttributeError, OSError):
            load = None
        freq = None
        try:
            f = psutil.cpu_freq()
            freq = round(f.current) if f else None
        except (NotImplementedError, FileNotFoundError, AttributeError):
            pass
        return {
            "percent": psutil.cpu_percent(None),
            "iowait_percent": round_or_none(getattr(times, "iowait", None)),
            "steal_percent": round_or_none(getattr(times, "steal", None)),
            "cores_logical": logical,
            "cores_physical": psutil.cpu_count(logical=False),
            "load": [round(x, 2) for x in load] if load else None,
            "load_per_core": round(load[0] / logical, 2) if load else None,
            "freq_mhz": freq,
        }

    def _memory(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {
            "total": vm.total,
            "available": vm.available,
            "used": vm.total - vm.available,
            "percent": vm.percent,
            "cached": getattr(vm, "cached", None),
            "swap_total": sw.total,
            "swap_used": sw.used,
            "swap_percent": sw.percent,
        }

    def _mount_table(self) -> str | None:
        candidates = [self.env.proc / "1" / "mounts"] if self.env.in_container else []
        candidates.append(self.env.proc / "self" / "mounts")
        for path in candidates:
            try:
                return path.read_text()
            except OSError:
                continue
        return None

    def _filesystems(self) -> list[dict[str, Any]]:
        table = self._mount_table()
        if table is None:  # non-Linux dev machine
            mounts = [
                {"device": p.device, "mount": p.mountpoint, "fstype": p.fstype}
                for p in psutil.disk_partitions(all=False)
            ]
        else:
            mounts = parse_mounts(table)
        out: list[dict[str, Any]] = []
        for mount in mounts:
            path = self.env.path(mount["mount"]) if self.env.in_container else Path(mount["mount"])
            usage = statvfs_usage(path)
            if usage is None and table is None:
                try:
                    u = psutil.disk_usage(mount["mount"])
                    usage = {"total": u.total, "used": u.used, "avail": u.free,
                             "percent": u.percent, "inodes_percent": None}  # fmt: skip
                except OSError:
                    usage = None
            if usage is not None:
                out.append({**mount, **usage})
        return out

    def _disks(self, now: float) -> list[dict[str, Any]]:
        try:
            counters = psutil.disk_io_counters(perdisk=True) or {}
        except (RuntimeError, OSError):
            return []
        block_dir = self.env.sys / "block"
        whole = {p.name for p in block_dir.iterdir()} if block_dir.is_dir() else None
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name, c in counters.items():
            if name.startswith(("loop", "ram", "zram", "sr")):
                continue
            if whole is not None and name not in whole:
                continue  # partition; the whole disk is reported
            seen.update({f"{name}:r", f"{name}:w", f"{name}:busy"})
            read = self._rates.rate(f"{name}:r", c.read_bytes, now)
            write = self._rates.rate(f"{name}:w", c.write_bytes, now)
            busy_ms = getattr(c, "busy_time", None)
            busy = self._rates.rate(f"{name}:busy", busy_ms, now) if busy_ms is not None else None
            out.append(
                {
                    "name": name,
                    "read_bps": round_or_none(read, 0),
                    "write_bps": round_or_none(write, 0),
                    "busy_percent": round_or_none(min(busy / 10, 100.0), 1)
                    if busy is not None
                    else None,  # busy_time is ms per s → /1000*100
                }
            )
        return out

    def _network(self, now: float) -> list[dict[str, Any]]:
        counters: dict[str, dict[str, int]] | None = None
        for path in (self.env.proc / "1" / "net" / "dev", self.env.proc / "net" / "dev"):
            try:
                counters = parse_net_dev(path.read_text())
                break
            except OSError:
                continue
        if counters is None:
            counters = {
                name: {"rx_bytes": c.bytes_recv, "tx_bytes": c.bytes_sent,
                       "rx_errors": c.errin, "tx_errors": c.errout,
                       "rx_dropped": c.dropin, "tx_dropped": c.dropout}
                for name, c in psutil.net_io_counters(pernic=True).items()
            }  # fmt: skip
        out: list[dict[str, Any]] = []
        for name, c in sorted(counters.items()):
            if name.startswith(EXCLUDED_NET_PREFIXES):
                continue
            rx = self._rates.rate(f"net:{name}:rx", c["rx_bytes"], now)
            tx = self._rates.rate(f"net:{name}:tx", c["tx_bytes"], now)
            out.append(
                {
                    "name": name,
                    "rx_bps": round_or_none(rx, 0),
                    "tx_bps": round_or_none(tx, 0),
                    "rx_bytes": c["rx_bytes"],
                    "tx_bytes": c["tx_bytes"],
                    "errors": c.get("rx_errors", 0) + c.get("tx_errors", 0),
                    "dropped": c.get("rx_dropped", 0) + c.get("tx_dropped", 0),
                }
            )
        return out

    def _temperatures(self) -> list[dict[str, Any]]:
        sensors = getattr(psutil, "sensors_temperatures", None)
        if sensors is None:
            return []
        try:
            readings = sensors()
        except (OSError, RuntimeError):
            return []
        out: list[dict[str, Any]] = []
        for chip, entries in readings.items():
            for entry in entries:
                if entry.current is None or entry.current <= 0:
                    continue
                out.append(
                    {
                        "sensor": chip,
                        "label": entry.label or chip,
                        "current": round(entry.current, 1),
                        "high": entry.high,
                        "critical": entry.critical,
                    }
                )
        out.sort(key=lambda t: t["current"], reverse=True)
        return out[:20]

    def _host_users(self) -> dict[int, str]:
        """uid -> name from the *host's* /etc/passwd (the container's doesn't know them)."""
        try:
            text = self.env.path("/etc/passwd").read_text(errors="replace")
        except OSError:
            return {}
        users: dict[int, str] = {}
        for line in text.splitlines():
            parts = line.split(":")
            if len(parts) > 2 and parts[2].isdigit():
                users[int(parts[2])] = parts[0]
        return users

    def _processes(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        statuses: dict[str, int] = {}
        has_uids = hasattr(psutil.Process, "uids")  # POSIX only
        attrs = ["pid", "name", "memory_info", "status", "num_threads"]
        if has_uids:
            attrs.append("uids")
        users = self._host_users() if has_uids else {}
        total_mem = psutil.virtual_memory().total or 1
        for proc in psutil.process_iter(attrs):
            info = proc.info
            uids = info.get("uids")
            user = (users.get(uids.real) or str(uids.real)) if uids else None
            status = info.get("status") or "unknown"
            statuses[status] = statuses.get(status, 0) + 1
            try:
                cpu = proc.cpu_percent(None)  # vs. previous call on the cached Process
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                cpu = 0.0
            mem = info.get("memory_info")
            rss = mem.rss if mem else 0
            rows.append(
                {
                    "pid": info["pid"],
                    "name": info.get("name") or "?",
                    "user": user,
                    "cpu_percent": round(cpu, 1),
                    "rss": rss,
                    "mem_percent": round(rss / total_mem * 100, 1),
                    "threads": info.get("num_threads"),
                }
            )
        n = self.cfg.top_n
        top_cpu = sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:n]
        top_mem = sorted(rows, key=lambda r: r["rss"], reverse=True)[:n]
        for row in {id(r): r for r in top_cpu + top_mem}.values():
            self._enrich(row)
        return {
            "total": len(rows),
            "by_status": statuses,
            "zombies": statuses.get("zombie", 0),
            "top_cpu": top_cpu,
            "top_memory": top_mem,
        }

    def _enrich(self, row: dict[str, Any]) -> None:
        pid = row["pid"]
        try:
            cmd = psutil.Process(pid).cmdline()
            row["cmd"] = " ".join(cmd)[:160] if cmd else None
        except (psutil.Error, OSError):
            row["cmd"] = None
        try:
            cgroup = (self.env.proc / str(pid) / "cgroup").read_text()
            row["container_id"] = container_id_from_cgroup(cgroup)
        except OSError:
            row["container_id"] = None

    def persist_view(self, data: dict[str, Any]) -> dict[str, Any]:
        procs = data.get("processes", {})
        slim = ("pid", "name", "cpu_percent", "rss", "container_id")
        return {
            **{k: v for k, v in data.items() if k != "processes"},
            "processes": {
                "total": procs.get("total"),
                "zombies": procs.get("zombies"),
                "top_cpu": [{k: p.get(k) for k in slim} for p in procs.get("top_cpu", [])[:5]],
                "top_memory": [
                    {k: p.get(k) for k in slim} for p in procs.get("top_memory", [])[:5]
                ],
            },
        }
