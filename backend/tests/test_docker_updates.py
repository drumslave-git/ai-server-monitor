"""The `docker:updates` command (`jobs/builtins.py` DOCKER_UPDATES) run by bash against a fake
`docker` and a fake `curl`: the registry digest comes from the daemon's
`GET /distribution/<image>/json`, asked at the same $DOCKER_HOST as the docker commands.
Invented image names and addresses only."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_server_monitor.jobs.builtins import DOCKER_UPDATES

pytestmark = pytest.mark.skipif(os.name != "posix", reason="runs the command with bash")

DOCKER = """#!/bin/bash
case "$1" in
  ps) if [ -n "$FAKE_IMAGES" ]; then printf '%s\\n' $FAKE_IMAGES; else
        printf '%s\\n' registry.example.invalid/team/app:1.0 invented/cache:2 local-build:dev \\
          invented/cache:2; fi;;
  image) case "${@: -1}" in
      registry.example.invalid/team/app:1.0) echo "registry.example.invalid/team/app@sha256:aaa1";;
      invented/cache:2) echo "invented/cache@sha256:bbb1";;
      *same*|*update*) echo "invented/any@sha256:aaa1";;
    esac;;
esac
"""

CURL = """#!/bin/bash
printf '%s\\n' "$*" >> "$CURL_LOG"
if [ -n "$PROXY_403" ]; then
  printf '<html><body><h1>403 Forbidden</h1>\\nRequest forbidden by administrative rules.\\n'
  printf '</body></html>\\n'
  exit 0
fi
desc() {  # the daemon's answer, fields in its order
  printf '{"Descriptor":{"mediaType":"application/vnd.oci.image.index.v1+json",'
  printf '"digest":"%s","size":10},"Platforms":[{"architecture":"amd64","os":"linux"}]}\\n' "$1"
}
case "${@: -1}" in
  */registry.example.invalid/team/app:1.0/json) desc sha256:aaa1;;
  */invented/cache:2/json) desc sha256:ccc2;;
  *same*) desc sha256:aaa1;;
  *update*) desc sha256:ccc2;;
  *) echo '{"message":"errors: denied: requested access to the resource is denied"}';;
esac
"""


def bin_dir(tmp_path: Path, *, curl: bool = True) -> Path:
    """Only the programs the command uses: the fakes plus the host's sort, sed, head, awk."""
    out = tmp_path / "bin"
    out.mkdir()
    for name in ("sort", "sed", "head", "awk"):
        found = shutil.which(name)
        assert found, name
        (out / name).symlink_to(found)
    fakes = {"docker": DOCKER, "curl": CURL} if curl else {"docker": DOCKER}
    for name, text in fakes.items():
        (out / name).write_text(text)
        (out / name).chmod(0o755)
    return out


def run(tmp_path: Path, *, curl: bool = True, **env: str) -> tuple[list[str], list[str]]:
    log = tmp_path / "curl.log"
    path = str(bin_dir(tmp_path, curl=curl))
    done = subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-c", DOCKER_UPDATES],
        env={"PATH": path, "CURL_LOG": str(log), **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    calls = log.read_text().splitlines() if log.exists() else []
    return done.stdout.splitlines(), calls


@pytest.mark.parametrize(
    ("docker_host", "expected"),
    [
        (
            "tcp://192.0.2.10:2375",
            "-sS http://192.0.2.10:2375/distribution/invented/cache:2/json",
        ),
        (
            "unix:///run/invented/docker.sock",
            "-sS --unix-socket /run/invented/docker.sock "
            "http://localhost/distribution/invented/cache:2/json",
        ),
        (
            None,  # unset: Docker's own default socket, as the docker commands use
            "-sS --unix-socket /var/run/docker.sock "
            "http://localhost/distribution/invented/cache:2/json",
        ),
    ],
)
def test_the_digest_comes_from_the_daemon_at_docker_host(
    tmp_path: Path, docker_host: str | None, expected: str
) -> None:
    env = {"DOCKER_HOST": docker_host} if docker_host else {}
    lines, calls = run(tmp_path, **env)
    assert lines == [
        "newer    invented/cache:2",
        "unknown  local-build:dev: "
        '{"message":"errors: denied: requested access to the resource is denied"}',
        "current  registry.example.invalid/team/app:1.0",
        "counts: 1 current, 1 newer, 1 unknown",
    ]
    assert calls[0] == expected
    assert len(calls) == 3  # each image once, nothing else asked


def test_a_proxy_without_distribution_gives_unknown_with_its_answer(tmp_path: Path) -> None:
    lines, _ = run(tmp_path, DOCKER_HOST="tcp://192.0.2.10:2375", PROXY_403="1")
    images = ("invented/cache:2", "local-build:dev", "registry.example.invalid/team/app:1.0")
    assert lines == [
        *(f"unknown  {image}: <html><body><h1>403 Forbidden</h1>" for image in images),
        "counts: 0 current, 0 newer, 3 unknown",
    ]


def test_without_curl_every_image_is_unknown_with_the_shells_error(tmp_path: Path) -> None:
    lines, calls = run(tmp_path, curl=False, DOCKER_HOST="tcp://192.0.2.10:2375")
    assert calls == []
    assert len(lines) == 4
    assert all(line.startswith("unknown  ") for line in lines[:3])
    assert all(line.endswith("curl: command not found") for line in lines[:3])
    assert lines[3] == "counts: 0 current, 0 newer, 3 unknown"


def test_newer_then_unknown_then_current_and_the_counts_last(tmp_path: Path) -> None:
    """Many images of mixed result: the lines the model must act on come first, whatever the
    image names, and it copies the counts instead of counting the lines."""
    names = [f"invented/{kind}-{n:02}:1" for n in range(14) for kind in ("same", "update", "own")]
    lines, _ = run(tmp_path, FAKE_IMAGES=" ".join(names), DOCKER_HOST="tcp://192.0.2.10:2375")
    kinds = [line.split()[0] for line in lines[:-1]]
    assert kinds == ["newer"] * 14 + ["unknown"] * 14 + ["current"] * 14
    assert lines[0] == "newer    invented/update-00:1"
    assert lines[14].startswith("unknown  invented/own-00:1: ")
    assert lines[28] == "current  invented/same-00:1"
    assert lines[-1] == "counts: 14 current, 14 newer, 14 unknown"


def test_no_running_container_still_prints_the_counts(tmp_path: Path) -> None:
    lines, calls = run(tmp_path, FAKE_IMAGES=" ", DOCKER_HOST="tcp://192.0.2.10:2375")
    assert lines == ["counts: 0 current, 0 newer, 0 unknown"]
    assert calls == []
