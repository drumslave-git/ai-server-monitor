"""Changing commands: a real root bash on the host itself, through util-linux `nsenter` into
the namespaces of PID 1 (the container has `pid: host`). Used only for the chat's
`bash_write`, and only after the owner approved the command (or the policy is `auto`).

Entering PID 1's namespaces needs two capabilities Docker does not grant by default
(measured: either one alone fails with "reassociate to namespaces failed"):

    cap_add:
      - SYS_ADMIN     # setns() into the host's mount, UTS, IPC, network and PID namespaces
      - SYS_PTRACE    # open /proc/1/ns/* of a process with more capabilities than ours

`probe()` tries once at start; without them the tool is not offered and the status says why.
The read-only `bash` is not affected: its sandbox keeps only CAP_DAC_READ_SEARCH whatever the
container has (re-measured with these capabilities granted).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from .shell import SEARCH_PATH, ShellResult, run_process

COMPOSE_HINT = (
    "add SYS_ADMIN and SYS_PTRACE to cap_add of the ai-server-monitor service in "
    "compose.yaml (they are commented out in the example), then recreate the container"
)
PROBE_TIMEOUT_S = 10.0


@dataclass
class ChangeShellStatus:
    available: bool
    reason: str | None = None
    """Why the change shell cannot work here, with what enables it."""


def nsenter_argv(command: str) -> list[str]:
    return [
        shutil.which("nsenter") or "nsenter",
        "--target",
        "1",
        "--mount",
        "--uts",
        "--ipc",
        "--net",
        "--pid",
        "--",
        "/bin/bash",
        "-c",
        command,
    ]


ENV = {
    "PATH": SEARCH_PATH,
    "LANG": "C.UTF-8",
    "HOME": "/root",
    "TERM": "dumb",
    "PAGER": "cat",
    "SYSTEMD_PAGER": "",
    "SYSTEMD_COLORS": "0",
    "DEBIAN_FRONTEND": "noninteractive",
}


class ChangeShell:
    def __init__(self) -> None:
        self.status = ChangeShellStatus(False, "not checked yet")

    async def probe(self) -> ChangeShellStatus:
        """Whether a root shell on the host works here (tried once, at start)."""
        if os.name != "posix":
            self.status = ChangeShellStatus(False, "the change shell needs Linux")
        elif os.geteuid() != 0:
            self.status = ChangeShellStatus(False, "the monitor does not run as root")
        elif shutil.which("nsenter") is None:
            self.status = ChangeShellStatus(False, "nsenter (util-linux) is missing in the image")
        else:
            result = await run_process(nsenter_argv("true"), ENV, PROBE_TIMEOUT_S, 2000)
            if result.exit_code == 0:
                self.status = ChangeShellStatus(True)
            else:
                why = " ".join(result.output.split())[:200] or f"exit {result.exit_code}"
                self.status = ChangeShellStatus(
                    False, f"cannot enter the host's namespaces ({why}): {COMPOSE_HINT}"
                )
        return self.status

    async def run(self, command: str, *, timeout_s: float, max_chars: int) -> ShellResult:
        if not self.status.available:
            return ShellResult(None, self.status.reason or "not available", 0.0, stopped="error")
        return await run_process(nsenter_argv(command), dict(ENV), timeout_s, max_chars)
