"""Sandbox launcher: runs as a separate process (`python -I _launch.py '<json spec>'`), builds
the sandbox around itself and `exec`s one command. Deliberately self-contained (standard
library only) so it starts fast and never imports the monitor.

Order matters: rlimits and priorities first, `chroot` into the host root, then, while still
root, drop the capability bounding set, set groups and ids, and only then `no_new_privs`
and `exec`. After the switch to a non-zero uid the effective/permitted capabilities are
gone; regaining root is checked and aborts the launch if it works.

Exit 126 + a message on stderr when the sandbox could not be set up (the command never runs).
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import platform
import sys

PR_SET_NO_NEW_PRIVS = 38
PR_CAPBSET_DROP = 24
SEARCH_PATH = ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
_IOPRIO_SYSCALL = {"x86_64": 251, "aarch64": 30, "armv7l": 314, "i686": 289, "riscv64": 30}


def fail(message: str) -> None:
    sys.stderr.write(f"sandbox setup failed: {message}\n")
    sys.stderr.flush()
    os._exit(126)


def limit(name: str, soft: int | None) -> None:
    import resource

    if soft is None:
        return
    which = getattr(resource, name)
    try:
        resource.setrlimit(which, (soft, soft))
    except (ValueError, OSError):  # lower than the current hard limit is all we need
        _, hard = resource.getrlimit(which)
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(which, (min(soft, hard), hard))


def find_binary(name: str) -> str | None:
    for directory in SEARCH_PATH:
        candidate = f"{directory}/{name}"
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def main() -> None:
    spec = json.loads(sys.argv[1])
    argv: list[str] = spec["argv"]
    privileged = os.geteuid() == 0
    libc = ctypes.CDLL(None, use_errno=True)

    limit("RLIMIT_CPU", spec.get("cpu_s"))
    limit("RLIMIT_AS", spec.get("address_space"))
    limit("RLIMIT_FSIZE", 0)
    limit("RLIMIT_CORE", 0)
    limit("RLIMIT_NOFILE", 256)
    if privileged:  # RLIMIT_NPROC counts every process of the uid: meaningful after the switch
        limit("RLIMIT_NPROC", spec.get("nproc"))
    with contextlib.suppress(OSError):
        os.nice(19)
    number = _IOPRIO_SYSCALL.get(platform.machine())
    if number:
        libc.syscall(number, 1, 0, 3 << 13)  # IOPRIO_WHO_PROCESS, self, class idle

    if privileged:
        try:
            os.chroot(spec["root"])
            os.chdir("/")
        except OSError as exc:
            fail(f"chroot {spec['root']}: {exc}")
        for cap in range(0, 64):  # EINVAL beyond the last capability: ignored
            libc.prctl(PR_CAPBSET_DROP, cap, 0, 0, 0)
        try:
            os.setgroups(spec["groups"])
            os.setresgid(spec["gid"], spec["gid"], spec["gid"])
            os.setresuid(spec["uid"], spec["uid"], spec["uid"])
        except OSError as exc:
            fail(f"dropping privileges: {exc}")
        try:
            os.setuid(0)
        except OSError:
            pass
        else:
            fail("could still switch back to root")
    else:
        with contextlib.suppress(OSError):
            os.chdir("/")
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        fail("no_new_privs not available")

    path = find_binary(argv[0])
    if path is None:
        sys.stderr.write(f"{argv[0]}: command not found on this host\n")
        os._exit(127)
    env = {
        "PATH": ":".join(SEARCH_PATH),
        "LANG": "C",
        "LC_ALL": "C",
        "HOME": "/",
        "TERM": "dumb",
        "SYSTEMD_PAGER": "",
        "PAGER": "cat",
    }
    try:
        os.execve(path, argv, env)
    except OSError as exc:
        fail(f"exec {path}: {exc}")


if __name__ == "__main__":
    main()
