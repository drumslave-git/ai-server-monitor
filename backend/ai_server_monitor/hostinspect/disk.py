"""`du`-style breakdown of a directory with a time budget, run through the sandbox.

A big media disk can take minutes to walk, so the scan never blocks: children of the target
are measured in parallel with idle IO priority, and when the budget is up the children that
are done are reported and the rest are listed as "not finished". Results are cached per
(path, depth) for a few minutes.
"""

from __future__ import annotations

import posixpath
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from .output import human
from .sandbox import RunResult

Runner = Callable[..., RunResult]
"""run(stages, timeout=..., max_chars=..., collect_all=...) -> RunResult"""

SCAN_BUDGET_S = 20.0
"""Default scan budget; the chat passes what is left of `chat.tool_timeout`."""
CACHE_TTL_S = 600.0
"""Default cache lifetime (`chat.disk_cache_ttl`)."""
MAX_CHILDREN_PARALLEL = 60
PARALLEL = 3
TOP_ENTRIES = 20
MAX_DU_LINES = 5000

_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


@dataclass
class Entry:
    path: str
    bytes: int
    complete: bool = True


@dataclass
class Scan:
    entries: list[Entry] = field(default_factory=list)
    unfinished: list[str] = field(default_factory=list)
    unreadable: int = 0
    elapsed_s: float = 0.0
    complete: bool = True


def parse_du(text: str) -> list[tuple[str, int]]:
    """`du -B1` lines → (path, bytes)."""
    rows = []
    for line in text.splitlines():
        size, _, path = line.partition("\t")
        if size.isdigit() and path:
            rows.append((path, int(size)))
    return rows


def _count_unreadable(stderr: str) -> int:
    return sum(
        1 for line in stderr.splitlines() if "Permission denied" in line or "cannot read" in line
    )


def scan(run: Runner, path: str, depth: int, budget_s: float = SCAN_BUDGET_S) -> Scan:
    """Measure `path` down to `depth` levels. Blocking; call from a thread."""
    started = time.monotonic()
    deadline = started + budget_s
    result = Scan()
    listing = run([["ls", "-A", "-1", path]], timeout=budget_s, max_chars=400_000, collect_all=True)
    names = [n for n in listing.output.splitlines() if n]
    if listing.exit_code not in (0, None) and not names:
        raise FileNotFoundError(listing.stderr.strip() or f"cannot list {path}")
    base = path.rstrip("/") or ""
    if not names or len(names) > MAX_CHILDREN_PARALLEL:
        # Too many children for one process each: a single walk, partial if it runs out of time.
        _single_walk(run, path, depth, deadline, result)
    else:
        _per_child(run, base, names, depth, deadline, result)
    result.elapsed_s = round(time.monotonic() - started, 1)
    result.entries.sort(key=lambda e: e.bytes, reverse=True)
    return result


def _single_walk(run: Runner, path: str, depth: int, deadline: float, out: Scan) -> None:
    r = run(
        [["du", "-x", "-B1", f"--max-depth={depth}", path]],
        timeout=max(deadline - time.monotonic(), 1.0),
        max_chars=2_000_000,
        collect_all=True,
    )
    out.unreadable += _count_unreadable(r.stderr)
    rows = parse_du(r.output)[:MAX_DU_LINES]
    out.entries.extend(
        Entry(p, b) for p, b in rows if posixpath.normpath(p) != posixpath.normpath(path)
    )
    if r.stopped:
        out.complete = False
        out.unfinished.append(f"{path} (walk stopped; sizes of the directories shown are partial)")


def _per_child(
    run: Runner, base: str, names: list[str], depth: int, deadline: float, out: Scan
) -> None:
    def measure(name: str) -> tuple[str, RunResult | None]:
        left = deadline - time.monotonic()
        if left <= 0.5:
            return name, None
        child = f"{base}/{name}"
        r = run(
            [["du", "-x", "-B1", f"--max-depth={depth - 1}", child]],
            timeout=left,
            max_chars=400_000,
            collect_all=True,
        )
        return name, r

    pool = ThreadPoolExecutor(max_workers=PARALLEL)
    futures = [pool.submit(measure, n) for n in names]
    wait(futures, timeout=max(deadline - time.monotonic(), 0.1) + 2)
    pool.shutdown(wait=False, cancel_futures=True)
    for name, future in zip(names, futures, strict=True):
        child = f"{base}/{name}"
        if not future.done():
            out.unfinished.append(child)
            continue
        _, r = future.result()
        if r is None or r.stopped:
            out.unfinished.append(child)
            if r is not None:
                rows = parse_du(r.output)
                out.entries.extend(Entry(p, b, complete=False) for p, b in rows[:MAX_DU_LINES])
            continue
        out.unreadable += _count_unreadable(r.stderr)
        rows = parse_du(r.output)
        out.entries.extend(Entry(p, b) for p, b in rows[:MAX_DU_LINES])
    out.complete = not out.unfinished


def largest_files(
    run: Runner, path: str, count: int, min_mb: int = 100, budget_s: float = SCAN_BUDGET_S
) -> dict[str, Any]:
    r = run(
        [["find", path, "-xdev", "-type", "f", "-size", f"+{min_mb}M", "-printf", "%s\t%p\n"]],
        timeout=budget_s,
        max_chars=2_000_000,
        collect_all=True,
    )
    rows = sorted(parse_du(r.output), key=lambda x: x[1], reverse=True)
    out: dict[str, Any] = {
        "path": path,
        "min_size": f"{min_mb} MB",
        "files": [{"path": p, "size": human(b)} for p, b in rows[:count]],
        "found": len(rows),
    }
    if r.stopped:
        out["approximate"] = f"scan stopped after {budget_s:.0f}s; larger files may be missing"
    if not rows and not r.stopped:
        out["note"] = f"no files over {min_mb} MB under {path}"
    return out


def summarize(
    scanned: Scan,
    path: str,
    depth: int,
    owners: Callable[[str], list[str]],
    inside: list[dict[str, str]],
) -> dict[str, Any]:
    norm = posixpath.normpath(path)
    top = [e for e in scanned.entries if posixpath.normpath(e.path) != norm][:TOP_ENTRIES]
    total = next((e.bytes for e in scanned.entries if posixpath.normpath(e.path) == norm), None)
    if total is None:
        total = sum(e.bytes for e in scanned.entries if posixpath.dirname(e.path) == norm)
    rows = []
    for e in top:
        row: dict[str, Any] = {"path": e.path, "size": human(e.bytes)}
        if total and scanned.complete:  # (of a partial total a percentage would mislead)
            row["percent"] = round(e.bytes / total * 100, 1)
        if not e.complete:
            row["partial"] = True
        who = owners(e.path)
        if who:
            row["owner"] = who
        rows.append(row)
    out: dict[str, Any] = {
        "path": path,
        "depth": depth,
        "total_scanned": human(total) if scanned.complete else f"at least {human(total)}",
        "largest": rows,
        "scan_seconds": scanned.elapsed_s,
    }
    own = owners(path)
    if own:
        out["path_owner"] = own
    if inside:
        out["owners_below"] = inside
    if not scanned.complete:
        out["result"] = "PARTIAL: the scan hit its time limit"
        out["approximate"] = (
            f"scan stopped after {scanned.elapsed_s:.0f}s; NOT measured (or only partly): "
            + ", ".join(scanned.unfinished[:15])
            + (f" and {len(scanned.unfinished) - 15} more" if len(scanned.unfinished) > 15 else "")
        )
        out["warning"] = (
            "These sizes are NOT the disk's usage: the entries named above are missing or "
            "partial and may be larger than any listed. Do not call the total the disk's usage "
            "or name a 'largest' directory as final; say which folders were not measured."
        )
    if scanned.unreadable:
        out["unreadable"] = (
            f"{scanned.unreadable} entries could not be read (permission denied for the "
            "unprivileged inspection user); real usage may be larger"
        )
    return out


def cached(
    path: str, depth: int, now: float | None = None, ttl: float = CACHE_TTL_S
) -> dict[str, Any] | None:
    now = time.time() if now is None else now
    with _lock:
        hit = _cache.get((path, depth))
    if hit and now - hit[0] < ttl:
        return {**hit[1], "cached_age_s": round(now - hit[0])}
    return None


def remember(path: str, depth: int, value: dict[str, Any], now: float | None = None) -> None:
    with _lock:
        _cache[(path, depth)] = (time.time() if now is None else now, value)


def clear_cache() -> None:
    with _lock:
        _cache.clear()
