"""The hardware load of a benchmark run: the sampler, the per-attempt and per-run numbers, the
"is the LLM on this host" verdict, the API and the setting that switches it off."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.bench import hardware as hw
from ai_server_monitor.bench import service as service_mod
from ai_server_monitor.collectors.base import Collector
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .fake_llm import CANNED_REPORT
from .test_bench_run import StubSystem, probe_transport, wait_done
from .test_chat import FakeLLM, say

GB = 2**30


def gpu_data(*, process: str | None = "llama-server", used: int = 10 * GB) -> dict[str, Any]:
    procs = [{"pid": 7, "name": process, "mem_used": 8 * GB}] if process else []
    procs.append({"pid": 9, "name": "Xorg", "mem_used": GB})
    return {
        "driver": "580",
        "gpus": [
            {
                "name": "RTX 3090",
                "util_percent": 80,
                "mem_used": used,
                "mem_total": 24 * GB,
                "power_w": 250.0,
                "temperature_c": 66,
                "processes": procs,
            }
        ],
    }


class FakeGPU(Collector):
    name = "gpu"
    process: str | None = "llama-server"

    async def collect(self) -> dict[str, Any]:
        return gpu_data(process=self.process)


class OtherProcessGPU(FakeGPU):
    process = "comfyui"


def sample(ts: float, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {c: None for c in hw.COLUMNS}
    base.update(ts=ts, vram_total=24 * GB, ram_total=32 * GB)
    base.update(kw)
    return base


# --------------------------------------------------------------------------- units


def test_read_gpu_names_the_llm_process_and_its_own_vram() -> None:
    out = hw.read_gpu(gpu_data())
    assert out["llm_process"] == "llama-server" and out["llm_vram"] == 8 * GB
    assert out["vram_used"] == 10 * GB and out["gpu_util"] == 80 and out["power_w"] == 250.0
    other = hw.read_gpu(gpu_data(process=None))
    assert other["llm_process"] is None and other["llm_vram"] is None
    assert hw.read_gpu(None)["gpus"] == 0 and hw.read_gpu({"gpus": []})["gpus"] == 0


def test_read_gpu_adds_up_several_gpus() -> None:
    two = {"gpus": [gpu_data()["gpus"][0], gpu_data(used=4 * GB)["gpus"][0]]}
    out = hw.read_gpu(two)
    assert out["vram_used"] == 14 * GB and out["vram_total"] == 48 * GB
    assert out["llm_vram"] == 16 * GB and out["power_w"] == 500.0 and out["gpu_util"] == 80


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://[::1]:8080/v1", True),
        ("http://host.docker.internal:11434/v1", True),
        ("https://ollama.example.com/v1", False),
        ("http://192.168.1.20:11434/v1", False),
        ("", False),
    ],
)
def test_endpoint_is_local(url: str, local: bool) -> None:
    assert hw.endpoint_is_local(url) is local


def test_stats_peak_and_average_and_empty() -> None:
    rows = [sample(1, vram_used=4 * GB, cpu=10.0), sample(2, vram_used=8 * GB, cpu=30.0)]
    out = hw.stats(rows)
    assert out["n"] == 2 and out["vram_used"] == {"avg": 6 * GB, "peak": 8 * GB}
    assert out["cpu"] == {"avg": 20.0, "peak": 30.0} and out["gpu_util"] is None
    assert out["vram_total"] == 24 * GB
    assert hw.stats([]) is None


def test_summary_counts_only_the_attempt_windows() -> None:
    rows = [sample(1, cpu=90.0), sample(5, cpu=10.0), sample(6, cpu=20.0), sample(9, cpu=95.0)]
    out = hw.summarize_hardware(rows, {"a": [(4.0, 7.0)], "b": [(8.0, 10.0)]})
    assert out["run"]["n"] == 3 and out["run"]["cpu"]["peak"] == 95.0
    assert out["scenarios"]["a"]["cpu"] == {"avg": 15.0, "peak": 20.0}
    assert out["scenarios"]["b"]["n"] == 1
    assert hw.summarize_hardware(rows, {"c": [(100.0, 101.0)]})["scenarios"]["c"] is None


def test_thin_keeps_short_series_and_averages_long_ones() -> None:
    short = [sample(i, cpu=float(i)) for i in range(5)]
    assert hw.thin(short) == short
    long = [sample(i, cpu=float(i)) for i in range(1500)]
    out = hw.thin(long, 500)
    assert len(out) <= 500 and out[0]["ts"] < out[-1]["ts"]
    assert out[0]["cpu"] == pytest.approx(1.0, abs=0.6)
    cols = hw.columnar(out)
    assert set(cols) == set(hw.COLUMNS) and len(cols["ts"]) == len(out)


async def test_sampler_reads_gpu_cpu_and_ram_and_survives_a_failing_gpu_read() -> None:
    class Flaky(FakeGPU):
        calls = 0

        async def collect(self) -> dict[str, Any]:
            Flaky.calls += 1
            if Flaky.calls == 2:
                raise RuntimeError("NVML went away")
            return await super().collect()

    gpu = Flaky(None, None)  # type: ignore[arg-type]
    prev = hw.psutil.cpu_times()
    sampler = hw.HardwareSampler(None, 1, gpu, 0.5)  # type: ignore[arg-type]
    first = await sampler.sample(prev, hw.psutil.cpu_times())
    second = await sampler.sample(prev, hw.psutil.cpu_times())
    assert first["vram_used"] == 10 * GB and first["llm_vram"] == 8 * GB
    assert second["vram_used"] is None and second["ram_used"] > 0
    assert first["cpu"] is None or 0 <= first["cpu"] <= 100
    assert sampler.llm_process == "llama-server" and sampler.gpu_name == "RTX 3090"


def test_describe_verdicts() -> None:
    s = hw.HardwareSampler(None, 1, None, 1.0)  # type: ignore[arg-type]
    assert s.describe("https://far.example.com/v1")["llm_here"] == "elsewhere"
    assert s.describe("http://localhost:11434/v1")["llm_here"] == "unknown"
    assert s.describe("https://far.example.com/v1")["gpu"] is False
    s.llm_process, s.gpu_count = "ollama", 1
    assert s.describe("https://far.example.com/v1")["llm_here"] == "here"


# --------------------------------------------------------------------------- whole run


def slow(llm: FakeLLM, delay: float = 0.12) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        return llm.handler(request)

    return httpx.MockTransport(handler)


@pytest.fixture
def fast_sampling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "MIN_INTERVAL_S", 0.02)
    monkeypatch.setattr(hw, "FIRST_SAMPLE_DELAY_S", 0.02)
    monkeypatch.setattr(service_mod.BenchService, "warmup_enabled", False)


def make_app(
    bootstrap: Bootstrap, gpu: type[Collector] | None, bench: dict[str, Any] | None = None
) -> Any:
    registry: dict[str, type[Collector]] = {"system": StubSystem}
    if gpu is not None:
        registry["gpu"] = gpu
    settings = Settings.model_validate(
        {
            "llm": {"base_url": LLM_URL, "model": "fake-model", "max_retries": 0},
            "bench": {"hardware_interval": "20ms", **(bench or {})},
        }
    )
    return create_app(
        bootstrap,
        llm_transport=slow(FakeLLM(say(CANNED_REPORT))),
        collector_registry=registry,
        seed_settings=settings,
    )


def run(client: TestClient, app: Any) -> dict[str, Any]:
    app.state.ctx.bench._probe_transport = probe_transport()
    response = client.post("/api/bench/runs", json={"ids": ["report.smoke_disk"], "repeats": 2})
    assert response.status_code == 202, response.text
    wait_done(client)
    return client.get(f"/api/bench/runs/{response.json()['run_id']}").json()


@pytest.mark.usefixtures("fast_sampling")
def test_run_records_samples_and_numbers_per_attempt_scenario_and_run(
    bootstrap: Bootstrap,
) -> None:
    app = make_app(bootstrap, FakeGPU)
    with TestClient(app) as client:
        done = run(client, app)
        assert done["status"] == "done", done
        meta = done["meta"]["hardware"]
        assert meta["recorded"] and meta["gpu"] and meta["gpu_name"] == "RTX 3090"
        assert meta["llm_process"] == "llama-server" and meta["llm_here"] == "here"
        assert meta["samples"] >= 3 and meta["baseline"]["vram_used"] == 10 * GB
        for attempt in done["attempts"]:
            assert attempt["hw"]["n"] >= 1
            assert attempt["hw"]["llm_vram"]["peak"] == 8 * GB
            assert attempt["hw"]["gpu_util"] == {"avg": 80.0, "peak": 80}
        total = done["summary"]["hardware"]
        assert total["n"] >= 2 and total["power_w"]["peak"] == 250.0
        assert total["ram_used"]["peak"] > 0 and total["vram_total"] == 24 * GB
        assert done["summary"]["scenarios"]["report.smoke_disk"]["hw"]["n"] == total["n"]

        # the runs list carries what the Hardware column needs: the recording's own description
        # (without the rest of the meta) next to the run-wide numbers
        (listed,) = client.get("/api/bench/runs").json()["items"]
        assert listed["hardware_meta"] == meta and "meta" not in listed
        assert listed["summary"]["hardware"] == total

        series = client.get(f"/api/bench/runs/{done['id']}/hardware").json()
        assert series["recorded"] and series["count"] == meta["samples"]
        pts = series["points"]
        assert len(pts["ts"]) == series["count"] and pts["ts"] == sorted(pts["ts"])
        assert set(pts["vram_used"]) == {10 * GB}
        assert pts["cpu"][-1] is None or 0 <= pts["cpu"][-1] <= 100


@pytest.mark.usefixtures("fast_sampling")
def test_llm_elsewhere_is_said_so_not_passed_off_as_the_models_usage(
    bootstrap: Bootstrap,
) -> None:
    app = make_app(bootstrap, OtherProcessGPU)
    with TestClient(app) as client:
        meta = run(client, app)["meta"]["hardware"]
        assert meta["gpu"] is True and meta["llm_process"] is None
        assert meta["llm_here"] == "elsewhere"  # LLM_URL is not a local address


@pytest.mark.usefixtures("fast_sampling")
def test_no_gpu_on_this_host_still_records_cpu_and_ram(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, None)
    with TestClient(app) as client:
        done = run(client, app)
        assert done["meta"]["hardware"]["gpu"] is False
        total = done["summary"]["hardware"]
        assert total["vram_used"] is None and total["gpu_util"] is None
        assert total["cpu"] is not None and total["ram_used"]["peak"] > 0


@pytest.mark.usefixtures("fast_sampling")
def test_switched_off_records_nothing_and_old_runs_say_not_recorded(
    bootstrap: Bootstrap,
) -> None:
    app = make_app(bootstrap, FakeGPU, {"record_hardware": False})
    with TestClient(app) as client:
        done = run(client, app)
        assert done["meta"]["hardware"] == {"recorded": False}
        assert "hardware" not in done["summary"]
        assert all(a["hw"] is None for a in done["attempts"])
        series = client.get(f"/api/bench/runs/{done['id']}/hardware").json()
        assert series["recorded"] is False and series["count"] == 0
        assert series["points"]["ts"] == []
        assert client.get("/api/bench/runs/999/hardware").status_code == 404


@pytest.mark.usefixtures("fast_sampling")
def test_deleting_a_run_deletes_its_samples(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeGPU)
    with TestClient(app) as client:
        done = run(client, app)
        assert client.delete(f"/api/bench/runs/{done['id']}").status_code == 204
        with sqlite3.connect(bootstrap.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM bench_hw_samples").fetchone() == (0,)
