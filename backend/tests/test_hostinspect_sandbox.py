"""The command sandbox itself: pipelines, deadlines, output caps and, when run as root on
Linux (as the monitor does in its container), the actual isolation: chroot into the host
root, unprivileged uid, no capabilities, nothing writable.

The root tests are skipped for a normal user; run them with
`docker run --rm -u 0 ... pytest tests/test_hostinspect_sandbox.py`."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ai_server_monitor.hostinspect.commands import parse_pipeline
from ai_server_monitor.hostinspect.sandbox import Sandbox, host_identity

posix_only = pytest.mark.skipif(os.name != "posix", reason="needs Linux")
root_only = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0, reason="needs root (the monitor runs as root)"
)


def _running(cmdline: str) -> bool:
    """Is a process with this (NUL-separated) command line alive? Zombies don't count."""
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if (
                    cmdline in (entry / "cmdline").read_text()
                    and "Z" not in (entry / "stat").read_text().split(")")[-1][:3]
                ):
                    return True
            except OSError:
                continue
    return False


def run(sb: Sandbox, line: str, **kw: object):  # type: ignore[no-untyped-def]
    return sb.run(parse_pipeline(line).stages, **kw)  # type: ignore[arg-type]


@posix_only
def test_pipeline_wires_stages_and_reports_exit_code() -> None:
    sb = Sandbox(Path("/"))
    result = sb.run([["seq", "1", "20"], ["sort", "-rn"], ["head", "-n", "3"]])
    assert result.output.split() == ["20", "19", "18"]
    assert result.exit_code == 0
    assert not result.timed_out and result.stopped is None


@posix_only
def test_exit_code_stderr_and_missing_command() -> None:
    sb = Sandbox(Path("/"))
    bad = sb.run([["ls", "/definitely/not/here"]])
    assert bad.exit_code != 0 and "No such file" in bad.stderr
    gone = sb.run([["no-such-binary-xyz"]])
    assert gone.exit_code == 127 and "not found" in gone.stderr


@posix_only
def test_deadline_kills_the_whole_process_group() -> None:
    sb = Sandbox(Path("/"))
    started = time.monotonic()
    result = sb.run([["sleep", "30"], ["cat"]], timeout=1.0)
    assert time.monotonic() - started < 5
    assert result.timed_out and result.stopped == "timeout"
    assert result.exit_code is None
    assert not _running("sleep\x0030")


@posix_only
def test_output_cap_keeps_head_and_tail() -> None:
    sb = Sandbox(Path("/"))
    result = sb.run([["seq", "1", "100000"]], max_chars=2000)
    assert result.truncated
    assert result.output.startswith("1\n")
    assert result.output.rstrip().endswith("100000")
    assert "lines cut" in result.output
    assert len(result.output) < 2300
    assert result.output_bytes > 500_000


@posix_only
def test_runaway_output_is_stopped() -> None:
    sb = Sandbox(Path("/"))
    started = time.monotonic()
    result = sb.run([["cat", "/dev/zero"]], timeout=20, max_chars=1000)
    assert result.stopped == "output limit"
    assert time.monotonic() - started < 15
    assert len(result.output) < 1500


@posix_only
def test_collect_all_keeps_the_start_up_to_the_limit() -> None:
    sb = Sandbox(Path("/"))
    result = sb.run([["seq", "1", "1000"]], max_chars=100_000, collect_all=True)
    assert not result.truncated
    assert result.output.count("\n") == 1000


def test_identity_from_the_hosts_own_files(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc/passwd").write_text("root:x:0:0::/root:/bin/sh\nnobody:x:65500:65501::/:/x\n")
    (tmp_path / "etc/group").write_text("adm:x:4:\nsystemd-journal:x:999:\nnogroup:x:65501:\n")
    ident = host_identity(tmp_path)
    assert (ident.uid, ident.gid) == (65500, 65501)
    assert ident.groups == (65501, 4, 999)
    fallback = host_identity(tmp_path / "missing")
    assert (fallback.uid, fallback.gid, fallback.groups) == (65534, 65534, (65534,))


# --------------------------------------------------------------------------- isolation (root)


def _fake_host(root: Path, binaries: list[str]) -> None:
    """A tiny host root: the given binaries with their shared libraries, and passwd/group."""
    for name in binaries:
        binary = shutil.which(name)
        assert binary, name
        targets = {binary}
        for line in subprocess.run(
            ["ldd", binary], capture_output=True, text=True
        ).stdout.splitlines():
            for part in line.split():
                if part.startswith("/") and os.path.exists(part):
                    targets.add(part)
        for target in targets:
            dest = root / target.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)  # follows symlinks: the host root has real files only
    (root / "etc").mkdir(exist_ok=True)
    (root / "etc/passwd").write_text("root:x:0:0::/root:/bin/sh\nnobody:x:65500:65501::/:/x\n")
    (root / "etc/group").write_text("adm:x:4:\nnogroup:x:65501:\nsystemd-journal:x:998:\n")
    (root / "etc/marker").write_text("this is the fake host\n")
    (root / "var/log").mkdir(parents=True, exist_ok=True)
    (root / "var/log/syslog").write_text("Nov 15 logrotate: error\n")
    for path in (root, root / "etc", root / "var", root / "var/log"):
        path.chmod(0o755)
    (root / "usr/lib").mkdir(parents=True, exist_ok=True)


@pytest.fixture(scope="module")
def host(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("fakehost")
    _fake_host(root, ["ls", "cat", "id", "grep", "touch", "sh", "sort", "head", "tail", "wc"])
    return root


@root_only
class TestIsolation:
    def test_runs_in_the_host_root_as_the_hosts_nobody(self, host: Path) -> None:
        sb = Sandbox(host)
        assert sb.isolated
        out = run(sb, "id").output
        assert "uid=65500" in out and "gid=65501" in out
        assert "4(" in out and "998" in out  # adm and systemd-journal from the host's group file
        assert run(sb, "cat /etc/marker").output == "this is the fake host\n"
        # the container's own filesystem is not visible
        assert "etc" in run(sb, "ls /").output
        assert run(sb, "ls /app").exit_code != 0
        assert "logrotate" in run(sb, "grep -r logrotate /var/log").output

    def test_no_capabilities_no_new_privs(self, host: Path) -> None:
        real = Sandbox(Path("/")).run(
            [["grep", "-E", "^(Cap|NoNewPrivs|Uid)", "/proc/self/status"]]
        )
        lines = dict(line.split(":\t") for line in real.output.splitlines())
        for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
            assert lines[key].strip() == "0" * 16, key
        assert lines["NoNewPrivs"].strip() == "1"
        assert lines["Uid"].split()[0] == str(Sandbox(Path("/")).identity.uid)

    def test_nothing_is_writable(self, host: Path) -> None:
        sb = Sandbox(host)
        for target in ("/etc/new", "/var/log/new", "/new", "/usr/new"):
            result = sb.run([["touch", target]])
            assert result.exit_code != 0, target
        assert not (host / "etc/new").exists() and not (host / "new").exists()
        result = sb.run([["sh", "-c", "echo x > /etc/marker"]])
        assert result.exit_code != 0
        assert (host / "etc/marker").read_text() == "this is the fake host\n"

    def test_cannot_become_root_again(self, host: Path) -> None:
        sb = Sandbox(host)
        result = sb.run([["sh", "-c", "id -u; kill -0 1 2>&1; echo done"]])
        assert result.output.split()[0] == "65500"

    def test_timeout_kills_unprivileged_children(self, host: Path) -> None:
        sb = Sandbox(Path("/"))
        result = sb.run([["sleep", "30"]], timeout=1.0)
        assert result.timed_out
        assert not _running("sleep\x0030")

    def test_real_root_path_works_too(self) -> None:
        sb = Sandbox(Path("/"))
        assert run(sb, "ls /etc | head -n 2").exit_code == 0
        result = sb.run([["sh", "-c", "touch /etc/asm-sandbox-test"]])
        assert result.exit_code != 0
        assert not Path("/etc/asm-sandbox-test").exists()
