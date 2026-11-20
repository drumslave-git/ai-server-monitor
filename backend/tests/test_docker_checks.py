"""The `docker:status` and `docker:resources` commands (`jobs/builtins.py` DOCKER_STATUS,
DOCKER_RESOURCES) run by bash against a fake `docker`: the command sorts and counts, so the
model reads the top lines and copies the counts. Invented container names only."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_server_monitor.jobs.builtins import DOCKER_RESOURCES, DOCKER_STATUS, TOPICS

pytestmark = pytest.mark.skipif(os.name != "posix", reason="runs the command with bash")

PS = [
    "invented-web\tUp 3 hours (healthy)\tinvented/web:1",
    "invented-once\tExited (0) 2 days ago\tinvented/once:1",
    "invented-db\tUp 2 minutes (unhealthy)\tinvented/db:16",
    "invented-cache\tUp 3 hours\tinvented/cache:7",
    "invented-worker\tExited (137) 5 minutes ago\tinvented/worker:2",
    "invented-new\tCreated\tinvented/new:1",
    "invented-proxy\tRestarting (1) 4 seconds ago\tinvented/proxy:3",
    "invented-paused\tUp 1 hour (Paused)\tinvented/paused:1",
    "invented-starting\tUp 5 seconds (health: starting)\tinvented/starting:1",
]

STATS = [
    "invented-web\t1.50%\t120MiB / 15.6GiB\t0.75%",
    "invented-db\t150.20%\t1.9GiB / 2GiB\t95.10%",
    "invented-cache\t0.00%\t8MiB / 15.6GiB\t0.05%",
    "invented-worker\t12.00%\t460MiB / 500MiB\t92.00%",
    "invented-proxy\t3.25%\t40MiB / 15.6GiB\t0.25%",
]


def run(tmp_path: Path, command: str, ps: list[str], stats: list[str]) -> list[str]:
    """`command` with only the fake docker plus the host's sort, awk and cat on the PATH."""
    out = tmp_path / "bin"
    out.mkdir()
    for name in ("sort", "awk", "cat"):
        found = shutil.which(name)
        assert found, name
        (out / name).symlink_to(found)
    (tmp_path / "ps").write_text("".join(f"{line}\n" for line in ps))
    (tmp_path / "stats").write_text("".join(f"{line}\n" for line in stats))
    docker = out / "docker"
    docker.write_text(
        f'#!/bin/bash\ncase "$1" in ps) cat {tmp_path}/ps;; stats) cat {tmp_path}/stats;; esac\n'
    )
    docker.chmod(0o755)
    done = subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-c", command],
        env={"PATH": str(out), "LANG": "de_DE.UTF-8"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.splitlines()


def names(lines: list[str]) -> list[str]:
    return [line.split("\t")[0] for line in lines]


def test_status_puts_the_containers_that_need_a_look_first_and_counts_them(
    tmp_path: Path,
) -> None:
    lines = run(tmp_path, DOCKER_STATUS, PS, [])
    assert sorted(lines[:-1]) == sorted(PS)  # every line, unchanged
    assert names(lines[:-1]) == [
        "invented-db",  # unhealthy
        "invented-proxy",  # restarting
        "invented-worker",  # exited non-zero
        "invented-paused",  # other
        "invented-new",  # created
        "invented-once",  # exited 0
        "invented-web",  # up, in the order docker printed them
        "invented-cache",
        "invented-starting",
    ]
    assert lines[-1] == (
        "counts: 3 up, 1 unhealthy, 1 restarting, 1 exited non-zero, 1 other, 1 created, 1 exited 0"
    )


def test_status_with_every_container_up(tmp_path: Path) -> None:
    lines = run(tmp_path, DOCKER_STATUS, [PS[0], PS[3]], [])
    assert names(lines[:-1]) == ["invented-web", "invented-cache"]
    assert lines[-1] == (
        "counts: 2 up, 0 unhealthy, 0 restarting, 0 exited non-zero, 0 other, 0 created, 0 exited 0"
    )


def test_resources_sorts_by_cpu_then_by_memory_and_counts_those_above_90(
    tmp_path: Path,
) -> None:
    lines = run(tmp_path, DOCKER_RESOURCES, [], STATS)
    at = lines.index("by memory:")
    assert lines[0] == "by CPU:"
    by_cpu, by_memory = lines[1:at], lines[at + 1 : -1]
    assert sorted(by_cpu) == sorted(by_memory) == sorted(STATS)  # unchanged, each once
    assert names(by_cpu) == [
        "invented-db",
        "invented-worker",
        "invented-proxy",
        "invented-web",
        "invented-cache",
    ]
    assert names(by_memory) == [
        "invented-db",
        "invented-worker",
        "invented-web",
        "invented-proxy",
        "invented-cache",
    ]
    assert lines[-1] == "counts: 5 containers, 2 above 90 % of their memory limit"


def test_resources_without_containers(tmp_path: Path) -> None:
    lines = run(tmp_path, DOCKER_RESOURCES, [], [])
    assert lines[0] == "by CPU:" and "by memory:" in lines
    assert lines[-1] == "counts: 0 containers, 0 above 90 % of their memory limit"


def test_the_prompts_quote_the_commands() -> None:
    prompts = {job.slug: job.prompt for job in TOPICS}
    assert DOCKER_STATUS in prompts["docker:status"]
    assert DOCKER_RESOURCES in prompts["docker:resources"]
