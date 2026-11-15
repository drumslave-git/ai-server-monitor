"""Generates the synthetic benchmark snapshots (`python _build.py`).

Everything here is invented: host name, addresses, container ids, GPU uuid, titles. The shape
is that of GET /api/collectors of a real instance (collector name -> status + result), plus
a top-level `now` (the moment the run pretends it is) and an optional `history`
(older samples per collector, `hours_ago`) for the digest's trends.

`base` is a healthy home AI/media server. The other files are small variations of it, made by
the functions at the bottom; they exist because a JSON merge-patch cannot change one element of
a list (containers, filesystems, ...).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

NOW = 1780315200.0  # 2026-06-01 12:00:00 UTC
GIB = 1024**3
MIB = 1024**2
TB = 10**12
OUT = Path(__file__).resolve().parent


def _hex(seed: str, n: int) -> str:
    return hashlib.sha256(("bench:" + seed).encode()).hexdigest()[:n]


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def _entry(name: str, data: dict[str, Any], ago: float, dur: float) -> dict[str, Any]:
    """One collector in the snapshot format of `bench/schema.py`."""
    del name, dur
    return {"ok": True, "state": "ok", "age_s": ago, "data": data}


# ------------------------------------------------------------------------------ docker

# name, image, compose project, mem MiB, cpu %, pids, restart_count, health
Row = tuple[str, str, str, float, float, int, int, str | None]
_CONTAINERS: list[Row] = [
    ("ollama", "ollama/ollama:0.9.6", "ai", 9800, 2.1, 38, 0, None),
    ("comfyui", "ghcr.io/example-org/comfyui:1.4", "ai", 13400, 0.8, 52, 7, None),
    ("open-webui", "ghcr.io/open-webui/open-webui:v0.6.18", "ai", 1650, 0.6, 44, 0, "healthy"),
    ("llamacpp-chat", "ghcr.io/ggml-org/llama.cpp:server-cuda", "ai", 2900, 1.4, 21, 0, "healthy"),
    ("llamacpp-embedding", "ghcr.io/ggml-org/llama.cpp:server-cuda", "ai", 1100, 0.3, 16, 0, None),
    ("qdrant", "qdrant/qdrant:v1.14.1", "ai", 820, 0.4, 36, 0, None),
    ("searxng", "searxng/searxng:2025.7.20", "ai", 310, 0.2, 14, 0, None),
    ("radarr", "lscr.io/linuxserver/radarr:5.26.2", "media", 410, 0.5, 31, 0, "healthy"),
    ("sonarr", "lscr.io/linuxserver/sonarr:4.0.15", "media", 460, 0.7, 33, 0, "healthy"),
    ("whisparr", "ghcr.io/example-org/whisparr:3.0.2", "media", 380, 0.4, 29, 0, "healthy"),
    ("prowlarr", "lscr.io/linuxserver/prowlarr:1.37.0", "media", 330, 0.3, 27, 0, "healthy"),
    ("qbittorrent", "lscr.io/linuxserver/qbittorrent:5.1.2", "media", 1380, 4.2, 48, 0, "healthy"),
    ("flaresolverr", "ghcr.io/flaresolverr/flaresolverr:v3.3.21", "media", 270, 0.1, 12, 0, None),
    ("jellyfin", "jellyfin/jellyfin:10.10.7", "media", 1150, 1.9, 61, 0, "healthy"),
    ("jellyseerr", "fallenbagel/jellyseerr:2.7.2", "media", 290, 0.1, 18, 0, "healthy"),
    ("bazarr", "lscr.io/linuxserver/bazarr:1.5.2", "media", 340, 0.2, 22, 0, "healthy"),
    (
        "homeassistant",
        "ghcr.io/home-assistant/home-assistant:2025.7.3",
        "home",
        980,
        2.8,
        63,
        0,
        None,
    ),
    ("mosquitto", "eclipse-mosquitto:2.0.21", "home", 14, 0.0, 3, 0, None),
    ("zigbee2mqtt", "koenkk/zigbee2mqtt:2.5.1", "home", 190, 0.4, 26, 0, None),
    ("esphome", "ghcr.io/esphome/esphome:2025.7.2", "home", 240, 0.1, 17, 0, None),
    ("grafana", "grafana/grafana:12.0.2", "monitoring", 270, 0.5, 24, 0, "healthy"),
    ("prometheus", "prom/prometheus:v3.5.0", "monitoring", 1750, 3.1, 19, 0, "healthy"),
    ("loki", "grafana/loki:3.5.2", "monitoring", 410, 0.9, 25, 0, None),
    ("promtail", "grafana/promtail:3.5.2", "monitoring", 95, 0.3, 14, 0, None),
    ("alertmanager", "prom/alertmanager:v0.28.1", "monitoring", 28, 0.0, 10, 0, None),
    ("cadvisor", "gcr.io/cadvisor/cadvisor:v0.52.1", "monitoring", 520, 6.5, 37, 0, "healthy"),
    ("node-exporter", "prom/node-exporter:v1.9.1", "monitoring", 24, 0.1, 9, 0, None),
    ("gpu-exporter", "utkuozdemir/nvidia_gpu_exporter:1.3.1", "monitoring", 22, 0.2, 8, 0, None),
    ("proxy-manager", "jc21/nginx-proxy-manager:2.12.3", "edge", 230, 0.2, 31, 0, "healthy"),
    ("vaultwarden", "vaultwarden/server:1.34.1", "edge", 48, 0.0, 14, 0, "healthy"),
    ("gitea", "gitea/gitea:1.24.3", "dev", 360, 0.3, 28, 0, None),
    ("gitea-db", "postgres:17-alpine", "dev", 120, 0.2, 12, 0, None),
    (
        "ai-server-monitor",
        "ghcr.io/example-org/ai-server-monitor:latest",
        "monitor",
        160,
        1.5,
        10,
        0,
        "healthy",
    ),
    (
        "ai-server-monitor-docker-proxy",
        "tecnativa/docker-socket-proxy:v0.4.1",
        "monitor",
        28,
        0.1,
        13,
        0,
        None,
    ),
]


def _container(row: Row) -> dict[str, Any]:
    name, image, project, mem_mib, cpu, pids, restarts, health = row
    mem_total = 64 * GIB
    mem = int(mem_mib * MIB)
    uptime_h = 24 * 13 + (sum(map(ord, name)) % 9)
    started = NOW - uptime_h * 3600
    status = f"Up {uptime_h // 24} days" + (f" ({health})" if health else "")
    return {
        "id": _hex(name, 12),
        "name": name,
        "image": image,
        "image_id": "sha256:" + _hex("img" + image, 64),
        "state": "running",
        "status": status,
        "compose_project": project,
        "compose_service": name,
        "health": health,
        "restart_count": restarts,
        "restarts_delta": 0,
        "restarted_since_last": False,
        "started_at": _iso(started),
        "oom_killed": False,
        "exit_code": None,
        "restart_policy": "unless-stopped",
        "cpu_percent": cpu,
        "mem_usage": mem,
        "mem_limit": mem_total,
        "mem_percent": round(mem / mem_total * 100, 1),
        "net_rx_bps": round(cpu * 3100 + 900, 1),
        "net_tx_bps": round(cpu * 2300 + 400, 1),
        "blk_read_bps": 0.0,
        "blk_write_bps": round(cpu * 900, 1),
        "pids": pids,
        "log_lines": 20 + pids * 3,
        "log_errors": 0,
        "log_error_samples": [],
    }


def _docker() -> dict[str, Any]:
    containers = [_container(r) for r in _CONTAINERS]
    return {
        "engine": {
            "version": "28.3.2",
            "storage_driver": "overlayfs",
            "images": 41,
            "warnings": [],
        },
        "summary": {
            "total": len(containers),
            "running": len(containers),
            "unhealthy": [],
            "restarting": [],
            "exited_with_error": [],
            "restarted_recently": [],
            "oom_killed": [],
        },
        "containers": containers,
        "disk_usage": {
            "images_bytes": 74 * GIB,
            "images_unused_bytes": 9 * GIB,
            "images_count": 41,
            "containers_rw_bytes": 2 * GIB,
            "volumes_bytes": 41 * GIB,
            "volumes_count": 17,
            "build_cache_bytes": 1 * GIB,
            "largest_volumes": [
                {"name": "ai_open-webui-data", "bytes": 12 * GIB},
                {"name": "monitoring_prometheus_data", "bytes": 9 * GIB},
                {"name": "monitoring_loki_data", "bytes": 7 * GIB},
                {"name": "ai_qdrant_storage", "bytes": 5 * GIB},
                {"name": "home_homeassistant_config", "bytes": 3 * GIB},
            ],
            "ts": NOW - 700,
        },
    }


# ------------------------------------------------------------------------------ system


def _fs(
    device: str, mount: str, fstype: str, total: int, used_pct: float, inodes: float | None
) -> dict[str, Any]:
    used = int(total * used_pct / 100)
    return {
        "device": device,
        "mount": mount,
        "fstype": fstype,
        "total": total,
        "used": used,
        "avail": total - used,
        "percent": used_pct,
        "inodes_percent": inodes,
    }


def _filesystems() -> list[dict[str, Any]]:
    return [
        _fs("/dev/nvme0n1p2", "/", "ext4", 1 * TB - 38 * 10**9, 38.2, 4.1),
        _fs("/dev/nvme0n1p1", "/boot/efi", "vfat", 1_124_995_072, 0.6, None),
        _fs("/dev/nvme1n1p1", "/mnt/disks/models", "ext4", 2 * TB - 70 * 10**9, 52.4, 0.3),
        _fs("/dev/sdb1", "/mnt/disks/media", "ext4", 14 * TB - 500 * 10**9, 61.0, 1.2),
        _fs("/dev/sdc1", "/mnt/disks/downloads", "ext4", 4 * TB - 140 * 10**9, 44.3, 2.2),
        _fs("/dev/sdd1", "/mnt/disks/backup", "ext4", 8 * TB - 280 * 10**9, 33.1, 0.6),
    ]


def _proc(
    pid: int, name: str, user: str, cpu: float, rss_mib: float, cmd: str, container: str | None
) -> dict[str, Any]:
    return {
        "pid": pid,
        "name": name,
        "user": user,
        "cpu_percent": cpu,
        "rss": int(rss_mib * MIB),
        "mem_percent": round(rss_mib * MIB / (64 * GIB) * 100, 1),
        "threads": 20 + pid % 30,
        "cmd": cmd,
        "container_id": _hex(container, 12) if container else None,
    }


def _system() -> dict[str, Any]:
    top_mem = [
        _proc(
            4120, "python3.11", "root", 0.8, 3120, "python3 main.py --listen --port 8188", "comfyui"
        ),
        _proc(3877, "ollama", "root", 2.1, 1890, "/usr/bin/ollama serve", "ollama"),
        _proc(
            5210,
            "prometheus",
            "nobody",
            3.1,
            1630,
            "/bin/prometheus --config.file=/etc/prometheus/prometheus.yml",
            "prometheus",
        ),
        _proc(
            6102,
            "qbittorrent-nox",
            "abc",
            4.2,
            1340,
            "/usr/bin/qbittorrent-nox --webui-port=8080",
            "qbittorrent",
        ),
        _proc(
            7344,
            "llama-server",
            "root",
            1.4,
            1180,
            "/app/llama-server -m /models/chat.gguf --port 8081",
            "llamacpp-chat",
        ),
        _proc(
            5901, "jellyfin", "abc", 1.9, 1090, "/jellyfin/jellyfin --datadir /config", "jellyfin"
        ),
        _proc(4412, "python3", "root", 0.6, 960, "python -m open_webui", "open-webui"),
        _proc(
            4800,
            "hass",
            "root",
            2.8,
            840,
            "python3 -m homeassistant --config /config",
            "homeassistant",
        ),
        _proc(6710, "cadvisor", "root", 6.5, 520, "/usr/bin/cadvisor -logtostderr", "cadvisor"),
        _proc(2210, "Xorg", "root", 0.2, 210, "/usr/lib/xorg/Xorg vt2 -displayfd 3", None),
    ]
    top_cpu = sorted(top_mem, key=lambda p: -p["cpu_percent"])
    return {
        "hostname": "atlas-lab",
        "kernel": "6.8.0-60-generic",
        "uptime_s": 14 * 86400 + 5 * 3600,
        "cpu": {
            "percent": 11.3,
            "iowait_percent": 0.4,
            "steal_percent": 0.0,
            "cores_logical": 16,
            "cores_physical": 8,
            "load": [1.38, 1.52, 1.47],
            "load_per_core": 0.09,
            "freq_mhz": 3900,
        },
        "memory": {
            "total": 64 * GIB,
            "available": int(41.5 * GIB),
            "used": int(21.8 * GIB),
            "percent": 35.2,
            "cached": int(19.5 * GIB),
            "swap_total": 8 * GIB,
            "swap_used": 0,
            "swap_percent": 0.0,
        },
        "filesystems": _filesystems(),
        "disks": [
            {"name": n, "read_bps": r, "write_bps": w, "busy_percent": b}
            for n, r, w, b in (
                ("nvme0n1", 0.0, 412_000.0, 0.8),
                ("nvme1n1", 120_000.0, 0.0, 0.3),
                ("sdb", 0.0, 0.0, 0.0),
                ("sdc", 0.0, 3_900_000.0, 4.6),
                ("sdd", 0.0, 0.0, 0.0),
            )
        ],
        "network": [
            {
                "name": "enp6s0",
                "rx_bps": 214_000.0,
                "tx_bps": 61_000.0,
                "rx_bytes": 9_841_223_004_112,
                "tx_bytes": 1_104_882_771_300,
                "errors": 0,
                "dropped": 0,
            }
        ],
        "temperatures": [
            {"sensor": "k10temp", "label": "Tctl", "current": 49.5, "high": None, "critical": None},
            {
                "sensor": "nvme",
                "label": "Composite",
                "current": 41.0,
                "high": 84.8,
                "critical": 89.8,
            },
            {
                "sensor": "nvme",
                "label": "Composite",
                "current": 38.0,
                "high": 84.8,
                "critical": 89.8,
            },
            {
                "sensor": "drivetemp",
                "label": "sdb",
                "current": 36.0,
                "high": 60.0,
                "critical": 70.0,
            },
        ],
        "processes": {
            "total": 468,
            "by_status": {"sleeping": 399, "idle": 66, "running": 2, "zombie": 1},
            "zombies": 1,
            "top_cpu": top_cpu,
            "top_memory": top_mem,
        },
    }


# ------------------------------------------------------------------------------ the rest


def _gpu() -> dict[str, Any]:
    parts = (_hex("g1", 8), _hex("g2", 4), _hex("g3", 4), _hex("g4", 4), _hex("g5", 12))
    return {
        "driver": "560.35.03",
        "gpus": [
            {
                "index": 0,
                "name": "NVIDIA GeForce RTX 4090",
                "uuid": "GPU-" + "-".join(parts),
                "util_percent": 6,
                "mem_util_percent": 3,
                "mem_used": int(15.1 * GIB),
                "mem_total": 24 * GIB,
                "mem_percent": 62.9,
                "temperature_c": 44,
                "fan_percent": 30,
                "power_w": 58.4,
                "power_limit_w": 450.0,
                "processes": [
                    {"pid": 3891, "name": "ollama", "mem_used": int(9.2 * GIB)},
                    {"pid": 4128, "name": "python3.11", "mem_used": int(4.7 * GIB)},
                    {"pid": 7351, "name": "llama-server", "mem_used": int(1.0 * GIB)},
                    {"pid": 2214, "name": "Xorg", "mem_used": 160 * MIB},
                ],
            }
        ],
    }


def _systemd() -> dict[str, Any]:
    return {
        "system_state": "running",
        "units_total": 812,
        "by_active_state": {"active": 598, "inactive": 214, "failed": 0},
        "failed": [],
        "restarting": [],
        "activating": [],
    }


def _journal() -> dict[str, Any]:
    return {
        "window_start": NOW - 120 - 60,
        "window_end": NOW - 120,
        "min_priority": "warning",
        "entries": 3,
        "truncated": False,
        "by_priority": {"warning": 3},
        "top_sources": [{"source": "kernel", "count": 3}],
        "top_messages": [
            {
                "source": "kernel",
                "priority": "warning",
                "count": 3,
                "sample": "kauditd_printk_skb: 41 callbacks suppressed",
                "first_ts": NOW - 170,
                "last_ts": NOW - 130,
            }
        ],
        "notable": {},
    }


def _apt() -> dict[str, Any]:
    pkgs = [
        ("containerd.io", "1.7.27-1", "1.7.28-1"),
        ("docker-buildx-plugin", "0.24.0-1", "0.25.0-1"),
        ("libssl3t64", "3.0.13-0ubuntu3.5", "3.0.13-0ubuntu3.6"),
        ("tzdata", "2025b-0ubuntu0.24.04.1", "2025c-0ubuntu0.24.04.1"),
    ]
    return {
        "distro": "Ubuntu 24.04.2 LTS",
        "upgradable_count": len(pkgs),
        "security_count": 0,
        "packages": [
            {
                "name": n,
                "current": c,
                "candidate": cand,
                "arch": "amd64",
                "suites": ["noble-updates"],
                "security": False,
            }
            for n, c, cand in pkgs
        ],
        "truncated": False,
        "lists_age_h": 6.2,
        "reboot_required": False,
        "reboot_required_pkgs": [],
    }


def _image_updates() -> dict[str, Any]:
    seen: dict[str, list[str]] = {}
    for name, image, *_ in _CONTAINERS:
        seen.setdefault(image, []).append(name)
    outdated = {"ghcr.io/ggml-org/llama.cpp:server-cuda", "postgres:17-alpine"}
    images = []
    for image, names in seen.items():
        status = "update_available" if image in outdated else "up_to_date"
        remote_seed = ("remote" if status != "up_to_date" else "local") + image
        images.append(
            {
                "image": image,
                "containers": names,
                "image_id": _hex("img" + image, 12),
                "status": status,
                "reason": None,
                "registry": image.split("/")[0] if image.count("/") > 1 else "docker.io",
                "local_digests": ["sha256:" + _hex("local" + image, 64)],
                "cached": False,
                "remote_digest": "sha256:" + _hex(remote_seed, 64),
            }
        )
    return {
        "summary": {
            "total": len(images),
            "up_to_date": len(images) - len(outdated),
            "update_available": len(outdated),
            "unknown": 0,
        },
        "updates_available": sorted(outdated),
        "images": images,
    }


def _queue_item(
    idx: int,
    title: str,
    *,
    size: int,
    left: int,
    indexer: str,
    problem: str | None = None,
    state: str = "downloading",
    added: str = "2026-05-30T08:12:41Z",
) -> dict[str, Any]:
    return {
        "id": 700_000 + idx,
        "download_id": _hex("dl" + title, 40).upper(),
        "title": title,
        "status": "warning" if problem else "downloading",
        "tracked_status": "warning" if problem else "ok",
        "tracked_state": state,
        "error": problem,
        "messages": [f"{title}: {problem}"] if problem else [],
        "size": size,
        "size_left": left,
        "protocol": "torrent",
        "indexer": indexer,
        "download_client": "qbittorrent",
        "added": added,
        "problem": bool(problem),
    }


def _arr_app(
    kind: str,
    version: str,
    missing: int,
    cutoff: int,
    items: list[dict[str, Any]],
    health: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    by_state: dict[str, int] = {}
    for it in items:
        by_state[it["tracked_state"]] = by_state.get(it["tracked_state"], 0) + 1
    return {
        "type": kind,
        "name": kind,
        "ok": True,
        "version": version,
        "health": health or [],
        "queue": {
            "total": len(items),
            "listed": len(items),
            "problems": sum(1 for i in items if i["problem"]),
            "by_state": by_state,
            "items": items,
        },
        "wanted": {"missing": missing, "cutoff_unmet": cutoff},
    }


def _indexers(failing: tuple[str, ...] = ()) -> dict[str, Any]:
    names = ["FictionTracker", "OpenLibraryTV", "NordTorrents", "PixelBay", "ArchiveSwarm"]
    rows = []
    for i, n in enumerate(names):
        bad = n in failing
        rows.append(
            {
                "id": 10 + i,
                "name": n,
                "protocol": "torrent",
                "enabled": True,
                "failing": bad,
                "disabled_till": "2026-06-02T06:14:00Z" if bad else None,
                "most_recent_failure": "2026-05-31T06:14:00Z" if bad else None,
            }
        )
    health = []
    if failing:
        msg = "Indexers unavailable due to failures for more than 6 hours: " + ", ".join(failing)
        health = [{"source": "IndexerLongTermStatusCheck", "level": "warning", "message": msg}]
    return {
        "type": "prowlarr",
        "name": "prowlarr",
        "ok": True,
        "version": "1.37.0.5076",
        "health": health,
        "indexers": rows,
        "summary": {"total": len(rows), "enabled": len(rows), "failing": len(failing)},
    }


def _qbit() -> dict[str, Any]:
    return {
        "type": "qbittorrent",
        "name": "qbittorrent",
        "ok": True,
        "version": "v5.1.2",
        "transfer": {
            "dl_speed": 8_400_000,
            "up_speed": 1_250_000,
            "connection_status": "connected",
        },
        "summary": {"total": 17, "by_state": {"uploading": 9, "stalledUP": 6, "downloading": 2}},
        "stalled_after_s": 21600,
        "stalled_total": 0,
        "stalled": [],
    }


def _arr(
    sonarr_items: list[dict[str, Any]] | None = None,
    failing: tuple[str, ...] = (),
) -> dict[str, Any]:
    radarr_items = [
        _queue_item(
            1,
            "Lighthouse.at.the.Edge.2024.1080p.WEB-DL",
            size=9_400_000_000,
            left=3_100_000_000,
            indexer="NordTorrents (Prowlarr)",
        ),
    ]
    if sonarr_items is None:
        sonarr_items = [
            _queue_item(
                2,
                "Harbor.Lights.S02E05.1080p.WEB-DL",
                size=2_100_000_000,
                left=400_000_000,
                indexer="OpenLibraryTV (Prowlarr)",
            ),
            _queue_item(
                3,
                "Harbor.Lights.S02E06.1080p.WEB-DL",
                size=2_200_000_000,
                left=2_200_000_000,
                indexer="OpenLibraryTV (Prowlarr)",
            ),
        ]
    whisparr_items = [
        _queue_item(
            4,
            "Studio.Aurora.24.05.28.Night.Shift.1080p",
            size=1_400_000_000,
            left=600_000_000,
            indexer="PixelBay (Prowlarr)",
        ),
    ]
    apps = [
        _arr_app("radarr", "5.26.2.10099", 31, 58, radarr_items),
        _arr_app("sonarr", "4.0.15.2941", 112, 207, sonarr_items),
        _arr_app("whisparr", "3.0.2.1024", 18, 40, whisparr_items),
    ]
    prowlarr = _indexers(failing)
    findings: list[dict[str, Any]] = []
    for app in apps:
        if app["queue"]["problems"]:
            findings.append(
                {
                    "severity": "warn",
                    "source": f"{app['type']}/{app['name']}",
                    "message": f"{app['queue']['problems']} queue item(s) with problems",
                }
            )
    for h in prowlarr["health"]:
        findings.append(
            {
                "severity": "warn",
                "source": "prowlarr/prowlarr",
                "message": "health: " + h["message"],
            }
        )
    return {
        "summary": {
            "instances": 5,
            "unreachable": [],
            "queue_total": sum(a["queue"]["total"] for a in apps),
            "queue_problems": sum(a["queue"]["problems"] for a in apps),
            "health_issues": len(prowlarr["health"]),
            "wanted_missing": sum(a["wanted"]["missing"] for a in apps),
            "wanted_cutoff_unmet": sum(a["wanted"]["cutoff_unmet"] for a in apps),
            "indexers_failing": len(failing),
            "stalled_candidates": 0,
        },
        "findings": findings,
        "apps": apps,
        "prowlarr": prowlarr,
        "qbittorrent": _qbit(),
    }


def _prometheus() -> dict[str, Any]:
    return {"url": "http://prometheus:9090", "summary": {"queries": 0, "failed": []}, "queries": []}


def base() -> dict[str, Any]:
    return {
        "name": "base",
        "now": NOW,
        "description": "A healthy home AI/media server (invented): 33 containers, RTX 4090, "
        "five data disks, Radarr/Sonarr/Whisparr/Prowlarr/qBittorrent all fine.",
        "collectors": {
            "system": _entry("system", _system(), 4, 0.06),
            "docker": _entry("docker", _docker(), 8, 0.21),
            "gpu": _entry("gpu", _gpu(), 7, 0.01),
            "systemd": _entry("systemd", _systemd(), 6, 0.01),
            "journal": _entry("journal", _journal(), 5, 0.01),
            "apt": _entry("apt", _apt(), 22_300, 2.1),
            "arr": _entry("arr", _arr(), 95, 0.12),
            "image_updates": _entry("image_updates", _image_updates(), 22_300, 5.2),
            "prometheus": _entry("prometheus", _prometheus(), 8, 0.0),
        },
        "history": {},
    }


# ------------------------------------------------------------------------------ variants


def _data(snap: dict[str, Any], name: str) -> dict[str, Any]:
    return snap["collectors"][name]["data"]


def _container_of(snap: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in _data(snap, "docker")["containers"] if c["name"] == name)


def _fs_of(snap: dict[str, Any], mount: str) -> dict[str, Any]:
    return next(f for f in _data(snap, "system")["filesystems"] if f["mount"] == mount)


def _set_journal(snap: dict[str, Any], **changes: Any) -> None:
    snap["collectors"]["journal"] = _entry("journal", {**_journal(), **changes}, 5, 0.01)


def disk_filling() -> dict[str, Any]:
    """/mnt/disks/downloads at 91 % and growing ~85 GB/day for a week: full in ~4 days."""
    snap = base()
    total = 4 * TB - 140 * 10**9
    now_used = int(total * 0.91)
    rate = 85 * 10**9
    fs = _fs_of(snap, "/mnt/disks/downloads")
    fs.update(used=now_used, avail=total - now_used, percent=91.0)
    assert rate * 4 < total - now_used < rate * 5, "ETA should be ~4 days"
    samples: list[dict[str, Any]] = []
    snap["history"]["system"] = samples
    for age_h in range(7 * 24, -1, -4):
        used = int(now_used - rate * age_h / 24)
        sample_fs = [
            {
                **fs,
                "used": used,
                "avail": total - used,
                "percent": round(used / total * 100, 1),
            }
        ]
        hours = age_h if age_h else 1 / 60
        samples.append({"hours_ago": hours, "data": {"filesystems": sample_fs}})
    return snap


def failed_unit_restart_loop() -> dict[str, Any]:
    """logrotate.service failed + `esphome` container restart loop (14 restarts in 24 h)."""
    snap = base()
    sd = _data(snap, "systemd")
    sd["system_state"] = "degraded"
    sd["by_active_state"] = {"active": 597, "inactive": 214, "failed": 1}
    sd["failed"] = [
        {
            "name": "logrotate.service",
            "description": "Rotate log files",
            "sub": "failed",
            "since": int(NOW - 5 * 3600),
            "result": "exit-code",
        }
    ]
    docker = _data(snap, "docker")
    c = _container_of(snap, "esphome")
    c.update(
        state="restarting",
        status="Restarting (1) 18 seconds ago",
        restart_count=17,
        restarts_delta=14,
        restarted_since_last=True,
        exit_code=1,
        log_errors=6,
        log_error_samples=["ERROR Could not open configuration file '/config/garage-sensor.yaml'"],
    )
    docker["summary"]["running"] -= 1
    docker["summary"]["restarting"] = ["esphome"]
    docker["summary"]["restarted_recently"] = ["esphome"]
    snap["history"]["docker"] = [
        {"hours_ago": 23.9, "data": {"containers": [{"name": "esphome", "restart_count": 3}]}},
        {"hours_ago": 1, "data": {"containers": [{"name": "esphome", "restart_count": 15}]}},
    ]
    _set_journal(
        snap,
        entries=21,
        by_priority={"warning": 3, "err": 18},
        top_sources=[{"source": "logrotate", "count": 6}, {"source": "dockerd", "count": 12}],
        top_messages=[
            {
                "source": "logrotate",
                "priority": "err",
                "count": 6,
                "sample": "error: /etc/logrotate.d/loki:7 unknown option 'maxsizee' -- "
                "ignoring line",
                "first_ts": NOW - 5 * 3600,
                "last_ts": NOW - 5 * 3600 + 3,
            },
            {
                "source": "dockerd",
                "priority": "err",
                "count": 12,
                "sample": "container esphome exited with code 1, restarting",
                "first_ts": NOW - 400,
                "last_ts": NOW - 40,
            },
        ],
    )
    return snap


def oom_pressure() -> dict[str, Any]:
    """comfyui OOM-killed, memory 96 % used, swap 6.4 of 8 GiB used."""
    snap = base()
    system = _data(snap, "system")
    system["memory"].update(
        available=int(2.6 * GIB),
        used=int(60.9 * GIB),
        percent=95.9,
        cached=int(1.1 * GIB),
        swap_used=int(6.4 * GIB),
        swap_percent=80.0,
    )
    system["cpu"].update(iowait_percent=18.5, load=[7.9, 6.1, 4.2], load_per_core=0.49)
    c = _container_of(snap, "comfyui")
    c.update(
        state="exited",
        status="Exited (137) 3 minutes ago",
        restart_count=8,
        restarts_delta=1,
        restarted_since_last=True,
        oom_killed=True,
        exit_code=137,
        mem_usage=0,
        mem_percent=0.0,
        cpu_percent=0.0,
        pids=0,
        log_errors=2,
        log_error_samples=["Killed"],
    )
    docker = _data(snap, "docker")
    docker["summary"]["running"] -= 1
    docker["summary"]["exited_with_error"] = ["comfyui"]
    docker["summary"]["oom_killed"] = ["comfyui"]
    _container_of(snap, "ollama").update(mem_usage=int(31 * GIB), mem_percent=48.4)
    oom = "Out of memory: Killed process 4120 (python3.11) total-vm:41230112kB, anon-rss:14102344kB"
    _set_journal(
        snap,
        entries=9,
        by_priority={"warning": 3, "err": 6},
        top_sources=[{"source": "kernel", "count": 9}],
        top_messages=[
            {
                "source": "kernel",
                "priority": "err",
                "count": 2,
                "sample": oom,
                "first_ts": NOW - 190,
                "last_ts": NOW - 188,
            },
            {
                "source": "kernel",
                "priority": "err",
                "count": 4,
                "sample": "oom_reaper: reaped process 4120 (python3.11), now anon-rss:0kB",
                "first_ts": NOW - 187,
                "last_ts": NOW - 180,
            },
        ],
        notable={"oom": {"count": 2, "samples": [oom]}},
    )
    return snap


def five_problems() -> dict[str, Any]:
    """Five problems at once: full models disk, failed backup, sick open-webui, hot GPU, updates."""
    snap = base()
    total = 2 * TB - 70 * 10**9
    used = int(total * 0.962)
    _fs_of(snap, "/mnt/disks/models").update(used=used, avail=total - used, percent=96.2)
    sd = _data(snap, "systemd")
    sd["system_state"] = "degraded"
    sd["by_active_state"] = {"active": 597, "inactive": 214, "failed": 1}
    sd["failed"] = [
        {
            "name": "backup-sync.service",
            "description": "Nightly sync to the backup disk",
            "sub": "failed",
            "since": int(NOW - 9 * 3600),
            "result": "exit-code",
        }
    ]
    _container_of(snap, "open-webui").update(
        health="unhealthy",
        status="Up 13 days (unhealthy)",
        log_errors=31,
        log_error_samples=["sqlalchemy.exc.OperationalError: connection to server refused"],
    )
    _data(snap, "docker")["summary"]["unhealthy"] = ["open-webui"]
    _data(snap, "gpu")["gpus"][0].update(
        temperature_c=86, fan_percent=100, util_percent=99, power_w=441.0
    )
    apt = _data(snap, "apt")
    for p in apt["packages"][:3]:
        p["security"] = True
        p["suites"] = ["noble-security"]
    apt["security_count"] = 3
    _set_journal(
        snap,
        entries=14,
        by_priority={"warning": 11, "err": 3},
        top_sources=[{"source": "kernel", "count": 8}, {"source": "backup-sync", "count": 3}],
        top_messages=[
            {
                "source": "backup-sync",
                "priority": "err",
                "count": 3,
                "sample": "rsync: write failed on '/mnt/disks/backup/models/ckpt.safetensors': "
                "No space left on device",
                "first_ts": NOW - 9 * 3600,
                "last_ts": NOW - 9 * 3600 + 5,
            },
            {
                "source": "kernel",
                "priority": "warning",
                "count": 8,
                "sample": "kauditd_printk_skb: 41 callbacks suppressed",
                "first_ts": NOW - 170,
                "last_ts": NOW - 40,
            },
        ],
    )
    return snap


def arr_stuck_queue() -> dict[str, Any]:
    """Sonarr: 6 stalled queue items; Prowlarr: one indexer failing. Host itself healthy."""
    titles = [
        ("Northern.Tides.S01E01.2160p.WEB-DL", 6_100_000_000),
        ("Northern.Tides.S01E02.2160p.WEB-DL", 6_000_000_000),
        ("Northern.Tides.S01E03.2160p.WEB-DL", 6_300_000_000),
        ("Glass.Orchard.S03E08.1080p.WEB-DL", 2_400_000_000),
        ("Glass.Orchard.S03E09.1080p.WEB-DL", 2_500_000_000),
        ("Quiet.Frequency.S01E04.1080p.WEB-DL", 2_200_000_000),
    ]
    stuck = [
        _queue_item(
            10 + i,
            t,
            size=s,
            left=s,
            indexer="FictionTracker (Prowlarr)",
            problem="The download is stalled with no connections",
            added="2026-05-27T19:00:00Z",
        )
        for i, (t, s) in enumerate(titles)
    ]
    ok = _queue_item(
        2,
        "Harbor.Lights.S02E05.1080p.WEB-DL",
        size=2_100_000_000,
        left=400_000_000,
        indexer="OpenLibraryTV (Prowlarr)",
    )
    snap = base()
    snap["collectors"]["arr"] = _entry(
        "arr", _arr([*stuck, ok], failing=("FictionTracker",)), 95, 0.12
    )
    return snap


VARIANTS = {
    "base": base,
    "base-disk-filling": disk_filling,
    "base-restart-loop": failed_unit_restart_loop,
    "base-oom": oom_pressure,
    "base-five-problems": five_problems,
    "base-arr-stuck": arr_stuck_queue,
}


def main() -> None:
    for name, fn in VARIANTS.items():
        snap = fn()
        snap["name"] = name
        if name != "base":
            snap["description"] = (fn.__doc__ or "").strip().splitlines()[0]
        text = json.dumps(snap, indent=1, ensure_ascii=False) + "\n"
        (OUT / f"{name}.json").write_text(text, encoding="utf-8", newline="\n")
        print(name, len(text))


if __name__ == "__main__":
    main()
