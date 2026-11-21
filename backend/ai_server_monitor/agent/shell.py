"""The `bash` tool's runner: a real `bash -c` on the host's own file system and tools, unable
to change the host.

Isolation is done with stock util-linux tools, not our own code (measured, see
docs/DESIGN.md, "Security boundaries"): `chroot` into the read-only host root, then the host's
`prlimit` (CPU, memory, no file writes, no core dumps), `nice`/`ionice` (low priority) and
`setpriv`: an uid no host process uses (so nothing can be signalled), cleared groups,
no_new_privs, and as the only capability CAP_DAC_READ_SEARCH, which reads every file and
writes none. That capability exists only when the container was started with
`cap_add: [DAC_READ_SEARCH]`; without it the shell runs as a plain unprivileged user (with the
log-reading groups adm and systemd-journal) and says so when a path was unreadable.

Bubblewrap and nsjail were tried: they need namespaces, which Docker's default seccomp
profile refuses. Without root (development, tests) bash runs directly, unisolated.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .output import HeadTail, human

SANDBOX_UID = 2147483000
"""Owns no host process and no host file. (The host's `nobody` can signal every process that
runs as nobody: measured.)"""
CAP_DAC_READ_SEARCH = 2
LOG_GROUPS = ("adm", "systemd-journal")
SEARCH_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
HOST_TOOLS = ("env", "bash", "prlimit", "nice", "ionice", "setpriv")
MAX_STREAM_BYTES = 32 * 1024 * 1024
"""Stop a command that has printed this much: nobody will read it."""
ADDRESS_SPACE = 4 << 30
CAP_NOTE = (
    "Note: root-only paths are unreadable for this shell because the monitor's container "
    "lacks the DAC_READ_SEARCH capability. The owner can enable read-only access to them by "
    "adding `cap_add: [DAC_READ_SEARCH]` to the ai-server-monitor service in compose.yaml."
)


_WRITE_DENIED = re.compile(
    r"cannot (touch|create|remove|delete|move|write|make|overwrite|change|set|link|kill)"
    r"|for writing|operation not permitted|\bkill\b|\.sock\b|signal",
    re.IGNORECASE,
)


def read_denied(output: str) -> bool:
    """Whether a "Permission denied" in `output` is a failed read (the capability would help),
    not a refused write, signal or socket connect (it would not)."""
    return any(
        "permission denied" in line.lower() and not _WRITE_DENIED.search(line)
        for line in output.splitlines()
    )


def capability_bounded(cap: int, status: Path = Path("/proc/self/status")) -> bool:
    """Whether this process may still hold `cap` (it is in the bounding set)."""
    try:
        for line in status.read_text().splitlines():
            if line.startswith("CapBnd:"):
                return bool(int(line.split()[1], 16) >> cap & 1)
    except (OSError, ValueError, IndexError):
        pass
    return False


def group_ids(root: Path, names: tuple[str, ...]) -> list[int]:
    """Ids of the named groups in the host's /etc/group (missing ones are left out)."""
    found: dict[str, int] = {}
    with contextlib.suppress(OSError):
        for line in (root / "etc/group").read_text(errors="replace").splitlines():
            fields = line.split(":")
            if len(fields) >= 3 and fields[2].isdigit():
                found[fields[0]] = int(fields[2])
    return [found[n] for n in names if n in found]


def resolve_docker_host(url: str | None) -> str | None:
    """`tcp://name:port` with the name resolved here: inside the chroot the host's resolv.conf
    applies, which does not know the compose service names."""
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("tcp", "http", "https") or not parts.hostname:
        return url
    try:
        info = socket.getaddrinfo(parts.hostname, parts.port or 2375, type=socket.SOCK_STREAM)
    except OSError:
        return url
    address = str(info[0][4][0])
    host = f"[{address}]" if ":" in address else address
    return f"{parts.scheme}://{host}:{parts.port or 2375}"


@dataclass
class ShellResult:
    exit_code: int | None
    """None when the command was stopped."""
    output: str
    """stdout and stderr as printed, head + tail kept when it was long."""
    duration_s: float
    stopped: str | None = None
    """'timeout' or 'output limit' when the command was stopped early."""
    output_bytes: int = 0
    truncated: bool = False
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.stopped is None and self.exit_code == 0

    def text(self) -> str:
        """What the model reads."""
        body = self.output.rstrip("\n") if self.output.strip() else "(no output)"
        if self.stopped == "timeout":
            status = (
                f"stopped after {self.duration_s:.0f}s (time limit); above is what it printed "
                "until then. A longer timeout_s or a narrower command may finish."
            )
        elif self.stopped:
            status = f"stopped after {human(self.output_bytes)} of output; narrow the command"
        else:
            status = f"exit code {self.exit_code}, {self.duration_s:.1f}s"
        return "\n".join(p for p in (body, f"[{status}]", self.note) if p)


class HostShell:
    def __init__(
        self,
        root: Path,
        *,
        docker_host: str | None = None,
        uid: int = SANDBOX_UID,
        extra_env: dict[str, str] | None = None,
    ):
        self.root = root
        self.docker_host = docker_host
        self.uid = uid
        self.extra_env = extra_env or {}

    @property
    def available(self) -> bool:
        return os.name == "posix" and shutil.which("bash") is not None

    @property
    def isolated(self) -> bool:
        """True when commands really run chrooted as the sandbox user (we are root)."""
        return os.name == "posix" and os.geteuid() == 0

    @property
    def read_all(self) -> bool:
        """The sandbox keeps CAP_DAC_READ_SEARCH: root-only files are readable."""
        return self.isolated and capability_bounded(CAP_DAC_READ_SEARCH)

    def missing_tools(self) -> list[str]:
        """Host programs the isolation needs that the host root does not have."""
        dirs = SEARCH_PATH.split(":")
        return [
            name
            for name in HOST_TOOLS
            if not any(os.access(self.root / d.lstrip("/") / name, os.X_OK) for d in dirs)
        ]

    def environment(self) -> dict[str, str]:
        env = {
            "PATH": SEARCH_PATH,
            "LANG": "C.UTF-8",
            "HOME": "/",
            "TERM": "dumb",
            "PAGER": "cat",
            "SYSTEMD_PAGER": "",
            "SYSTEMD_COLORS": "0",
        }
        docker = resolve_docker_host(self.docker_host) if self.isolated else self.docker_host
        if docker:
            env["DOCKER_HOST"] = docker
        env.update(self.extra_env)
        return env

    def argv(self, command: str, timeout_s: float) -> list[str]:
        env = self.environment()
        if not self.isolated:
            return ["bash", "-c", command]
        caps = "-all,+dac_read_search" if self.read_all else "-all"
        gids = [] if self.read_all else group_ids(self.root, LOG_GROUPS)
        groups = [f"--groups={','.join(map(str, gids))}"] if gids else ["--clear-groups"]
        return [
            shutil.which("chroot") or "/usr/sbin/chroot",
            str(self.root),
            "/usr/bin/env",
            "-i",
            *(f"{k}={v}" for k, v in env.items()),
            "prlimit",
            f"--cpu={max(int(timeout_s) + 5, 10)}",
            f"--as={ADDRESS_SPACE}",
            "--fsize=0",
            "--core=0",
            "--nofile=1024",
            "--nproc=512",
            "--",
            "nice",
            "-n",
            "19",
            "ionice",
            "-c",
            "2",
            "-n",
            "7",
            "setpriv",
            f"--reuid={self.uid}",
            f"--regid={self.uid}",
            *groups,
            f"--inh-caps={caps}",
            f"--ambient-caps={caps}",
            f"--bounding-set={caps}",
            "--no-new-privs",
            "--",
            "bash",
            "-c",
            command,
        ]

    async def run(self, command: str, *, timeout_s: float, max_chars: int) -> ShellResult:
        """Run `command` with bash; stdout and stderr merged, at most `timeout_s` seconds.
        Cancelling the call kills the command."""
        if not self.available:
            return ShellResult(None, "the shell needs Linux with bash", 0.0, stopped="error")
        if self.isolated and (missing := self.missing_tools()):
            return ShellResult(
                None,
                f"the host lacks {', '.join(missing)} (util-linux/coreutils), which the "
                "read-only sandbox needs",
                0.0,
                stopped="error",
            )
        env = {"PATH": SEARCH_PATH} if self.isolated else self.environment()
        result = await run_process(self.argv(command, timeout_s), env, timeout_s, max_chars)
        if self.isolated and not self.read_all and read_denied(result.output):
            result.note = CAP_NOTE
        return result


async def run_process(
    argv: list[str], env: dict[str, str], timeout_s: float, max_chars: int
) -> ShellResult:
    """Run `argv` in its own process group, stdout and stderr merged and capped (head + tail),
    at most `timeout_s` seconds; the whole group is killed on timeout or cancellation."""
    started = time.monotonic()
    sink = HeadTail(max_chars * 35 // 100, max_chars * 65 // 100)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        # Its own session, so its own process group (killpg below). Not `process_group=0`:
        # uvloop, which uvicorn uses in the image, rejects that argument.
        start_new_session=True,
    )
    assert proc.stdout is not None
    stopped: str | None = None
    try:
        async with asyncio.timeout(timeout_s):
            while chunk := await proc.stdout.read(65536):
                sink.feed(chunk)
                if sink.total > MAX_STREAM_BYTES:
                    stopped = "output limit"
                    break
            if stopped is None:
                await proc.wait()
    except TimeoutError:
        stopped = "timeout"
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
    return ShellResult(
        exit_code=None if stopped else proc.returncode,
        output=sink.render(),
        duration_s=round(time.monotonic() - started, 2),
        stopped=stopped,
        output_bytes=sink.total,
        truncated=sink.truncated,
    )
