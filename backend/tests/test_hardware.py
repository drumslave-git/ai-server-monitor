"""The hardware meters' endpoint: CPU and RAM from psutil, NVIDIA GPUs from one nvidia-smi call
in the jobs' host shell, outside the tool layer. The GPU names and sizes are invented."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent.shell import HostShell, ShellResult
from ai_server_monitor.config import Bootstrap
from ai_server_monitor.hardware import GPU_QUERY, MIB, parse_gpus
from ai_server_monitor.main import create_app

ONE_GPU = "Demo Accelerator 9000, 37, 2048, 16384\n"
TWO_GPUS = "Demo Accelerator 9000, 37, 2048, 16384\nTest Card, Mk II, 5, 512, 8192\n"


def fake_shell(monkeypatch: pytest.MonkeyPatch, result: ShellResult) -> list[str]:
    commands: list[str] = []

    async def run(self: HostShell, command: str, *, timeout_s: float, max_chars: int) -> Any:
        commands.append(command)
        return result

    monkeypatch.setattr(HostShell, "run", run)
    return commands


def test_parse_reads_every_gpu_line_and_keeps_commas_in_the_name() -> None:
    gpus = parse_gpus(TWO_GPUS)
    assert [(g.name, g.util_percent, g.vram_used, g.vram_total) for g in gpus] == [
        ("Demo Accelerator 9000", 37.0, 2048 * MIB, 16384 * MIB),
        ("Test Card, Mk II", 5.0, 512 * MIB, 8192 * MIB),
    ]


def test_parse_leaves_out_lines_whose_numbers_cannot_be_read() -> None:
    gpus = parse_gpus("Demo Accelerator 9000, [N/A], 2048, 16384\nnot a gpu line\n" + ONE_GPU)
    assert [g.name for g in gpus] == ["Demo Accelerator 9000"]
    assert gpus[0].util_percent == 37.0


def test_endpoint_answers_cpu_ram_and_the_gpus_without_audit(
    bootstrap: Bootstrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = fake_shell(monkeypatch, ShellResult(exit_code=0, output=TWO_GPUS, duration_s=0.03))
    with TestClient(create_app(bootstrap)) as client:
        body = client.get("/api/hardware").json()
        audit = client.get("/api/chat/audit").json()["items"]
    assert commands == [GPU_QUERY]
    assert 0 <= body["cpu_percent"] <= 100
    assert 0 < body["ram_used"] <= body["ram_total"]
    assert body["gpus"] == [
        {
            "name": "Demo Accelerator 9000",
            "util_percent": 37.0,
            "vram_used": 2048 * MIB,
            "vram_total": 16384 * MIB,
        },
        {
            "name": "Test Card, Mk II",
            "util_percent": 5.0,
            "vram_used": 512 * MIB,
            "vram_total": 8192 * MIB,
        },
    ]
    assert audit == []


@pytest.mark.parametrize(
    "result",
    [
        ShellResult(exit_code=127, output="bash: nvidia-smi: command not found", duration_s=0.0),
        ShellResult(exit_code=9, output="NVIDIA-SMI has failed", duration_s=0.1),
        ShellResult(None, "the shell needs Linux with bash", 0.0, stopped="error"),
        ShellResult(exit_code=0, output="", duration_s=0.02),
    ],
    ids=["missing", "fails", "no shell", "lists nothing"],
)
def test_no_readable_gpu_answers_an_empty_list(
    bootstrap: Bootstrap, monkeypatch: pytest.MonkeyPatch, result: ShellResult
) -> None:
    fake_shell(monkeypatch, result)
    with TestClient(create_app(bootstrap)) as client:
        body = client.get("/api/hardware").json()
    assert body["gpus"] == []
    assert body["ram_total"] > 0
