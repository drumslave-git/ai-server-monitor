"""Deterministic anomaly rules: snapshot + trends -> list of Flag.

Every rule is a small function that never raises on odd data (collector shapes vary and
one of them comes from another module); `evaluate` also guards each rule so one bad rule
cannot take the digest down.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import ReportSettings
from .models import SEVERITY_RANK, Flag, fmt_bytes, fmt_pct, num
from .snapshot import Snapshot
from .trends import JournalSummary, Trends

log = logging.getLogger(__name__)

MAX_PER_RULE = 5
"""Flags of one kind beyond this are folded into a single '+N more' flag."""


@dataclass
class Context:
    snap: Snapshot
    cfg: ReportSettings
    trends: Trends
    journal: JournalSummary | None
    journal_label: str = "this period"
    pending_approvals: int = 0


Rule = Callable[[Context], list[Flag]]


def _fold(flags: list[Flag], what: str) -> list[Flag]:
    """Keep the worst MAX_PER_RULE flags, summarize the rest."""
    if len(flags) <= MAX_PER_RULE:
        return flags
    flags = sorted(flags, key=lambda f: -SEVERITY_RANK[f.severity])
    rest = flags[MAX_PER_RULE:]
    worst = max(rest, key=lambda f: SEVERITY_RANK[f.severity]).severity
    return [*flags[:MAX_PER_RULE], Flag(worst, f"{len(rest)} more {what}")]


# --------------------------------------------------------------------------- rules


def collector_failures(c: Context) -> list[Flag]:
    flags: list[Flag] = []
    for name, entry in c.snap.raw.items():
        state = entry.get("state")
        if state == "disabled":
            continue
        if state == "unavailable":
            flags.append(
                Flag("info", f"Collector {name} unavailable", str(entry.get("reason") or ""))
            )
        elif entry.get("ok") is False:
            flags.append(
                Flag(
                    "warn",
                    f"Collector {name} is failing",
                    str(entry.get("error") or "unknown error"),
                )
            )
    return flags


def disks(c: Context) -> list[Flag]:
    flags: list[Flag] = []
    for fs in c.snap.data("system").get("filesystems") or []:
        mount = fs.get("mount", "?")
        pct = fs.get("percent")
        if pct is not None:
            detail = f"{fmt_bytes(fs.get('avail'))} free of {fmt_bytes(fs.get('total'))}"
            if num(pct) >= c.cfg.disk_crit_percent:
                flags.append(Flag("crit", f"Disk {mount} {num(pct):.0f}% full", detail))
            elif num(pct) >= c.cfg.disk_warn_percent:
                flags.append(Flag("warn", f"Disk {mount} {num(pct):.0f}% full", detail))
        inodes = fs.get("inodes_percent")
        if inodes is not None and num(inodes) >= c.cfg.disk_warn_percent:
            sev = "crit" if num(inodes) >= c.cfg.disk_crit_percent else "warn"
            flags.append(
                Flag(sev, f"Disk {mount} out of inodes", f"{fmt_pct(num(inodes))} inodes used")
            )
    return _fold(flags, "disk problems")


def disk_filling(c: Context) -> list[Flag]:
    flags: list[Flag] = []
    for mount, trend in c.trends.disks.items():
        if trend.eta_days is None:
            continue
        if trend.eta_days <= c.cfg.disk_full_crit_days:
            sev = "crit"
        elif trend.eta_days <= c.cfg.disk_full_warn_days:
            sev = "warn"
        else:
            continue
        flags.append(
            Flag(
                sev,
                f"Disk {mount} will be full in ~{trend.eta_days:.0f} days",
                f"growing {fmt_bytes(trend.growth_per_day)}/day "
                f"over the last {trend.span_days:.1f} days",
            )
        )
    return _fold(flags, "disks filling")


def memory(c: Context) -> list[Flag]:
    mem = c.snap.data("system").get("memory") or {}
    flags: list[Flag] = []
    pct = mem.get("percent")
    if pct is not None:
        detail = (
            f"{fmt_pct(num(pct))} used, {fmt_bytes(mem.get('available'))} available "
            f"of {fmt_bytes(mem.get('total'))}"
        )
        if num(pct) >= c.cfg.memory_crit_percent:
            flags.append(Flag("crit", "Memory almost exhausted", detail))
        elif num(pct) >= c.cfg.memory_warn_percent:
            flags.append(Flag("warn", "High memory use", detail))
    swap = mem.get("swap_percent")
    if swap is not None and num(mem.get("swap_total")) > 0 and num(swap) >= c.cfg.swap_warn_percent:
        flags.append(
            Flag(
                "warn",
                "Heavy swap use",
                f"{fmt_pct(num(swap))} of {fmt_bytes(mem.get('swap_total'))} swap used",
            )
        )
    return flags


def load(c: Context) -> list[Flag]:
    cpu = c.snap.data("system").get("cpu") or {}
    cores = num(cpu.get("cores_logical"), 0)
    loads = cpu.get("load") or []
    if cores <= 0 or len(loads) < 2:
        return []
    per_core = num(loads[1]) / cores
    detail = f"5-min load {num(loads[1]):.1f} on {cores:.0f} cores ({per_core:.2f}/core)"
    if per_core >= c.cfg.load_crit_per_core:
        return [Flag("crit", "System overloaded", detail)]
    if per_core >= c.cfg.load_warn_per_core:
        return [Flag("warn", "High system load", detail)]
    return []


def zombies(c: Context) -> list[Flag]:
    n = int(num((c.snap.data("system").get("processes") or {}).get("zombies")))
    if n >= 20:
        return [Flag("info", f"{n} zombie processes", "parents are not reaping their children")]
    return []


def temperatures(c: Context) -> list[Flag]:
    flags: list[Flag] = []
    for t in c.snap.data("system").get("temperatures") or []:
        high, cur = t.get("high"), t.get("current")
        if cur is None:
            continue
        crit = t.get("critical")
        if crit is not None and num(cur) >= num(crit):
            flags.append(
                Flag(
                    "crit",
                    f"{t.get('label') or t.get('sensor')} at critical temperature",
                    f"{num(cur):.0f}°C",
                )
            )
        elif high is not None and num(cur) >= num(high):
            flags.append(
                Flag(
                    "warn",
                    f"{t.get('label') or t.get('sensor')} running hot",
                    f"{num(cur):.0f}°C (high mark {num(high):.0f}°C)",
                )
            )
    return _fold(flags, "temperature problems")


def gpu(c: Context) -> list[Flag]:
    flags: list[Flag] = []
    for g in c.snap.data("gpu").get("gpus") or []:
        label = f"GPU {g.get('index', 0)} ({g.get('name', '?')})"
        temp = g.get("temperature_c")
        if temp is not None and num(temp) >= c.cfg.gpu_temp_warn_c:
            flags.append(Flag("warn", f"{label} is hot", f"{num(temp):.0f}°C"))
        mem = g.get("mem_percent")
        if mem is not None and num(mem) >= 95:
            flags.append(Flag("warn", f"{label} VRAM nearly full", f"{fmt_pct(num(mem))} used"))
    return flags


def restart_loops(c: Context) -> list[Flag]:
    flags: list[Flag] = []
    threshold = c.cfg.restart_loop_count
    containers = c.snap.data("docker").get("containers") or []
    for ct in containers:
        name = ct.get("name", "?")
        today = max(c.trends.docker_restarts_24h.get(name, 0), int(num(ct.get("restarts_delta"))))
        if ct.get("state") == "restarting":
            flags.append(
                Flag(
                    "crit",
                    f"Container {name} is restart-looping",
                    f"state restarting, restarted {today}x in 24h",
                )
            )
        elif today >= threshold:
            flags.append(
                Flag(
                    "warn",
                    f"Container {name} restarted {today}x in 24h",
                    f"exit code {ct.get('exit_code')}" if ct.get("exit_code") else "",
                )
            )
    for unit in c.snap.data("systemd").get("restarting") or []:
        name = unit.get("name", "?")
        today = c.trends.unit_restarts_24h.get(name, 0)
        if today >= threshold or (
            int(num(unit.get("delta"))) > 0 and int(num(unit.get("n_restarts"))) >= threshold
        ):
            flags.append(
                Flag(
                    "warn",
                    f"Service {name} keeps restarting",
                    f"{int(num(unit.get('n_restarts')))} restarts total, {today} in 24h",
                )
            )
    return _fold(flags, "restart loops")


def failed_units(c: Context) -> list[Flag]:
    sd = c.snap.data("systemd")
    flags = [
        Flag(
            "warn",
            f"Unit {u.get('name', '?')} failed",
            f"result: {u.get('result') or u.get('sub') or '?'}",
        )
        for u in sd.get("failed") or []
    ]
    return _fold(flags, "failed units")


NORMAL_EXIT_CODES = (0, 130, 143)
"""Clean exit, SIGINT and SIGTERM: someone stopped the container, nothing failed."""

_AGO_UNITS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "week": 7 * 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}
_AGO = re.compile(
    r"(\d+|an?|about an?|less than an?)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.I
)


def exited_ago_s(status: str | None) -> float | None:
    """Seconds since a container exited, from Docker's status text ('Exited (1) 3 days ago')."""
    match = _AGO.search(status or "")
    if not match:
        return None
    count = match.group(1).lower()
    n = int(count) if count.isdigit() else 1
    return n * _AGO_UNITS[match.group(2).lower()]


def recent_error_exits(containers: list[dict[str, Any]], window_s: float) -> list[dict[str, Any]]:
    """Exited containers worth mentioning: a real error code (not a normal stop) within the
    trend window. Old failures of long-stopped containers are not news."""
    out: list[dict[str, Any]] = []
    for ct in containers:
        if ct.get("state") != "exited":
            continue
        code = ct.get("exit_code")
        if code is None or code in NORMAL_EXIT_CODES:
            continue
        age = exited_ago_s(ct.get("status"))
        if age is not None and age > window_s:
            continue
        out.append(ct)
    return out


def docker_health(c: Context) -> list[Flag]:
    dk = c.snap.data("docker")
    flags: list[Flag] = []
    summary = dk.get("summary") or {}
    for name in summary.get("unhealthy") or []:
        flags.append(Flag("warn", f"Container {name} is unhealthy", "failing its health check"))
    for name in summary.get("oom_killed") or []:
        flags.append(Flag("crit", f"Container {name} was OOM-killed", "ran out of memory"))
    window = c.cfg.trend_window.total_seconds()
    for ct in recent_error_exits(dk.get("containers") or [], window):
        flags.append(
            Flag(
                "info",
                f"Container {ct.get('name')} exited with an error",
                f"{ct.get('status') or ''}, exit code {ct.get('exit_code')}",
            )
        )
    for ct in dk.get("containers") or []:
        errors = int(num(ct.get("log_errors")))
        if errors >= c.cfg.log_error_burst:
            flags.append(
                Flag(
                    "warn",
                    f"Container {ct.get('name')} logging many errors",
                    f"{errors} error lines in the last check",
                )
            )
    warnings = (dk.get("engine") or {}).get("warnings") or []
    for w in warnings[:2]:
        flags.append(Flag("info", "Docker engine warning", str(w)[:200]))
    return _fold(flags, "container problems")


def image_updates(c: Context) -> list[Flag]:
    data = c.snap.data("image_updates")
    flagged = [i for i in data.get("images") or [] if i.get("status") == "update_available"]
    if not flagged:
        return []
    names = ", ".join(str(i.get("image")) for i in flagged[:4])
    return [Flag("info", f"{len(flagged)} container images have updates", names)]


def docker_data_fs(filesystems: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The filesystem holding /var/lib/docker (longest mount point that contains it)."""
    best = None
    for fs in filesystems:
        mount = str(fs.get("mount") or "")
        inside = mount == "/" or "/var/lib/docker".startswith(mount.rstrip("/") + "/")
        if inside and (best is None or len(mount) > len(str(best.get("mount")))):
            best = fs
    return best


def unused_images(c: Context) -> list[Flag]:
    du = c.snap.data("docker").get("disk_usage") or {}
    unused = num(du.get("images_unused_bytes"))
    fs = docker_data_fs(c.snap.data("system").get("filesystems") or [])
    if fs is None or unused < c.cfg.unused_images_note_gb * 1e9:
        return []
    if num(fs.get("percent")) < c.cfg.disk_warn_percent:
        return []
    return [
        Flag(
            "info",
            f"{fmt_bytes(unused)} of unused Docker images",
            f"disk {fs.get('mount')} is {num(fs.get('percent')):.0f}% full; "
            "'docker image prune -a' would free the unused images",
        )
    ]


def arr_findings(c: Context) -> list[Flag]:
    """Problems found by the *arr/qBittorrent collector. Capped at warn: a broken Radarr
    does not make the server itself critical."""
    flags: list[Flag] = []
    for f in c.snap.data("arr").get("findings") or []:
        sev = "warn" if f.get("severity") in ("warn", "crit") else "info"
        title = f"{f.get('source', '?')}: {f.get('message', '')}"
        flags.append(Flag(sev, title[:160]))
    return _fold(flags, "*arr problems")


def approvals(c: Context) -> list[Flag]:
    n = c.pending_approvals
    if n <= 0:
        return []
    return [Flag("info", f"{n} action(s) waiting for your approval", "see Approvals in the UI")]


def journal(c: Context) -> list[Flag]:
    j = c.journal
    if j is None:
        return []
    flags: list[Flag] = []
    for name, hit in (j.notable or {}).items():
        sev = "crit" if name in ("oom", "filesystem", "hardware") else "warn"
        sample = (hit.get("samples") or [""])[0]
        flags.append(
            Flag(
                sev,
                f"Kernel/system event: {name} ({int(num(hit.get('count')))}x)",
                str(sample)[:200],
            )
        )
    if j.errors >= c.cfg.log_error_burst:
        flags.append(Flag("warn", f"Journal error burst: {j.errors} errors {c.journal_label}", ""))
    for m in j.messages[:3]:
        count = int(num(m.get("count")))
        if count >= c.cfg.log_error_burst and m.get("priority") in (
            "emerg",
            "alert",
            "crit",
            "err",
            "warning",
        ):
            flags.append(
                Flag(
                    "warn",
                    f"Repeated log message x{count} from {m.get('source')}",
                    str(m.get("sample"))[:160],
                )
            )
    return _fold(flags, "journal problems")


def updates(c: Context) -> list[Flag]:
    apt = c.snap.data("apt")
    flags: list[Flag] = []
    sec = int(num(apt.get("security_count")))
    if sec:
        flags.append(Flag("warn", f"{sec} security updates pending", "apt upgrade needed"))
    if apt.get("reboot_required"):
        pkgs = ", ".join((apt.get("reboot_required_pkgs") or [])[:3])
        flags.append(Flag("info", "Reboot required", pkgs))
    age = apt.get("lists_age_h")
    if age is not None and num(age) > 7 * 24:
        flags.append(
            Flag(
                "info",
                "apt package lists are stale",
                f"last refreshed {num(age) / 24:.0f} days ago",
            )
        )
    return flags


RULES: tuple[Rule, ...] = (
    collector_failures,
    disks,
    disk_filling,
    memory,
    load,
    zombies,
    temperatures,
    gpu,
    restart_loops,
    failed_units,
    docker_health,
    image_updates,
    unused_images,
    arr_findings,
    approvals,
    journal,
    updates,
)


def evaluate(c: Context) -> list[Flag]:
    """All flags, worst first (stable within a severity)."""
    flags: list[Flag] = []
    for rule in RULES:
        try:
            flags.extend(rule(c))
        except Exception:
            log.exception("anomaly rule %s failed", rule.__name__)
    return sorted(flags, key=lambda f: -SEVERITY_RANK[f.severity])
