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
      invented/limited:1) echo "invented/limited@sha256:ddd1";;
      invented/gone:1) echo "Error: No such image: invented/gone:1" >&2; exit 1;;
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
  */invented/limited:1/json)
    echo '{"message":"toomanyrequests: retry-after: 1.5ms, allowed: 10/minute"}';;
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
        "newer    invented/cache:2 | local: invented/cache@sha256:bbb1 | registry: sha256:ccc2",
        "local    local-build:dev",
        "current  registry.example.invalid/team/app:1.0",
        "counts: 1 newer, 0 limited, 0 unknown, 1 local, 1 current",
    ]
    assert calls[0] == expected
    assert len(calls) == 2  # each registry image once; the local image has no registry to ask


def test_a_local_image_is_its_own_class_and_no_registry_is_asked(tmp_path: Path) -> None:
    """Fault 2 of the first 0.10.2 Daily: images built on the host showed as registry errors."""
    lines, calls = run(tmp_path, FAKE_IMAGES="local-build:dev", DOCKER_HOST="tcp://192.0.2.10:2375")
    assert lines == [
        "local    local-build:dev",
        "counts: 0 newer, 0 limited, 0 unknown, 1 local, 0 current",
    ]
    assert calls == []


def test_the_registrys_rate_limit_is_its_own_class_with_its_answer(tmp_path: Path) -> None:
    """Fault 3: `toomanyrequests` was one of the "unknown registry errors"."""
    lines, _ = run(
        tmp_path,
        FAKE_IMAGES="invented/limited:1 invented/cache:2 invented/gone:1",
        DOCKER_HOST="tcp://192.0.2.10:2375",
    )
    assert lines == [
        "newer    invented/cache:2 | local: invented/cache@sha256:bbb1 | registry: sha256:ccc2",
        "limited  invented/limited:1: "
        '{"message":"toomanyrequests: retry-after: 1.5ms, allowed: 10/minute"}',
        "unknown  invented/gone:1: Error: No such image: invented/gone:1",
        "counts: 1 newer, 1 limited, 1 unknown, 0 local, 0 current",
    ]


def test_a_proxy_without_distribution_gives_unknown_with_its_answer(tmp_path: Path) -> None:
    lines, _ = run(tmp_path, DOCKER_HOST="tcp://192.0.2.10:2375", PROXY_403="1")
    images = ("invented/cache:2", "registry.example.invalid/team/app:1.0")
    assert lines == [
        *(f"unknown  {image}: <html><body><h1>403 Forbidden</h1>" for image in images),
        "local    local-build:dev",
        "counts: 0 newer, 0 limited, 2 unknown, 1 local, 0 current",
    ]


def test_without_curl_every_registry_image_is_unknown_with_the_shells_error(
    tmp_path: Path,
) -> None:
    lines, calls = run(tmp_path, curl=False, DOCKER_HOST="tcp://192.0.2.10:2375")
    assert calls == []
    assert len(lines) == 4
    assert all(line.startswith("unknown  ") for line in lines[:2])
    assert all(line.endswith("curl: command not found") for line in lines[:2])
    assert lines[2] == "local    local-build:dev"
    assert lines[3] == "counts: 0 newer, 0 limited, 2 unknown, 1 local, 0 current"


def test_newer_then_the_not_checked_then_local_then_current_and_the_counts_last(
    tmp_path: Path,
) -> None:
    """Many images of mixed result: the lines the model must act on come first, whatever the
    image names, and it copies the counts instead of counting the lines."""
    names = [f"invented/{kind}-{n:02}:1" for n in range(14) for kind in ("same", "update", "own")]
    lines, _ = run(tmp_path, FAKE_IMAGES=" ".join(names), DOCKER_HOST="tcp://192.0.2.10:2375")
    kinds = [line.split()[0] for line in lines[:-1]]
    assert kinds == ["newer"] * 14 + ["local"] * 14 + ["current"] * 14
    assert lines[0].startswith("newer    invented/update-00:1 | local: invented/any@sha256:aaa1")
    assert lines[14] == "local    invented/own-00:1"
    assert lines[28] == "current  invented/same-00:1"
    assert lines[-1] == "counts: 14 newer, 0 limited, 0 unknown, 14 local, 14 current"


def test_no_running_container_still_prints_the_counts(tmp_path: Path) -> None:
    lines, calls = run(tmp_path, FAKE_IMAGES=" ", DOCKER_HOST="tcp://192.0.2.10:2375")
    assert lines == ["counts: 0 newer, 0 limited, 0 unknown, 0 local, 0 current"]
    assert calls == []
