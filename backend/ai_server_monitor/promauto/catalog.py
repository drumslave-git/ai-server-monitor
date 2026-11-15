"""A compact catalog of what a Prometheus scrapes, small enough for a 4B model.

Per job: its targets and a ranked, size-capped list of metrics (type, series count, HELP).
Listing a whole Prometheus is far too much (the user's has 1000 metric names, ~13k tokens
after filtering); one slice per job is ~600 tokens.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from ..promql import PromQL, PromQLError
from .hosts import instance_host, is_other_host

MAX_METRICS_PER_JOB = 28
MAX_FAMILIES = 10
MAX_SLICE_CHARS = 3000
HELP_CHARS = 60
MANY_SERIES = 50
"""A metric with more series than this must be aggregated (sum/topk) to be a useful query."""

# Histogram parts and exporter internals say nothing about the host or the service.
_DROP = re.compile(r"(_bucket|_sum|_count|_created|_info)$")
_INTERNAL = re.compile(
    r"^(go_|process_|promhttp_|scrape_|net_conntrack|node_scrape|node_exporter_|node_textfile|"
    r"prometheus_(tsdb|engine|remote|notifications|sd|rule|template|web|config|target_|http_))"
)
# What the monitor already collects about the host itself.
_COVERED = re.compile(
    r"^node_(cpu_seconds|memory_Mem(Total|Free|Available|Cached|Buffers)|memory_Swap|"
    r"filesystem_(size|avail|free)|network_(receive|transmit)_bytes|disk_(read|written)_bytes|"
    r"disk_io_time_seconds|load\d|boottime|uname|hwmon_temp)"
)
_USEFUL = re.compile(
    r"(error|fail|drop|throttl|oom|retrans|pressure|timeout|restart|state|temperature|power|"
    r"util|saturat|latency|duration|queue|pending|lag|reset|invalid|overflow|inodes|offset|"
    r"health|status|connections|sockets|ecc|clock|fan|battery)",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
# Cumulative values the exporter declares as gauges, or does not declare at all.
_CUMULATIVE_NAME = re.compile(r"(_total$|_count$)")
_CUMULATIVE_WORDS = re.compile(r"((^|_)counters?_|_aggregate_|_cumulative)")
_CUMULATIVE_HELP = re.compile(r"cumulative", re.IGNORECASE)
_SNMP_STYLE = re.compile(r"^node_(netstat|snmp)_")
_SNMP_GAUGES = re.compile(r"(CurrEstab|MaxConn|Forwarding|DefaultTTL|Rto(Algorithm|Min|Max))")
# ECC counters are only worth watching where ECC exists; consumer cards always report 0.
_ECC_ERRORS = re.compile(r"ecc.*errors", re.IGNORECASE)
_ECC_MODE = re.compile(r"ecc.*mode", re.IGNORECASE)


def is_counter(name: str, info: dict[str, Any] | None) -> bool:
    """Does the metric only ever grow (so it must be read through rate() or increase())?

    The declared type decides when it says counter. Exporters are sloppy though: a
    nvidia-smi "…_counters_…_seconds" total is declared a gauge, node's netstat counters have
    no type at all. So the name (_total, counters_, aggregate), the help text ("cumulative") and
    the netstat/snmp naming also count.
    """
    info = info or {}
    kind = str(info.get("type") or "")
    if kind == "counter":
        return True
    if _CUMULATIVE_WORDS.search(name) or _CUMULATIVE_HELP.search(str(info.get("help") or "")):
        return True
    if kind == "gauge":
        return False  # declared a gauge: "_total" alone is not enough (container_fs_inodes_total)
    if _CUMULATIVE_NAME.search(name):
        return True
    return bool(_SNMP_STYLE.match(name)) and not _SNMP_GAUGES.search(name)


def _counter_of(meta: dict[str, Any], name: str) -> bool:
    entries = meta.get(name)
    return is_counter(name, entries[0] if isinstance(entries, list) and entries else None)


def drop_unusable(metrics: dict[str, int]) -> dict[str, int]:
    """Without an ECC mode metric there is no sign the GPU has ECC: leave its error counters."""
    if any(_ECC_MODE.search(n) for n in metrics):
        return metrics
    return {n: c for n, c in metrics.items() if not _ECC_ERRORS.search(n)}


@dataclass
class JobSlice:
    job: str
    targets: list[dict[str, str]]
    metrics: dict[str, int]
    """name -> number of series (all kept metrics, not only the listed ones)."""
    text: str = ""
    other_host: str = ""
    """Address of the other machine this job scrapes ('' = the monitored server)."""
    counters: set[str] = field(default_factory=set)
    """Metrics that only ever grow (see is_counter)."""

    @property
    def usable(self) -> bool:
        return bool(self.metrics)


@dataclass
class Catalog:
    jobs: list[JobSlice] = field(default_factory=list)
    hash: str = ""
    total_metrics: int = 0

    def job(self, name: str) -> JobSlice | None:
        return next((j for j in self.jobs if j.job == name), None)


def keep_metric(name: str) -> bool:
    return not _DROP.search(name) and not _INTERNAL.match(name)


def score(name: str, series: int, has_help: bool) -> int:
    s = 0
    if _USEFUL.search(name):
        s += 3
    if series > 200:
        s -= 2
    if has_help:
        s += 1
    if _COVERED.match(name):
        s -= 5
    return s


def _type_letter(kind: str) -> str:
    return {"gauge": "g", "counter": "c", "histogram": "h", "summary": "s"}.get(kind, "?")


def render_slice(
    job: str, targets: list[dict[str, str]], metrics: dict[str, int], meta: dict[str, Any]
) -> str:
    def info(name: str) -> dict[str, Any]:
        entries = meta.get(name)
        return entries[0] if isinstance(entries, list) and entries else {}

    ranked = sorted(
        metrics,
        key=lambda n: (-score(n, metrics[n], bool(info(n).get("help"))), n),
    )[:MAX_METRICS_PER_JOB]
    families: dict[str, int] = {}
    for name in metrics:
        fam = "_".join(name.split("_")[:2])
        families[fam] = families.get(fam, 0) + 1
    shown = ", ".join(
        f"{fam}_* {n}" for fam, n in sorted(families.items(), key=lambda kv: -kv[1])[:MAX_FAMILIES]
    )
    where = ", ".join(f"{t['instance']} {t['health']}" for t in targets[:4])
    if len(targets) > 4:
        where += f", +{len(targets) - 4} more"
    lines = [
        f"JOB {job}: {len(targets)} target(s) [{where}], {len(metrics)} metrics",
        f"families: {shown}",
        "metrics [g=gauge, c=COUNTER (only grows: use rate/increase), xN = series count]:",
    ]
    for name in ranked:
        meta_entry = info(name)
        kind = (
            "c" if is_counter(name, meta_entry) else _type_letter(str(meta_entry.get("type", "")))
        )
        n = metrics[name]
        many = f" x{n}" + (" aggregate!" if n > MANY_SERIES else "") if n > 1 else ""
        help_text = str(meta_entry.get("help") or "").strip().replace("\n", " ")[:HELP_CHARS]
        lines.append(f"{name} [{kind}{many}] {help_text}".rstrip())
    text = "\n".join(lines)
    return text[:MAX_SLICE_CHARS]


def mentions_known_metric(query: str, metrics: dict[str, int] | set[str]) -> bool:
    """Does the query use at least one metric of this job? (Stops invented metric names:
    an alert-style query on an unknown metric would otherwise be 'healthy' forever.)"""
    return any(token in metrics for token in _TOKEN.findall(query))


def _selector_value(job: str) -> str:
    return job.replace("\\", "\\\\").replace('"', '\\"')


async def build_catalog(client: PromQL, big_client: PromQL) -> Catalog:
    """Read targets, metadata and the metric names of each job. Raises PromQLError."""
    data = await client.get_data("/api/v1/targets", {"state": "active"})
    active = (data or {}).get("activeTargets") or []
    by_job: dict[str, list[dict[str, str]]] = {}
    for t in active:
        labels = t.get("labels") or {}
        job = str(labels.get("job") or "")
        if not job:
            continue
        by_job.setdefault(job, []).append(
            {
                "instance": str(labels.get("instance") or t.get("scrapeUrl") or "?"),
                "health": str(t.get("health") or "unknown"),
            }
        )
    try:
        meta = await client.get_data("/api/v1/metadata") or {}
    except PromQLError:
        meta = {}  # older/proxied Prometheus without metadata: names and counts still work
    jobs: list[JobSlice] = []
    for job in sorted(by_job):
        result = await big_client.instant(f'count by (__name__) ({{job="{_selector_value(job)}"}})')
        metrics: dict[str, int] = {}
        for series in result.series:
            name = (series.get("labels") or {}).get("__name__")
            value = series.get("value")
            if name and keep_metric(name) and isinstance(value, int | float):
                metrics[name] = int(value)
        metrics = drop_unusable(metrics)
        other = next(
            (
                instance_host(t["instance"])
                for t in by_job[job]
                if is_other_host(t["instance"], job)
            ),
            "",
        )
        jobs.append(
            JobSlice(
                job=job,
                targets=by_job[job],
                metrics=metrics,
                text=render_slice(job, by_job[job], metrics, meta) if metrics else "",
                other_host=other,
                counters={n for n in metrics if _counter_of(meta, n)},
            )
        )
    digest = hashlib.sha256()
    for j in jobs:
        digest.update(j.job.encode() + b"\0" + "\0".join(sorted(j.metrics)).encode() + b"\1")
    return Catalog(jobs, digest.hexdigest()[:16], sum(len(j.metrics) for j in jobs))
