"""The `bash` tool's sandbox runner. The isolated (root) path is measured in a deployment-like
container (docs/DESIGN.md, "Security boundaries"); here: its command line, and real bash runs."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_server_monitor.agent import shell as shell_module
from ai_server_monitor.agent.shell import (
    CAP_NOTE,
    SANDBOX_UID,
    HostShell,
    ShellResult,
    capability_bounded,
    group_ids,
    resolve_docker_host,
)

linux_only = pytest.mark.skipif(os.name != "posix", reason="runs real bash")


def host_root(tmp_path: Path) -> Path:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/group").write_text("root:x:0:\nadm:x:4:syslog\nsystemd-journal:x:999:\n")
    for name in ("env", "bash", "prlimit", "nice", "ionice", "setpriv"):
        binary = tmp_path / "usr/bin" / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    return tmp_path


def isolated(monkeypatch: pytest.MonkeyPatch, *, read_all: bool) -> None:
    monkeypatch.setattr(HostShell, "isolated", property(lambda self: True))
    monkeypatch.setattr(HostShell, "read_all", property(lambda self: read_all))


def test_isolated_command_line_keeps_only_dac_read_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = host_root(tmp_path)
    isolated(monkeypatch, read_all=True)
    argv = HostShell(root, docker_host="tcp://10.1.2.3:2375").argv("df -h | head", 30)
    assert argv[1] == str(root) and argv[2:4] == ["/usr/bin/env", "-i"]
    assert "DOCKER_HOST=tcp://10.1.2.3:2375" in argv and "PATH=" in " ".join(argv)
    assert argv[argv.index("prlimit") + 1] == "--cpu=35" and "--fsize=0" in argv
    setpriv = argv[argv.index("setpriv") :]
    assert f"--reuid={SANDBOX_UID}" in setpriv and f"--regid={SANDBOX_UID}" in setpriv
    assert "--clear-groups" in setpriv and "--no-new-privs" in setpriv
    for flag in ("--inh-caps", "--ambient-caps", "--bounding-set"):
        assert f"{flag}=-all,+dac_read_search" in setpriv
    assert argv[-3:] == ["bash", "-c", "df -h | head"]


def test_without_the_capability_it_is_a_plain_user_with_the_log_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = host_root(tmp_path)
    isolated(monkeypatch, read_all=False)
    argv = HostShell(root).argv("ls", 300)
    assert "--groups=4,999" in argv and "--clear-groups" not in argv
    assert "--ambient-caps=-all" in argv and "--bounding-set=-all" in argv
    assert not [a for a in argv if "dac_read_search" in a]
    assert HostShell(root).missing_tools() == []
    (root / "usr/bin/setpriv").unlink()
    assert HostShell(root).missing_tools() == ["setpriv"]


def test_capability_and_group_lookups(tmp_path: Path) -> None:
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nCapBnd:\t00000000a80425fb\n")
    assert not capability_bounded(2, status)  # Docker's default set: no DAC_READ_SEARCH
    status.write_text("CapBnd:\t00000000a80425ff\n")
    assert capability_bounded(2, status)
    assert not capability_bounded(2, tmp_path / "missing")
    assert group_ids(host_root(tmp_path), ("systemd-journal", "nope", "adm")) == [999, 4]


def test_docker_host_names_are_resolved_for_the_chroot() -> None:
    assert resolve_docker_host(None) is None
    assert resolve_docker_host("unix:///var/run/docker.sock") == "unix:///var/run/docker.sock"
    resolved = resolve_docker_host("tcp://localhost:2375")
    assert resolved in ("tcp://127.0.0.1:2375", "tcp://[::1]:2375")
    assert resolve_docker_host("tcp://no-such-host.invalid:2375") == (
        "tcp://no-such-host.invalid:2375"
    )


def test_result_text_says_how_it_ended() -> None:
    assert ShellResult(0, "hi\n", 0.25).text() == "hi\n[exit code 0, 0.2s]"
    assert "(no output)" in ShellResult(1, "", 0.1).text()
    stopped = ShellResult(None, "partial", 30.0, stopped="timeout").text()
    assert "stopped after 30s" in stopped and "timeout_s" in stopped
    noted = ShellResult(1, "du: Permission denied", 1.0, note=CAP_NOTE).text()
    assert noted.endswith(CAP_NOTE) and "cap_add: [DAC_READ_SEARCH]" in noted


# --------------------------------------------------------------------------- real bash (Linux)


@linux_only
async def test_probe_real_bash_pipes_exit_codes_and_merged_stderr(tmp_path: Path) -> None:
    shell = HostShell(tmp_path)
    if shell.isolated:
        pytest.skip("as root the shell chroots into the host root")
    result = await shell.run(
        "printf 'b\\na\\n' | sort; echo oops >&2; (exit 3)", timeout_s=10, max_chars=1000
    )
    assert result.exit_code == 3 and result.stopped is None
    assert result.output.splitlines() == ["a", "b", "oops"]
    assert "exit code 3" in result.text() and result.note is None


@linux_only
async def test_probe_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    shell = HostShell(tmp_path)
    if shell.isolated:
        pytest.skip("as root the shell chroots into the host root")
    result = await shell.run("echo started; sleep 30 & sleep 30", timeout_s=0.5, max_chars=1000)
    assert result.stopped == "timeout" and result.exit_code is None
    assert result.output.strip() == "started" and result.duration_s < 10


@linux_only
async def test_probe_long_output_keeps_head_and_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = HostShell(tmp_path)
    if shell.isolated:
        pytest.skip("as root the shell chroots into the host root")
    result = await shell.run("seq 1 20000", timeout_s=10, max_chars=1000)
    assert result.exit_code == 0 and result.truncated
    lines = result.output.splitlines()
    assert lines[0] == "1" and lines[-1] == "20000" and "lines cut" in result.output
    monkeypatch.setattr(shell_module, "MAX_STREAM_BYTES", 1000)
    flood = await shell.run("yes", timeout_s=10, max_chars=500)
    assert flood.stopped == "output limit"


@pytest.mark.skipif(os.name != "posix", reason="process groups and uvloop are POSIX only")
def test_processes_run_under_uvloop_as_in_the_image() -> None:
    """The image serves with uvicorn[standard], i.e. uvloop, whose subprocess_exec rejects
    some asyncio arguments (`process_group`): the shell must start and time out there too."""
    uvloop = pytest.importorskip("uvloop")
    env = {"PATH": "/usr/bin:/bin"}

    async def both() -> tuple[ShellResult, ShellResult]:
        done = await shell_module.run_process(["sh", "-c", "echo hi"], env, 10, 1000)
        slow = await shell_module.run_process(["sh", "-c", "sleep 30"], env, 0.5, 1000)
        return done, slow

    done, slow = uvloop.run(both())
    assert done.exit_code == 0 and done.output == "hi\n"
    assert slow.stopped == "timeout" and slow.duration_s < 10
