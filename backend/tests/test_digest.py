from __future__ import annotations

import json
from typing import Any

from ai_server_monitor.config import ReportSettings
from ai_server_monitor.db import Database
from ai_server_monitor.digest import WindowObserver, build_digest, fingerprint
from ai_server_monitor.digest.digester import Section, fit_budget
from ai_server_monitor.digest.models import Flag, score_from_flags, worst_severity
from ai_server_monitor.digest.trends import disk_trend_from_points
from ai_server_monitor.llm.text import estimate_tokens

NOW = 1_800_000_000.0
DAY = 86400.0
GB = 1e9


def entry(data: dict[str, Any], *, ok: bool = True, ts: float = NOW) -> dict[str, Any]:
    return {
        "state": "active",
        "reason": None,
        "ok": ok,
        "error": None if ok else "boom",
        "last_ts": ts,
        "result": {"ts": ts, "ok": ok, "data": data if ok else {}, "error": None},
    }


def healthy_system(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "hostname": "box",
        "kernel": "6.8",
        "uptime_s": 90_000,
        "cpu": {"percent": 10.0, "cores_logical": 8, "load": [0.5, 0.6, 0.7]},
        "memory": {
            "total": 32 * GB,
            "available": 24 * GB,
            "used": 8 * GB,
            "percent": 25.0,
            "swap_total": 4 * GB,
            "swap_used": 0,
            "swap_percent": 0.0,
        },
        "filesystems": [
            {
                "mount": "/",
                "fstype": "ext4",
                "total": 500 * GB,
                "used": 100 * GB,
                "avail": 400 * GB,
                "percent": 20.0,
                "inodes_percent": 5.0,
            }
        ],
        "disks": [],
        "network": [],
        "temperatures": [],
        "processes": {"total": 200, "zombies": 0, "top_cpu": [], "top_memory": []},
    }
    data.update(over)
    return data


def snapshot(**collectors: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snap = {"system": entry(healthy_system())}
    snap.update(collectors)
    return snap


async def digest_of(db: Database, snap: dict[str, Any], **cfg: Any):  # type: ignore[no-untyped-def]
    return await build_digest(snap, db, ReportSettings(**cfg), now=NOW)


def titles(digest) -> list[tuple[str, str]]:  # type: ignore[no-untyped-def]
    return [(f.severity, f.title) for f in digest.flags]


# --------------------------------------------------------------------------- rules


async def test_healthy_server_has_no_flags(db: Database) -> None:
    d = await digest_of(db, snapshot())
    assert d.flags == []
    assert "none — all checks passed" in d.text
    assert worst_severity(d.flags) == "ok" and score_from_flags(d.flags) == 100


async def test_threshold_rules(db: Database) -> None:
    system = healthy_system(
        cpu={"percent": 99, "cores_logical": 4, "load": [20, 14.0, 9]},
        memory={
            "total": 32 * GB,
            "available": 0.5 * GB,
            "used": 31 * GB,
            "percent": 98.4,
            "swap_total": 4 * GB,
            "swap_used": 3 * GB,
            "swap_percent": 75.0,
        },
        filesystems=[
            {"mount": "/", "total": 100 * GB, "used": 96 * GB, "avail": 4 * GB, "percent": 96.0},
            {"mount": "/data", "total": 100 * GB, "used": 87 * GB, "avail": 13 * GB, "percent": 87},
            {
                "mount": "/inodes",
                "total": GB,
                "used": 1,
                "avail": GB,
                "percent": 1,
                "inodes_percent": 99,
            },
        ],
    )
    d = await digest_of(db, snapshot(system=entry(system)))
    found = titles(d)
    assert ("crit", "Disk / almost full") in found
    assert ("warn", "Disk /data filling up") in found
    assert ("crit", "Disk /inodes out of inodes") in found
    assert ("crit", "Memory almost exhausted") in found
    assert ("warn", "Heavy swap use") in found
    assert ("crit", "System overloaded") in found  # 14 / 4 cores = 3.5 per core
    assert d.flags[0].severity == "crit"  # worst first
    assert worst_severity(d.flags) == "crit" and score_from_flags(d.flags) < 50


async def test_thresholds_come_from_settings(db: Database) -> None:
    snap = snapshot()  # disk at 20%
    assert not (await digest_of(db, snap)).flags
    d = await digest_of(db, snap, disk_warn_percent=10, disk_crit_percent=15)
    assert titles(d) == [("crit", "Disk / almost full")]


async def test_services_containers_and_updates(db: Database) -> None:
    snap = snapshot(
        systemd=entry(
            {
                "system_state": "degraded",
                "units_total": 100,
                "failed": [{"name": "a.service", "result": "exit-code", "since": NOW - 3600}],
                "restarting": [{"name": "b.service", "n_restarts": 9, "delta": 2}],
            }
        ),
        docker=entry(
            {
                "summary": {
                    "running": 2,
                    "total": 4,
                    "unhealthy": ["web"],
                    "oom_killed": ["db"],
                    "exited_with_error": ["old"],
                    "restarted_recently": [],
                },
                "containers": [
                    {"name": "web", "state": "running", "health": "unhealthy", "cpu_percent": 2},
                    {"name": "db", "state": "running", "oom_killed": True, "cpu_percent": 1},
                    {"name": "loop", "state": "restarting", "restarts_delta": 4},
                    {"name": "old", "state": "exited", "exit_code": 1, "status": "Exited (1)"},
                    {
                        "name": "noisy",
                        "state": "running",
                        "log_errors": 50,
                        "log_error_samples": ["x"],
                    },
                ],
            }
        ),
        apt=entry({"upgradable_count": 3, "security_count": 2, "reboot_required": True}),
    )
    found = titles(await digest_of(db, snap))
    assert ("warn", "Unit a.service failed") in found
    assert ("warn", "Service b.service keeps restarting") in found
    assert ("warn", "Container web is unhealthy") in found
    assert ("crit", "Container db was OOM-killed") in found
    assert ("crit", "Container loop is restart-looping") in found
    assert ("info", "Container old exited with an error") in found
    assert ("warn", "Container noisy logging many errors") in found
    assert ("warn", "2 security updates pending") in found
    assert ("info", "Reboot required") in found


async def test_journal_bursts_and_kernel_events(db: Database) -> None:
    window = {
        "window_start": NOW - 60,
        "window_end": NOW,
        "entries": 40,
        "by_priority": {"err": 30, "warning": 10},
        "top_messages": [
            {"source": "app", "priority": "err", "count": 30, "sample": "connection refused"}
        ],
        "notable": {"oom": {"count": 1, "samples": ["kernel: Out of memory: Killed process 5"]}},
    }
    d = await digest_of(db, snapshot(journal=entry(window)))
    found = titles(d)
    assert ("crit", "Kernel/system event: oom (1x)") in found
    assert any(t.startswith("Journal error burst: 30 errors") for _, t in found)
    assert ("warn", "Repeated log message x30 from app") in found
    assert "System journal" in d.text and "connection refused" in d.text


async def test_collector_failure_and_unavailable(db: Database) -> None:
    snap = snapshot(
        docker=entry({}, ok=False),
        gpu={"state": "unavailable", "reason": "no NVIDIA driver", "ok": None, "result": None},
        apt={"state": "disabled", "reason": None, "ok": None, "result": None},
    )
    d = await digest_of(db, snap)
    found = titles(d)
    assert ("warn", "Collector docker is failing") in found
    assert ("info", "Collector gpu unavailable") in found
    assert not any("apt" in t for _, t in found)  # disabled = deliberate, not a flag
    assert "docker: FAILING (boom)" in d.text and "gpu: not available" in d.text


async def test_odd_collector_data_never_raises(db: Database) -> None:
    snap = {
        "system": entry({"cpu": None, "memory": {"percent": "x"}, "filesystems": [None, {}, 5]}),
        "docker": entry({"containers": [{"name": None}, "str"], "summary": None}),
        "systemd": entry({"failed": [None], "restarting": "no"}),
        "image_updates": entry({"updates": [{"image": "nginx"}, "redis", 7]}),
        "apt": entry({"packages": "oops", "upgradable_count": None}),
    }
    d = await digest_of(db, snap)  # must not raise
    assert "container images with updates" in d.text


async def test_image_updates_read_defensively(db: Database) -> None:
    snap = snapshot(
        image_updates=entry(
            {
                "images": [
                    {"name": "a", "update_available": True},
                    {"name": "b", "update_available": False},
                ]
            }
        )
    )
    d = await digest_of(db, snap)
    assert "container images with updates: 1 — a" in d.text


# --------------------------------------------------------------------------- trends


async def _put_samples(
    db: Database, collector: str, rows: list[tuple[float, dict[str, Any]]]
) -> None:
    await db.executemany(
        "INSERT INTO collector_samples (ts, collector, ok, data) VALUES (?, ?, 1, ?)",
        [(ts, collector, json.dumps(data)) for ts, data in rows],
    )


async def test_disk_growth_trend_predicts_full_disk(db: Database) -> None:
    # 100 GB/day growth over 3 days on a 500 GB disk with 100 GB left -> full in ~1 day.
    rows = []
    for hour in range(0, 72):
        ts = NOW - (72 - hour) * 3600
        used = 100 * GB + (hour / 24) * 100 * GB
        rows.append((ts, {"filesystems": [{"mount": "/", "used": used}]}))
    await _put_samples(db, "system", rows)
    system = healthy_system(
        filesystems=[
            {
                "mount": "/",
                "fstype": "ext4",
                "total": 500 * GB,
                "used": 400 * GB,
                "avail": 100 * GB,
                "percent": 80.0,
            },
        ]
    )
    d = await digest_of(db, snapshot(system=entry(system)))
    assert ("crit", "Disk / will be full in ~1 days") in titles(d)
    assert "trend +" in d.text and "full in ~1d" in d.text


def test_flat_or_short_history_gives_no_eta() -> None:
    flat = [(NOW - i * 3600, 100 * GB) for i in range(30, 0, -1)]
    trend = disk_trend_from_points("/", flat, 50 * GB, NOW)
    assert trend is not None and trend.eta_days is None
    assert disk_trend_from_points("/", flat[:2], 50 * GB, NOW) is None  # too few points
    short = [(NOW - i * 60, i * GB) for i in range(10, 0, -1)]
    assert disk_trend_from_points("/", short, GB, NOW) is None  # spans minutes only


async def test_restart_counts_from_history(db: Database) -> None:
    await _put_samples(
        db,
        "docker",
        [(NOW - 20 * 3600, {"containers": [{"name": "web", "restart_count": 2},
                                           {"name": "ok", "restart_count": 5}]})],
    )  # fmt: skip
    docker = entry(
        {
            "summary": {},
            "containers": [
                {"name": "web", "state": "running", "restart_count": 7},
                {"name": "ok", "state": "running", "restart_count": 5},
            ],
        }
    )
    d = await digest_of(db, snapshot(docker=docker))
    assert ("warn", "Container web restarted 5x in 24h") in titles(d)
    assert "Restarts in 24h: web x5" in d.text


# --------------------------------------------------------------------------- budget / hash


def big_snapshot() -> dict[str, dict[str, Any]]:
    containers = [
        {"name": f"c{i}", "state": "running", "cpu_percent": 50 - i, "mem_usage": i * 1e8}
        for i in range(30)
    ]
    packages = [{"name": f"pkg{i}", "security": False} for i in range(30)]
    return snapshot(
        docker=entry({"summary": {"running": 30, "total": 30}, "containers": containers}),
        apt=entry({"upgradable_count": 30, "security_count": 0, "packages": packages}),
        systemd=entry(
            {
                "system_state": "degraded",
                "failed": [{"name": f"u{i}.service", "result": "exit-code"} for i in range(8)],
            }
        ),
    )


def test_budget_shortens_then_drops_lowest_priority_first() -> None:
    def sec(key: str, priority: int, n: int) -> Section:
        return Section(
            key, key.title(), priority, [f"{key} line {i} " + "x" * 60 for i in range(n)]
        )

    sections = [
        sec("flags", 100, 6),
        sec("disks", 80, 10),
        sec("gpu", 55, 10),
        sec("updates", 40, 10),
    ]
    everything, _ = fit_budget(sections, "# header", 100_000)
    text, kept = fit_budget(sections, "# header", 250)
    assert estimate_tokens(text) <= 250
    assert kept["flags"] == 6  # never cut
    assert kept["updates"] < kept["gpu"] or kept["updates"] == 0
    assert ("(+" in text and "more lines omitted" in text) or kept["updates"] == 0
    assert len(text) < len(everything)
    tiny, kept = fit_budget(sections, "# header", 100)
    assert kept["updates"] == 0 and kept["gpu"] == 0 and kept["flags"] == 6
    assert "## Flags" in tiny and "## Updates" not in tiny and tiny.startswith("# header")


async def test_digest_stays_within_budget(db: Database) -> None:
    d = await digest_of(db, big_snapshot(), digest_token_budget=500)
    assert d.tokens <= 500
    assert "SERVER DIGEST" in d.text and "Rule-based flags" in d.text


async def test_fingerprint_ignores_small_fluctuations(db: Database) -> None:
    a = await digest_of(db, snapshot(system=entry(healthy_system(uptime_s=90_000))))
    wobble = healthy_system(
        uptime_s=95_000, cpu={"percent": 12.0, "cores_logical": 8, "load": [0.6, 0.6, 0.7]}
    )
    b = await digest_of(db, snapshot(system=entry(wobble)))
    assert a.hash == b.hash
    flagged = healthy_system(
        filesystems=[
            {"mount": "/", "total": 500 * GB, "used": 480 * GB, "avail": 20 * GB, "percent": 96.0}
        ]
    )
    c = await digest_of(db, snapshot(system=entry(flagged)))
    assert c.hash != a.hash
    assert fingerprint("a 12 b") == fingerprint("a 13 b")  # 1 significant digit


def test_flag_dict_shape() -> None:
    assert Flag("warn", "t", "d").to_dict() == {"severity": "warn", "title": "t", "detail": "d"}


# --------------------------------------------------------------------------- observer


async def test_window_observer_sums_windows_since_cutoff(db: Database) -> None:
    obs = WindowObserver()

    def win(end: float, errs: int) -> dict[str, Any]:
        return {"window_start": end - 60, "window_end": end, "entries": errs,
                "by_priority": {"err": errs}, "top_messages": [], "notable": {}}  # fmt: skip

    for i, errs in enumerate([5, 7, 9]):
        obs.observe({"journal": entry(win(NOW - 240 + i * 60, errs))})
    obs.observe({"journal": entry(win(NOW - 120, 5))})  # same window again: ignored
    total = obs.summary(since=NOW - 1000)
    assert total is not None and total.errors == 21 and total.windows == 3
    recent = obs.summary(since=NOW - 150)
    assert recent is not None and recent.errors == 9
    assert obs.summary(since=NOW) is None

    d = await build_digest(
        snapshot(), db, ReportSettings(log_error_burst=20), observer=obs, now=NOW
    )
    assert any(f.title.startswith("Journal error burst") for f in d.flags)
