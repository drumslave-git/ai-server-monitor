from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from ai_server_monitor.collectors.base import CollectorUnavailable, HostEnv
from ai_server_monitor.collectors.gpu import GPUCollector
from ai_server_monitor.config import CollectorSettings


class NVMLError(Exception):
    pass


def make_fake_nvml(count: int = 1, init_error: bool = False) -> types.ModuleType:
    nvml = types.ModuleType("pynvml")
    state = {"shutdown": False}

    def init() -> None:
        if init_error:
            raise NVMLError("Driver Not Loaded")

    def not_supported(*args: object) -> None:
        raise NVMLError("Not Supported")

    nvml.nvmlInit = init  # type: ignore[attr-defined]
    nvml.nvmlShutdown = lambda: state.update(shutdown=True)  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetCount = lambda: count  # type: ignore[attr-defined]
    nvml.nvmlSystemGetDriverVersion = lambda: b"580.82"  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetHandleByIndex = lambda i: f"h{i}"  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetName = lambda h: "NVIDIA GeForce RTX 4090"  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetUUID = lambda h: "GPU-1234"  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetMemoryInfo = lambda h: SimpleNamespace(  # type: ignore[attr-defined]
        used=12 * 2**30, total=24 * 2**30, free=12 * 2**30
    )
    nvml.nvmlDeviceGetUtilizationRates = lambda h: SimpleNamespace(gpu=87, memory=40)  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetTemperature = lambda h, sensor: 71  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetFanSpeed = not_supported  # type: ignore[attr-defined]  # passively cooled
    nvml.nvmlDeviceGetPowerUsage = lambda h: 312_500  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetEnforcedPowerLimit = lambda h: 450_000  # type: ignore[attr-defined]
    nvml.nvmlDeviceGetComputeRunningProcesses = lambda h: [  # type: ignore[attr-defined]
        SimpleNamespace(pid=999_999_991, usedGpuMemory=10 * 2**30)
    ]
    nvml.nvmlDeviceGetGraphicsRunningProcesses = not_supported  # type: ignore[attr-defined]
    nvml._state = state  # type: ignore[attr-defined]
    return nvml


async def test_collect(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = make_fake_nvml()
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    collector = GPUCollector(CollectorSettings(enabled="auto"), HostEnv())
    await collector.check()
    data = await collector.collect()

    assert data["driver"] == "580.82"
    (gpu,) = data["gpus"]
    assert gpu["name"] == "NVIDIA GeForce RTX 4090"
    assert gpu["util_percent"] == 87 and gpu["mem_percent"] == 50.0
    assert gpu["temperature_c"] == 71
    assert gpu["fan_percent"] is None  # unsupported field doesn't break collection
    assert gpu["power_w"] == 312.5 and gpu["power_limit_w"] == 450.0
    assert gpu["processes"] == [{"pid": 999_999_991, "name": None, "mem_used": 10 * 2**30}]
    await collector.close()
    assert fake._state["shutdown"] is True  # type: ignore[attr-defined]


async def test_unavailable_without_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pynvml", make_fake_nvml(init_error=True))
    with pytest.raises(CollectorUnavailable, match="NVML"):
        await GPUCollector(CollectorSettings(), HostEnv()).check()


async def test_unavailable_without_gpus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pynvml", make_fake_nvml(count=0))
    with pytest.raises(CollectorUnavailable, match="no NVIDIA"):
        await GPUCollector(CollectorSettings(), HostEnv()).check()
