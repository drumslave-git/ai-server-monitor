"""Running validated commands as if on the host, read-only and unprivileged.

Each pipeline stage is started through `_launch.py`, which chroots into the (read-only) host
root, drops every capability, switches to the host's `nobody` user with the groups `adm` and
`systemd-journal` (so logs and the journal are readable), sets rlimits, nice 19 and idle IO
priority, and execs the host's own binary. The stages are wired together with OS pipes here;
no shell is involved. A deadline kills the whole process group, output is read with a hard
cap and rendered as head + tail.

Without root (development, tests) the launcher skips chroot and the user switch but keeps the
rest, and `Sandbox.isolated` is False so callers can say so.
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .output import HeadTail

LAUNCHER = str(Path(__file__).with_name("_launch.py"))
DEFAULT_TIMEOUT_S = 30.0
MAX_STREAM_BYTES = 32 * 1024 * 1024
"""Stop a command that has produced this much: nobody will read it."""
STDERR_CAP = 4000
GROUPS = ("adm", "systemd-journal")


@dataclass(frozen=True)
class Identity:
    uid: int
    gid: int
    groups: tuple[int, ...]


def _read_ids(path: Path) -> dict[str, tuple[int, ...]]:
    """name → (id, gid-or-0) from passwd/group style files."""
    out: dict[str, tuple[int, ...]] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        fields = line.split(":")
        if len(fields) >= 3 and fields[2].isdigit():
            gid = int(fields[3]) if len(fields) > 3 and fields[3].isdigit() else 0
            out[fields[0]] = (int(fields[2]), gid)
    return out


def host_identity(root: Path) -> Identity:
    """The host's `nobody` plus the log-reading groups, looked up in the host's own files."""
    users = _read_ids(root / "etc/passwd")
    groups = _read_ids(root / "etc/group")
    uid, gid = users.get("nobody", (65534, 65534))
    extra = tuple(groups[name][0] for name in GROUPS if name in groups)
    return Identity(uid=uid, gid=gid or 65534, groups=(gid or 65534, *extra))


@dataclass
class RunResult:
    exit_code: int | None
    output: str
    stderr: str = ""
    duration_s: float = 0.0
    timed_out: bool = False
    output_bytes: int = 0
    truncated: bool = False
    stopped: str | None = None
    """Why the command was stopped early: 'timeout' or 'output limit'."""
    notes: list[str] = field(default_factory=list)


class Sandbox:
    def __init__(
        self,
        root: Path = Path("/"),
        *,
        cpu_s: int = 60,
        address_space: int = 4 << 30,
        nproc: int = 256,
    ):
        self.root = root
        self.identity = host_identity(root)
        self.cpu_s, self.address_space, self.nproc = cpu_s, address_space, nproc

    @property
    def available(self) -> bool:
        return os.name == "posix"

    @property
    def isolated(self) -> bool:
        """True when commands really run chrooted as an unprivileged user (we are root)."""
        return os.name == "posix" and os.geteuid() == 0

    def _spec(
        self, argv: list[str], cpu_s: int | None = None, io_best_effort: bool = False
    ) -> list[str]:
        spec = {
            "argv": argv,
            "root": str(self.root),
            "uid": self.identity.uid,
            "gid": self.identity.gid,
            "groups": list(self.identity.groups),
            "cpu_s": cpu_s or self.cpu_s,
            "io_best_effort": io_best_effort,
            "address_space": self.address_space,
            "nproc": self.nproc,
        }
        return [sys.executable, "-I", LAUNCHER, json.dumps(spec)]

    def run(
        self,
        stages: list[list[str]],
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_chars: int = 5000,
        collect_all: bool = False,
        cpu_s: int | None = None,
        io_best_effort: bool = False,
    ) -> RunResult:
        """Run the pipeline and wait (blocking: call from a thread). With `collect_all` the
        output is kept whole up to `max_chars` instead of head + tail. `cpu_s` raises the
        CPU-time limit of each process (a disk walk may need minutes of CPU) and
        `io_best_effort` runs it at the lowest normal IO priority instead of the idle class
        (idle starves for as long as anything else keeps the disk busy)."""
        if not self.available:
            raise RuntimeError("the command sandbox needs Linux")
        started = time.monotonic()
        deadline = started + timeout
        collector = (
            HeadTail(max_chars, 0)
            if collect_all
            else HeadTail(max_chars * 35 // 100, max_chars * 65 // 100)
        )
        err_r, err_w = os.pipe()
        procs: list[subprocess.Popen[bytes]] = []
        previous: int | None = None
        stopped: str | None = None
        stderr = bytearray()
        try:
            pgid: int | None = None
            for argv in stages:
                proc = subprocess.Popen(
                    self._spec(argv, cpu_s, io_best_effort),
                    stdin=previous if previous is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=err_w,
                    close_fds=True,
                    process_group=0 if pgid is None else pgid,
                )
                if pgid is None:
                    pgid = proc.pid
                if previous is not None:
                    os.close(previous)
                assert proc.stdout is not None
                previous = os.dup(proc.stdout.fileno())
                proc.stdout.close()
                procs.append(proc)
            os.close(err_w)
            err_w = -1
            assert previous is not None
            out_fd, previous = previous, None
            stopped = self._pump(out_fd, err_r, collector, stderr, deadline)
            os.close(out_fd)
            if stopped is None:
                for proc in procs:
                    remaining = deadline - time.monotonic()
                    try:
                        proc.wait(timeout=max(remaining, 0.05))
                    except subprocess.TimeoutExpired:
                        stopped = "timeout"
                        break
            if stopped:
                self._kill(pgid)
                for proc in procs:
                    proc.wait()
        finally:
            for fd in (previous, err_w, err_r):
                if fd is not None and fd >= 0:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            if pgid is not None and any(p.poll() is None for p in procs):
                self._kill(pgid)
                for proc in procs:
                    proc.wait()
        code = procs[-1].returncode if procs else None
        return RunResult(
            exit_code=None if stopped else code,
            output=collector.render(),
            stderr=bytes(stderr).decode("utf-8", "replace")[-STDERR_CAP:],
            duration_s=round(time.monotonic() - started, 2),
            timed_out=stopped == "timeout",
            output_bytes=collector.total,
            truncated=collector.truncated,
            stopped=stopped,
        )

    @staticmethod
    def _kill(pgid: int | None) -> None:
        if pgid is None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)

    @staticmethod
    def _pump(
        out_fd: int, err_fd: int, collector: HeadTail, stderr: bytearray, deadline: float
    ) -> str | None:
        """Read stdout/stderr until both close. Returns why we gave up, if we did."""
        sel = selectors.DefaultSelector()
        sel.register(out_fd, selectors.EVENT_READ, "out")
        sel.register(err_fd, selectors.EVENT_READ, "err")
        open_streams = 2
        try:
            while open_streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return "timeout"
                for key, _ in sel.select(timeout=min(remaining, 0.5)):
                    data = os.read(key.fd, 65536)
                    if not data:
                        sel.unregister(key.fd)
                        open_streams -= 1
                    elif key.data == "out":
                        collector.feed(data)
                        if collector.total > MAX_STREAM_BYTES:
                            return "output limit"
                    elif len(stderr) < STDERR_CAP * 4:
                        stderr += data
        finally:
            sel.close()
        return None
