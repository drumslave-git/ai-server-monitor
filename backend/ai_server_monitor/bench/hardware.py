"""The hardware load while a benchmark run goes: GPU, VRAM, power, host CPU and RAM.

A speed number alone says little ("12 t/s" at 20 GB of VRAM is not "12 t/s" at 6 GB), so a run
samples the machine the monitor runs on every second or so and keeps the samples with the run.
GPU numbers come from the monitor's own GPU collector (NVML), CPU and RAM from psutil, the same
sources as the dashboard; nothing new runs on the host.

Whether the numbers are the model's: the LLM server is looked up by name among the processes the
GPU lists. Seen → the VRAM it holds is recorded on its own. Not seen and the endpoint is not a
local address → the LLM server is elsewhere and the numbers are only "this host"; the page says
so instead of presenting them as the model's usage.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any
from urllib.parse import urlparse

import psutil

from ..collectors.base import Collector
from ..db import Database

log = logging.getLogger(__name__)

LLM_PROCESS_NAMES = (
    "llama-server",
    "llama_server",
    "ollama",
    "vllm",
    "koboldcpp",
    "sglang",
    "tabbyapi",
    "text-generation",
)
"""Process names (substring, any case) of the usual LLM servers."""
FIELDS = ("gpu_util", "vram_used", "llm_vram", "power_w", "temp_c", "cpu", "ram_used")
COLUMNS = ("ts", *FIELDS, "vram_total", "ram_total")
MAX_POINTS = 600
"""Samples sent to the chart; more are averaged into buckets."""
FIRST_SAMPLE_DELAY_S = 0.3
FLUSH_EVERY = 10
LOCAL_HOSTS = frozenset({"localhost", "host.docker.internal", "gateway.docker.internal"})


def is_llm_process(name: str | None) -> bool:
    low = (name or "").lower()
    return any(token in low for token in LLM_PROCESS_NAMES)


def endpoint_is_local(base_url: str | None) -> bool:
    """Does the LLM endpoint point at this machine by its address (localhost, loopback)?"""
    host = (urlparse(base_url or "").hostname or "").lower()
    if not host:
        return False
    if host in LOCAL_HOSTS or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _cpu_busy(times: Any) -> tuple[float, float]:
    """(busy, total) seconds from psutil's cpu_times; guest time is already inside user."""
    fields = {k: v for k, v in times._asdict().items() if k not in ("guest", "guest_nice")}
    total = float(sum(fields.values()))
    idle = float(fields.get("idle", 0.0) + fields.get("iowait", 0.0))
    return total - idle, total


def read_gpu(data: dict[str, Any] | None) -> dict[str, Any]:
    """Collapse the GPU collector's result to one number set across all GPUs; names the LLM
    process when the GPU lists it."""
    out: dict[str, Any] = {"gpus": 0, "llm_process": None}
    gpus = (data or {}).get("gpus") or []
    if not gpus:
        return out
    out["gpus"] = len(gpus)
    out["name"] = gpus[0].get("name")

    def total(key: str) -> float | None:
        vals = [g[key] for g in gpus if g.get(key) is not None]
        return sum(vals) if vals else None

    def top(key: str) -> float | None:
        vals = [g[key] for g in gpus if g.get(key) is not None]
        return max(vals) if vals else None

    out.update(
        gpu_util=top("util_percent"),
        vram_used=total("mem_used"),
        vram_total=total("mem_total"),
        power_w=total("power_w"),
        temp_c=top("temperature_c"),
    )
    held: list[int] = []
    for g in gpus:
        for proc in g.get("processes") or []:
            if is_llm_process(proc.get("name")):
                out["llm_process"] = proc["name"]
                if proc.get("mem_used") is not None:
                    held.append(int(proc["mem_used"]))
    out["llm_vram"] = sum(held) if held else None
    return out


class HardwareSampler:
    """Samples in the background from `start()` to `stop()`; keeps every sample in memory (for
    per-attempt numbers) and writes them to `bench_hw_samples` in small batches."""

    def __init__(self, db: Database, run_id: int, gpu: Collector | None, interval_s: float) -> None:
        self.db = db
        self.run_id = run_id
        self.gpu = gpu
        self.interval_s = interval_s
        self.samples: list[dict[str, Any]] = []
        self.gpu_name: str | None = None
        self.gpu_count = 0
        self.llm_process: str | None = None
        self._pending: list[tuple[Any, ...]] = []
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"bench-hw-{self.run_id}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._flush()

    async def _loop(self) -> None:
        prev = psutil.cpu_times()
        delay = min(self.interval_s, FIRST_SAMPLE_DELAY_S)
        while True:
            await asyncio.sleep(delay)
            delay = self.interval_s
            try:
                now = psutil.cpu_times()
                sample = await self.sample(prev, now)
                prev = now
            except asyncio.CancelledError:
                raise
            except Exception:  # one bad reading must not stop the recording
                log.debug("hardware sample failed", exc_info=True)
                continue
            self.samples.append(sample)
            self._pending.append(tuple(sample.get(c) for c in COLUMNS))
            if len(self._pending) >= FLUSH_EVERY:
                await self._flush()

    async def sample(self, prev: Any, now: Any) -> dict[str, Any]:
        busy0, total0 = _cpu_busy(prev)
        busy1, total1 = _cpu_busy(now)
        cpu = round((busy1 - busy0) / (total1 - total0) * 100, 1) if total1 > total0 else None
        vm = psutil.virtual_memory()
        gpu: dict[str, Any] = {}
        if self.gpu is not None:
            try:
                gpu = read_gpu(await asyncio.wait_for(self.gpu.collect(), 5))
            except Exception:
                log.debug("GPU reading failed", exc_info=True)
        if gpu.get("gpus"):
            self.gpu_count = gpu["gpus"]
            self.gpu_name = self.gpu_name or gpu.get("name")
        if gpu.get("llm_process"):
            self.llm_process = gpu["llm_process"]
        return {
            "ts": time.time(),
            "gpu_util": gpu.get("gpu_util"),
            "vram_used": gpu.get("vram_used"),
            "vram_total": gpu.get("vram_total"),
            "llm_vram": gpu.get("llm_vram"),
            "power_w": gpu.get("power_w"),
            "temp_c": gpu.get("temp_c"),
            "cpu": cpu,
            "ram_used": vm.total - vm.available,
            "ram_total": vm.total,
        }

    async def _flush(self) -> None:
        rows, self._pending = self._pending, []
        if not rows:
            return
        try:
            await self.db.executemany(
                f"INSERT INTO bench_hw_samples (run_id, {', '.join(COLUMNS)}) "
                f"VALUES (?, {', '.join('?' * len(COLUMNS))})",
                [(self.run_id, *r) for r in rows],
            )
        except Exception:
            log.warning("could not save hardware samples of run %d", self.run_id, exc_info=True)

    def describe(self, base_url: str | None) -> dict[str, Any]:
        """What the recording says about itself (stored in the run's meta)."""
        has_gpu = self.gpu_count > 0
        if self.llm_process:
            here = "here"
        elif endpoint_is_local(base_url):
            here = "unknown"
        else:
            here = "elsewhere"
        return {
            "recorded": True,
            "interval_s": self.interval_s,
            "samples": len(self.samples),
            "gpu": has_gpu,
            "gpu_name": self.gpu_name,
            "gpu_count": self.gpu_count,
            "llm_process": self.llm_process,
            "llm_here": here,
            "baseline": self.baseline(),
        }

    def baseline(self) -> dict[str, Any] | None:
        """The first reading, before the model was loaded: what else was already using the box."""
        if not self.samples:
            return None
        first = self.samples[0]
        return {k: first.get(k) for k in ("vram_used", "ram_used", "cpu", "gpu_util")}


# --------------------------------------------------------------------------- numbers


def stats(samples: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    """{n, vram_total, ram_total, <field>: {avg, peak} | None} over some samples."""
    if not samples:
        return None
    out: dict[str, Any] = {"n": len(samples)}
    for field in FIELDS:
        vals = [s[field] for s in samples if s.get(field) is not None]
        out[field] = {"avg": round(sum(vals) / len(vals), 1), "peak": max(vals)} if vals else None
    for field in ("vram_total", "ram_total"):
        vals = [s[field] for s in samples if s.get(field) is not None]
        out[field] = max(vals) if vals else None
    return out


def in_windows(
    samples: Iterable[dict[str, Any]], windows: Iterable[tuple[float, float]]
) -> list[dict[str, Any]]:
    spans = list(windows)
    return [s for s in samples if any(a <= s["ts"] <= b for a, b in spans)]


def summarize_hardware(
    samples: Sequence[dict[str, Any]], windows: dict[str, list[tuple[float, float]]]
) -> dict[str, Any]:
    """Numbers of the attempts only (the cold-start request and the judge are left out):
    {"run": stats, "scenarios": {scenario id: stats}}."""
    every = [w for spans in windows.values() for w in spans]
    return {
        "run": stats(in_windows(samples, every)),
        "scenarios": {sid: stats(in_windows(samples, spans)) for sid, spans in windows.items()},
    }


def thin(rows: Sequence[dict[str, Any]], limit: int = MAX_POINTS) -> list[dict[str, Any]]:
    """At most `limit` points: neighbours are averaged into buckets."""
    if len(rows) <= limit:
        return list(rows)
    size = -(-len(rows) // limit)
    out = []
    for i in range(0, len(rows), size):
        chunk = rows[i : i + size]
        point: dict[str, Any] = {"ts": round(sum(r["ts"] for r in chunk) / len(chunk), 2)}
        for col in COLUMNS[1:]:
            vals = [r[col] for r in chunk if r.get(col) is not None]
            point[col] = round(sum(vals) / len(vals), 1) if vals else None
        out.append(point)
    return out


def columnar(rows: Sequence[dict[str, Any]]) -> dict[str, list[Any]]:
    return {col: [r.get(col) for r in rows] for col in COLUMNS}
