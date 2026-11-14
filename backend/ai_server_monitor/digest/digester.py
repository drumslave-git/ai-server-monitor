"""Snapshot digester: collector data -> compact, pre-analyzed plain text for the LLM.

Plain text sections (not JSON) because small models read them better. Every section has
a priority; over the token budget the lowest-priority sections are shortened first, then
dropped. The flags section (rule output) and the header are never cut.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import ReportSettings
from ..db import Database
from ..llm.text import estimate_tokens
from .models import (
    Flag,
    fmt_bytes,
    fmt_duration,
    fmt_pct,
    fmt_rate,
    num,
)
from .rules import Context, evaluate, recent_error_exits
from .snapshot import Snapshot
from .trends import JournalSummary, Trends, WindowObserver, compute_trends, summarize_windows

NEVER_DROP = 90  # sections at or above this priority are kept (and not shortened)
MIN_LINES = 2  # a shortened section keeps at least this many body lines


@dataclass
class Section:
    key: str
    title: str
    priority: int
    lines: list[str]

    def render(self, limit: int | None = None) -> str:
        body = self.lines
        if limit is not None and len(body) > limit:
            body = [*body[:limit], f"(+{len(self.lines) - limit} more lines omitted)"]
        return "\n".join([f"## {self.title}", *body])


@dataclass
class Digest:
    text: str
    flags: list[Flag]
    hash: str
    tokens: int
    generated: float
    sections: list[dict[str, Any]] = field(default_factory=list)
    """Per section: key, title, priority, lines, kept_lines (0 = dropped)."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tokens": self.tokens,
            "hash": self.hash,
            "generated": self.generated,
            "flags": [f.to_dict() for f in self.flags],
            "sections": self.sections,
        }


# --------------------------------------------------------------------------- sections


def _top(
    items: list[dict[str, Any]], key: str, n: int, minimum: float = 0.0
) -> list[dict[str, Any]]:
    rows = [i for i in items if num(i.get(key)) > minimum]
    return sorted(rows, key=lambda i: -num(i.get(key)))[:n]


def _header(snap: Snapshot, flags: list[Flag]) -> str:
    sysd = snap.data("system")
    apt = snap.data("apt")
    parts = [str(sysd.get("hostname") or "server")]
    if apt.get("distro"):
        parts.append(str(apt["distro"]))
    if sysd.get("kernel"):
        parts.append(f"kernel {sysd['kernel']}")
    if sysd.get("uptime_s") is not None:
        parts.append(f"up {fmt_duration(sysd['uptime_s'])}")
    stamp = datetime.fromtimestamp(snap.now, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"# SERVER DIGEST — {', '.join(parts)} — generated {stamp}"


def _collectors_section(snap: Snapshot) -> Section:
    ok: list[str] = []
    problems: list[str] = []
    for name, entry in snap.raw.items():
        state = entry.get("state")
        if state == "disabled":
            continue
        if state == "unavailable":
            problems.append(f"{name}: not available ({entry.get('reason') or 'unknown reason'})")
        elif entry.get("ok") is False:
            problems.append(f"{name}: FAILING ({entry.get('error') or 'unknown error'})")
        elif entry.get("ok") is None:
            problems.append(f"{name}: no data yet")
        else:
            ok.append(name)
    lines = [f"working: {', '.join(ok) or 'none'}", *problems]
    return Section("collectors", "Data sources", 70, lines)


def _flags_section(flags: list[Flag]) -> Section:
    if not flags:
        return Section("flags", "Rule-based flags", 100, ["none — all checks passed"])
    lines = []
    for f in flags:
        tail = f" — {f.detail}" if f.detail else ""
        lines.append(f"[{f.severity.upper()}] {f.title}{tail}")
    return Section("flags", "Rule-based flags", 100, lines)


def _resources_section(snap: Snapshot) -> Section | None:
    sysd = snap.data("system")
    if not sysd:
        return None
    lines: list[str] = []
    cpu = sysd.get("cpu") or {}
    if cpu:
        loads = "/".join(f"{num(x):.2f}" for x in (cpu.get("load") or []))
        extra = ""
        if num(cpu.get("iowait_percent")) >= 5:
            extra += f", iowait {fmt_pct(num(cpu['iowait_percent']))}"
        if num(cpu.get("steal_percent")) >= 5:
            extra += f", steal {fmt_pct(num(cpu['steal_percent']))}"
        lines.append(
            f"CPU {fmt_pct(num(cpu.get('percent')))} busy on {cpu.get('cores_logical', '?')} "
            f"threads{extra}; load 1/5/15 min: {loads}"
        )
    mem = sysd.get("memory") or {}
    if mem:
        swap = (
            f", swap {fmt_pct(num(mem.get('swap_percent')))} of {fmt_bytes(mem.get('swap_total'))}"
            if num(mem.get("swap_total")) > 0
            else ", no swap"
        )
        lines.append(
            f"Memory {fmt_pct(num(mem.get('percent')))} used "
            f"({fmt_bytes(mem.get('used'))} of {fmt_bytes(mem.get('total'))}, "
            f"{fmt_bytes(mem.get('available'))} available, cache excluded){swap}"
        )
    procs = sysd.get("processes") or {}
    if procs:
        lines.append(f"Processes {procs.get('total', '?')} total, {procs.get('zombies', 0)} zombie")
    busy = _top(sysd.get("disks") or [], "busy_percent", 3, minimum=10)
    for d in busy:
        lines.append(
            f"Disk {d.get('name')} busy {fmt_pct(num(d.get('busy_percent')))} "
            f"(read {fmt_rate(d.get('read_bps'))}, write {fmt_rate(d.get('write_bps'))})"
        )
    for nic in (sysd.get("network") or [])[:3]:
        extra = ""
        if num(nic.get("errors")):
            extra = f", {int(num(nic['errors']))} errors"
        lines.append(
            f"Network {nic.get('name')}: in {fmt_rate(nic.get('rx_bps'))}, "
            f"out {fmt_rate(nic.get('tx_bps'))}{extra}"
        )
    temps = [t for t in sysd.get("temperatures") or [] if t.get("current") is not None]
    if temps:
        hottest = max(temps, key=lambda t: num(t.get("current")))
        lines.append(
            f"Hottest sensor: {hottest.get('label') or hottest.get('sensor')} "
            f"{num(hottest.get('current')):.0f}°C"
        )
    return Section("resources", "Resources", 90, lines)


def _disks_section(snap: Snapshot, trends: Trends) -> Section | None:
    filesystems = snap.data("system").get("filesystems") or []
    if not filesystems:
        return None
    lines: list[str] = []
    for fs in sorted(filesystems, key=lambda f: -num(f.get("percent"))):
        mount = fs.get("mount", "?")
        line = (
            f"{mount} ({fs.get('fstype', '?')}): {fmt_pct(num(fs.get('percent')))} used, "
            f"{fmt_bytes(fs.get('avail'))} free of {fmt_bytes(fs.get('total'))}"
        )
        tr = trends.disks.get(mount)
        if tr is not None:
            if abs(tr.growth_per_day) >= 50e6:
                sign = "+" if tr.growth_per_day > 0 else "-"
                line += f"; trend {sign}{fmt_bytes(abs(tr.growth_per_day))}/day"
                if tr.span_days >= 1.5:
                    line += f" over {tr.span_days:.0f}d"
            if tr.eta_days is not None:
                line += f", full in ~{tr.eta_days:.0f}d"
        lines.append(line)
    return Section("disks", "Disks", 80, lines)


def _systemd_section(snap: Snapshot, trends: Trends) -> Section | None:
    sd = snap.data("systemd")
    if not sd:
        return None
    lines = [
        f"System state: {sd.get('system_state', '?')}; {sd.get('units_total', '?')} units, "
        f"{len(sd.get('failed') or [])} failed"
    ]
    for u in (sd.get("failed") or [])[:8]:
        since = u.get("since")
        age = f", since {fmt_duration(snap.now - num(since))} ago" if since else ""
        lines.append(f"FAILED {u.get('name')} ({u.get('result') or u.get('sub')}){age}")
    for u in (sd.get("restarting") or [])[:5]:
        today = trends.unit_restarts_24h.get(u.get("name"), 0)
        lines.append(
            f"restarts: {u.get('name')} {int(num(u.get('n_restarts')))} total"
            + (f", {today} in last 24h" if today else "")
        )
    if len(lines) == 1:
        lines.append("no failed units")
    return Section("systemd", "Services (systemd)", 75, lines)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _pick_sample(samples: list[str]) -> str:
    """The most informative sample line: skip bare traceback/stack lines."""
    cleaned = [_ANSI.sub("", str(s)).strip() for s in samples]
    useful = [s for s in cleaned if s and not s.startswith(("Traceback", "File ", "^"))]
    return (useful or cleaned or [""])[0][:140]


def _containers_section(
    snap: Snapshot, trends: Trends, top_n: int, window_s: float
) -> Section | None:
    dk = snap.data("docker")
    if not dk:
        return None
    containers = dk.get("containers") or []
    summary = dk.get("summary") or {}
    lines = [
        f"{summary.get('running', '?')} running of "
        f"{summary.get('total', len(containers))} containers"
        + (f", {len(summary.get('unhealthy') or [])} unhealthy" if summary.get("unhealthy") else "")
    ]
    running = [c for c in containers if c.get("state") == "running"]
    cpu = _top(running, "cpu_percent", top_n, minimum=1)
    if cpu:
        lines.append(
            "Top CPU: " + ", ".join(f"{c['name']} {fmt_pct(num(c['cpu_percent']))}" for c in cpu)
        )
    mem = _top(running, "mem_usage", top_n)
    if mem:
        lines.append(
            "Top RAM: " + ", ".join(f"{c['name']} {fmt_bytes(c['mem_usage'])}" for c in mem)
        )
    io = sorted(running, key=lambda c: -(num(c.get("blk_read_bps")) + num(c.get("blk_write_bps"))))[
        :top_n
    ]
    io = [c for c in io if num(c.get("blk_read_bps")) + num(c.get("blk_write_bps")) >= 100_000]
    if io:
        lines.append(
            "Top disk IO: "
            + ", ".join(
                f"{c['name']} r {fmt_rate(c.get('blk_read_bps'))} "
                f"w {fmt_rate(c.get('blk_write_bps'))}"
                for c in io
            )
        )
    for c in containers:
        if c.get("state") == "restarting" or (c.get("health") == "unhealthy"):
            lines.append(f"PROBLEM {c['name']}: state {c.get('state')}, health {c.get('health')}")
    if trends.docker_restarts_24h:
        lines.append(
            "Restarts in 24h: "
            + ", ".join(
                f"{n} x{k}"
                for n, k in sorted(trends.docker_restarts_24h.items(), key=lambda kv: -kv[1])[:6]
            )
        )
    if summary.get("restarted_recently"):
        lines.append("Restarted since last check: " + ", ".join(summary["restarted_recently"][:6]))
    if summary.get("oom_killed"):
        lines.append("OOM-killed: " + ", ".join(summary["oom_killed"][:6]))
    exits = recent_error_exits(containers, window_s)
    if exits:
        lines.append("Exited with error: " + ", ".join(str(c.get("name")) for c in exits[:6]))
    noisy = _top(containers, "log_errors", 3, minimum=0)
    for c in noisy:
        sample = _pick_sample(c.get("log_error_samples") or [])
        lines.append(f'Log errors: {c["name"]} {int(num(c["log_errors"]))} lines, e.g. "{sample}"')
    du = dk.get("disk_usage") or {}
    if du:
        lines.append(
            f"Docker disk: images {fmt_bytes(du.get('images_bytes'))} "
            f"({fmt_bytes(du.get('images_unused_bytes'))} unused), "
            f"volumes {fmt_bytes(du.get('volumes_bytes'))}, "
            f"build cache {fmt_bytes(du.get('build_cache_bytes'))}"
        )
    return Section("containers", "Containers (Docker)", 65, lines)


def _process_section(snap: Snapshot, top_n: int) -> Section | None:
    procs = snap.data("system").get("processes") or {}
    if not procs:
        return None
    names = {c.get("id"): c.get("name") for c in snap.data("docker").get("containers") or []}

    def label(p: dict[str, Any]) -> str:
        cid = p.get("container_id")
        where = f" [container {names.get(cid) or cid}]" if cid else ""
        return f"{p.get('name')}{where}"

    lines: list[str] = []
    cpu = _top(procs.get("top_cpu") or [], "cpu_percent", top_n, minimum=1)
    if cpu:
        lines.append(
            "By CPU: " + ", ".join(f"{label(p)} {fmt_pct(num(p['cpu_percent']))}" for p in cpu)
        )
    mem = (procs.get("top_memory") or [])[:top_n]
    if mem:
        lines.append("By RAM: " + ", ".join(f"{label(p)} {fmt_bytes(p.get('rss'))}" for p in mem))
    if not lines:
        return None
    return Section("processes", "Top processes", 60, lines)


def _gpu_section(snap: Snapshot) -> Section | None:
    gpus = snap.data("gpu").get("gpus") or []
    if not gpus:
        return None
    lines: list[str] = []
    for g in gpus:
        line = (
            f"GPU {g.get('index', 0)} {g.get('name')}: util {fmt_pct(num(g.get('util_percent')))}, "
            f"VRAM {fmt_bytes(g.get('mem_used'))} of {fmt_bytes(g.get('mem_total'))} "
            f"({fmt_pct(num(g.get('mem_percent')))}), {num(g.get('temperature_c')):.0f}°C, "
            f"{num(g.get('power_w')):.0f}W of {num(g.get('power_limit_w')):.0f}W"
        )
        lines.append(line)
        procs = sorted(g.get("processes") or [], key=lambda p: -num(p.get("mem_used")))[:3]
        if procs:
            lines.append(
                "  VRAM by process: "
                + ", ".join(f"{p.get('name')} {fmt_bytes(p.get('mem_used'))}" for p in procs)
            )
    return Section("gpu", "GPU", 55, lines)


def _journal_section(j: JournalSummary | None, label: str) -> Section | None:
    if j is None:
        return None
    prios = ", ".join(f"{n} {p}" for p, n in sorted(j.by_priority.items(), key=lambda kv: -kv[1]))
    lines = [f"{j.entries} entries {label} ({prios or 'none'})"]
    for name, hit in j.notable.items():
        sample = (hit.get("samples") or [""])[0][:160]
        lines.append(f"EVENT {name} x{int(num(hit.get('count')))}: {sample}")
    for m in j.messages[:4]:
        lines.append(
            f"[{m.get('priority')}] x{int(num(m.get('count')))} {m.get('source')}: "
            f"{str(m.get('sample'))[:140]}"
        )
    return Section("journal", "System journal", 50, lines)


def _updates_section(snap: Snapshot) -> Section | None:
    apt = snap.data("apt")
    img = snap.data("image_updates")
    lines: list[str] = []
    if apt:
        line = (
            f"apt: {apt.get('upgradable_count', 0)} upgradable, "
            f"{apt.get('security_count', 0)} security"
        )
        if apt.get("reboot_required"):
            line += ", REBOOT REQUIRED"
        lines.append(line)
        pkgs = [p for p in apt.get("packages") or [] if isinstance(p, dict)]
        pkgs.sort(key=lambda p: not p.get("security"))
        if pkgs:
            lines.append(
                "  packages: "
                + ", ".join(
                    f"{p.get('name')}{' (security)' if p.get('security') else ''}" for p in pkgs[:8]
                )
                + (" …" if len(pkgs) > 8 or apt.get("truncated") else "")
            )
    if img:
        total = (img.get("summary") or {}).get("total", len(img.get("images") or []))
        updates = [i for i in img.get("images") or [] if i.get("status") == "update_available"]
        if updates:
            shown = ", ".join(
                f"{i.get('image')} ({', '.join(map(str, (i.get('containers') or [])[:3]))})"
                for i in updates[:6]
            )
            lines.append(f"container images with newer builds: {len(updates)} of {total} — {shown}")
        else:
            lines.append(f"container images: no newer builds found ({total} checked)")
    if not lines:
        return None
    return Section("updates", "Pending updates", 40, lines)


# --------------------------------------------------------------------------- budget


def fit_budget(
    sections: list[Section], header: str, budget_tokens: int
) -> tuple[str, dict[str, int]]:
    """Render the sections within the budget. Returns (text, {key: kept_lines})."""
    limits: dict[str, int | None] = {s.key: None for s in sections}
    dropped: set[str] = set()

    def render() -> str:
        parts = [header]
        for s in sections:
            if s.key not in dropped:
                parts.append(s.render(limits[s.key]))
        return "\n\n".join(parts)

    def lines_kept(s: Section) -> int:
        lim = limits[s.key]
        return len(s.lines) if lim is None else min(lim, len(s.lines))

    text = render()
    while estimate_tokens(text) > budget_tokens:
        candidates = [s for s in sections if s.priority < NEVER_DROP and s.key not in dropped]
        if not candidates:
            break
        shrinkable = [s for s in candidates if lines_kept(s) > MIN_LINES]
        if shrinkable:
            target = min(shrinkable, key=lambda s: s.priority)
            limits[target.key] = max(MIN_LINES, lines_kept(target) // 2)
        else:
            dropped.add(min(candidates, key=lambda s: s.priority).key)
        text = render()
    return text, {s.key: 0 if s.key in dropped else lines_kept(s) for s in sections}


# --------------------------------------------------------------------------- fingerprint

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_VOLATILE_LINE = re.compile(
    r"^(# SERVER DIGEST|Processes |Network |Hottest sensor|Top |By CPU|By RAM)"
)


def _round_sig(match: re.Match[str]) -> str:
    value = float(match.group(0))
    if value == 0:
        return "0"
    magnitude = 10 ** (len(str(int(value))) - 1) if value >= 1 else 1
    return str(int(round(value / magnitude) * magnitude)) if value >= 1 else "0"


def fingerprint(text: str) -> str:
    """Hash of the digest with volatile content blurred: timestamps/uptime and live rates
    are dropped, remaining numbers are rounded to one significant digit. Small
    fluctuations keep the hash; a new flag, failed unit or state change flips it."""
    kept = [ln for ln in text.splitlines() if not _VOLATILE_LINE.match(ln)]
    blurred = _NUMBER.sub(_round_sig, "\n".join(kept))
    return hashlib.sha256(blurred.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- entry point


async def build_digest(
    raw_snapshot: dict[str, dict[str, Any]],
    db: Database,
    cfg: ReportSettings,
    *,
    observer: WindowObserver | None = None,
    now: float | None = None,
) -> Digest:
    now = time.time() if now is None else now
    snap = Snapshot(raw_snapshot, now)
    window_s = cfg.trend_window.total_seconds()
    trends = await compute_trends(
        db, snap.data("system"), snap.data("docker"), snap.data("systemd"), window_s, now
    )

    # Journal: sum all windows since the last report interval when observed, else the
    # latest window alone.
    journal: JournalSummary | None = None
    label = "in the last collection window"
    if observer is not None:
        journal = observer.summary(now - cfg.interval.total_seconds())
        if journal is not None:
            label = f"in the last {fmt_duration(max(journal.seconds, 60))}"
    if journal is None and snap.has("journal"):
        journal = summarize_windows([snap.data("journal")])
        label = f"in the last {fmt_duration(max(journal.seconds, 60))}"

    flags = evaluate(Context(snap, cfg, trends, journal, label))

    sections: list[Section] = [
        s
        for s in (
            _flags_section(flags),
            _collectors_section(snap),
            _resources_section(snap),
            _disks_section(snap, trends),
            _systemd_section(snap, trends),
            _containers_section(snap, trends, cfg.digest_top_n, window_s),
            _process_section(snap, cfg.digest_top_n),
            _gpu_section(snap),
            _journal_section(journal, label),
            _updates_section(snap),
        )
        if s is not None
    ]
    header = _header(snap, flags)
    text, kept = fit_budget(sections, header, cfg.digest_token_budget)
    return Digest(
        text=text,
        flags=flags,
        hash=fingerprint(text),
        tokens=estimate_tokens(text),
        generated=now,
        sections=[
            {
                "key": s.key,
                "title": s.title,
                "priority": s.priority,
                "lines": len(s.lines),
                "kept_lines": kept[s.key],
            }
            for s in sections
        ],
    )
