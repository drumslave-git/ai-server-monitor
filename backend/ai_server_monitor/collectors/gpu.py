"""NVIDIA GPUs via NVML (nvidia-ml-py). Needs the NVIDIA container runtime with the
`utility` capability (NVIDIA_DRIVER_CAPABILITIES=compute,utility)."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psutil

from .base import Collector, CollectorUnavailable

_nvml_lock = threading.Lock()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class GPUCollector(Collector):
    name = "gpu"
    timeout = timedelta(seconds=20)

    _nvml: Any = None

    def _load(self) -> Any:
        if self._nvml is None:
            try:
                import pynvml
            except ImportError as exc:  # pragma: no cover - dependency is installed
                raise CollectorUnavailable("nvidia-ml-py not installed") from exc
            with _nvml_lock:
                try:
                    pynvml.nvmlInit()
                except Exception as exc:  # NVMLError_LibraryNotFound, DriverNotLoaded, ...
                    raise CollectorUnavailable(f"NVML unavailable: {exc}") from exc
            self._nvml = pynvml
        return self._nvml

    async def check(self) -> None:
        nvml = await asyncio.to_thread(self._load)
        count = await asyncio.to_thread(nvml.nvmlDeviceGetCount)
        if count == 0:
            raise CollectorUnavailable("no NVIDIA GPUs found")

    async def collect(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._collect_sync)

    def _collect_sync(self) -> dict[str, Any]:
        nvml = self._load()

        def safe(fn: Callable[..., Any], *args: Any) -> Any:
            try:
                return fn(*args)
            except Exception:  # NVMLError_NotSupported etc. — field just stays empty
                return None

        gpus: list[dict[str, Any]] = []
        driver = safe(nvml.nvmlSystemGetDriverVersion)
        for index in range(nvml.nvmlDeviceGetCount()):
            handle = nvml.nvmlDeviceGetHandleByIndex(index)
            mem = safe(nvml.nvmlDeviceGetMemoryInfo, handle)
            util = safe(nvml.nvmlDeviceGetUtilizationRates, handle)
            power = safe(nvml.nvmlDeviceGetPowerUsage, handle)
            limit = safe(nvml.nvmlDeviceGetEnforcedPowerLimit, handle)
            procs: dict[int, int | None] = {}
            for getter in ("nvmlDeviceGetComputeRunningProcesses",
                           "nvmlDeviceGetGraphicsRunningProcesses"):  # fmt: skip
                for p in safe(getattr(nvml, getter), handle) or []:
                    procs[p.pid] = getattr(p, "usedGpuMemory", None)
            gpus.append(
                {
                    "index": index,
                    "name": _text(safe(nvml.nvmlDeviceGetName, handle) or "?"),
                    "uuid": _text(safe(nvml.nvmlDeviceGetUUID, handle) or ""),
                    "util_percent": getattr(util, "gpu", None),
                    "mem_util_percent": getattr(util, "memory", None),
                    "mem_used": getattr(mem, "used", None),
                    "mem_total": getattr(mem, "total", None),
                    "mem_percent": round(mem.used / mem.total * 100, 1)
                    if mem and mem.total
                    else None,
                    "temperature_c": safe(nvml.nvmlDeviceGetTemperature, handle, 0),
                    "fan_percent": safe(nvml.nvmlDeviceGetFanSpeed, handle),
                    "power_w": round(power / 1000, 1) if power is not None else None,
                    "power_limit_w": round(limit / 1000, 1) if limit is not None else None,
                    "processes": [
                        {"pid": pid, "name": _proc_name(pid), "mem_used": used}
                        for pid, used in sorted(procs.items(), key=lambda kv: -(kv[1] or 0))
                    ][:10],
                }
            )
        return {"driver": _text(driver) if driver else None, "gpus": gpus}

    async def close(self) -> None:
        if self._nvml is not None:
            with _nvml_lock, contextlib.suppress(Exception):
                self._nvml.nvmlShutdown()
            self._nvml = None


def _proc_name(pid: int) -> str | None:
    try:
        return psutil.Process(pid).name()
    except (psutil.Error, OSError):
        return None
