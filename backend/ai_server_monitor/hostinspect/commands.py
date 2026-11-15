"""Turning the model's command line into something safe to run: no shell, an allowlist of
binaries, per-command argument rules, and a guard for paths that must never be read.

The line is parsed by `parse_pipeline`: quotes and backslashes work as in a shell, `|` joins
stages, and everything else a shell would interpret (`; & && || > < $( ) backticks, variables)
is refused. Nothing is ever executed through a shell. Allowed commands are looked up by bare
name inside the host root at run time, so the host's own `journalctl`, `ss`, `du`... are used.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import posixpath
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MAX_COMMAND_CHARS = 1000
MAX_STAGES = 5
MAX_GLOB_MATCHES = 200


class Refused(Exception):
    """The command is not allowed; the message tells the model why and what to do instead."""


# --------------------------------------------------------------------------- tokenizer

_REFUSED_OPERATORS = {
    ";": "command chaining (`;`, `&&`, `||`)",
    "&": "command chaining or background jobs (`&`, `&&`)",
    "<": "redirection (`<`)",
    ">": "redirection (`>`; the system is read-only)",
    "(": "subshells and command substitution",
    ")": "subshells and command substitution",
    "`": "command substitution (backticks)",
    "$": "variables and command substitution (`$`)",
}


def _refuse_operator(char: str) -> Refused:
    return Refused(
        f"{_REFUSED_OPERATORS[char]} is not allowed. Run ONE command per call (call run_command "
        "again for the next one), or a pipeline of allowed commands joined with `|`. "
        "There is no shell: no variables, no expansion."
    )


def split_stages(line: str) -> list[list[str]]:
    """Split a command line into stages of words (shell-like quoting, `|` as the only
    operator). Raises Refused for anything else a shell would interpret."""
    if len(line) > MAX_COMMAND_CHARS:
        raise Refused(f"command too long (max {MAX_COMMAND_CHARS} characters)")
    stages: list[list[str]] = [[]]
    word: str | None = None
    quote: str | None = None
    i, n = 0, len(line)

    def flush() -> None:
        nonlocal word
        if word is not None:
            stages[-1].append(word)
            word = None

    while i < n:
        c = line[i]
        if quote == "'":
            if c == "'":
                quote = None
            else:
                word = (word or "") + c
        elif quote == '"':
            if c == '"':
                quote = None
            elif c == "\\" and i + 1 < n and line[i + 1] in '"\\$`':
                word = (word or "") + line[i + 1]
                i += 1
            elif c in "$`":
                raise _refuse_operator(c)
            else:
                word = (word or "") + c
        elif c in " \t":
            flush()
        elif c in "\r\n":
            raise Refused("only a single line is allowed")
        elif c in "'\"":
            quote = c
            word = word or ""
        elif c == "\\":
            if i + 1 >= n:
                raise Refused("dangling backslash")
            word = (word or "") + line[i + 1]
            i += 1
        elif c == "|":
            if i + 1 < n and line[i + 1] == "|":
                raise _refuse_operator(";")
            flush()
            if not stages[-1]:
                raise Refused("empty command before `|`")
            stages.append([])
        elif c in _REFUSED_OPERATORS:
            raise _refuse_operator(c)
        else:
            word = (word or "") + c
        i += 1
    if quote:
        raise Refused("unterminated quote")
    flush()
    if not stages[-1]:
        raise Refused("empty command" if len(stages) == 1 else "empty command after `|`")
    if len(stages) > MAX_STAGES:
        raise Refused(f"at most {MAX_STAGES} commands in a pipeline")
    return stages


# --------------------------------------------------------------------------- path guard

DENY_BASENAMES = frozenset(
    {
        "shadow", "gshadow", "shadow-", "gshadow-", "monitor.db", ".netrc", ".pgpass",
        ".git-credentials", ".htpasswd", "cookies.sqlite", "login data", "logins.json",
        "key3.db", "key4.db", "keyring", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    }
)  # fmt: skip
DENY_PATTERNS = (
    "id_*", "*.key", "*.pem", "*.p12", "*.pfx", "*.jks", "*.keystore", "*.kdbx", ".env",
    ".env.*", "*.env", "*secret*", "*password*", "*credential*", "ssh_host_*", "*_rsa",
    "*_ed25519", "*.ovpn", "*.gpg", "*.asc",
)  # fmt: skip
DENY_DIR_NAMES = frozenset(
    {
        ".ssh", ".gnupg", ".mozilla", ".password-store", "google-chrome", "chromium",
        "bravesoftware", "wireguard",
    }
)  # fmt: skip
DENY_PREFIXES = ("/etc/ssl/private", "/etc/letsencrypt", "/etc/wireguard")


def denied_reason(norm: str, extra_dirs: tuple[str, ...] = ()) -> str | None:
    """Why this absolute, normalised host path must not be read (None = fine)."""
    parts = [p for p in norm.split("/") if p]
    base = parts[-1].lower() if parts else ""
    if base in DENY_BASENAMES:
        return f"{parts[-1]} is on the deny list (credentials)"
    if parts and parts[0] == "proc" and base in ("environ", "mem"):
        return "process environments are on the deny list"
    for pattern in DENY_PATTERNS:
        if fnmatch.fnmatchcase(base, pattern):
            return f"{parts[-1]} looks like a secret or key ({pattern}) and is on the deny list"
    for part in parts:
        if part.lower() in DENY_DIR_NAMES:
            return f"{part}/ is on the deny list (keys, profiles)"
    for prefix in (*DENY_PREFIXES, *extra_dirs):
        if norm == prefix or norm.startswith(prefix.rstrip("/") + "/"):
            return f"{prefix} is on the deny list"
    return None


def monitor_data_dirs(mountinfo: str | None = None, mountpoint: str = "/data") -> tuple[str, ...]:
    """Where the monitor's own data directory (settings with secrets, the SQLite DB) lives as
    seen from the host: the `root` field of the container's mount at /data."""
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(errors="replace")
        except OSError:
            return ()
    found: list[str] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4 and fields[4] == mountpoint and fields[3] not in ("", "/"):
            found.append(posixpath.normpath(fields[3].replace("\\040", " ")))
    return tuple(found)


def normalize(arg: str) -> str:
    return posixpath.normpath(arg if arg.startswith("/") else "/" + arg)


@dataclass
class PathGuard:
    """Checks (and glob-expands) path arguments against the deny list, looking through
    symlinks inside the host root."""

    root: Path = Path("/")
    extra_dirs: tuple[str, ...] = ()

    def _prefix(self) -> str:
        return "" if str(self.root) in ("/", "") else str(self.root).rstrip("/")

    def reason(self, arg: str) -> str | None:
        norm = normalize(arg)
        why = denied_reason(norm, self.extra_dirs)
        if why:
            return why
        if os.name == "posix":
            prefix = self._prefix()
            real = os.path.realpath(prefix + norm)
            if prefix and real.startswith(prefix + "/"):
                real = real[len(prefix) :]
            elif prefix and real == prefix:
                real = "/"
            if real != norm:
                return denied_reason(posixpath.normpath(real), self.extra_dirs)
        return None

    def expand(self, arg: str) -> tuple[list[str], int]:
        """Glob-expand an absolute path argument ourselves (there is no shell). Returns the
        matches that pass the deny list and how many were dropped."""
        if not arg.startswith("/") or not any(c in arg for c in "*?["):
            return [arg], 0
        prefix = self._prefix()
        matches = sorted(glob.glob(prefix + arg))[:MAX_GLOB_MATCHES]
        if not matches:
            return [arg], 0
        kept, dropped = [], 0
        for match in matches:
            host = match[len(prefix) :] if prefix else match
            if self.reason(host):
                dropped += 1
            else:
                kept.append(host)
        return kept, dropped


# --------------------------------------------------------------------------- per-command rules

Rule = Callable[[list[str]], list[str]]

READERS = frozenset(
    {"ls", "stat", "du", "df", "find", "cat", "head", "tail", "grep", "egrep", "zgrep", "wc",
     "sort", "uniq", "cut", "file", "column"}
)  # fmt: skip
GREPS = frozenset({"grep", "egrep", "zgrep"})
SIMPLE = frozenset(
    {"lsblk", "findmnt", "tr", "pgrep", "ps", "free", "uptime", "vmstat", "iostat", "lsof",
     "uname", "id", "date", "hostname", "getent", "nvidia-smi", "ip", "ss", "top", "mount",
     "journalctl", "systemctl", "dmesg"}
)  # fmt: skip
ALLOWED = READERS | SIMPLE


def _bad(command: str, option: str, why: str = "") -> Refused:
    return Refused(f"`{command} {option}` is not allowed{': ' + why if why else ''} (read-only)")


def _deny_options(command: str, denied: tuple[str, ...], argv: list[str]) -> None:
    for arg in argv[1:]:
        if arg == "--":
            break
        for option in denied:
            if arg == option or (option.startswith("--") and arg.startswith(option + "=")):
                raise _bad(command, option)
            if option.endswith("*") and arg.startswith(option[:-1]):
                raise _bad(command, arg)


def _short_cluster_has(arg: str, letters: str) -> bool:
    return arg.startswith("-") and not arg.startswith("--") and any(c in letters for c in arg[1:])


def _rule_find(argv: list[str]) -> list[str]:
    _deny_options(
        "find",
        (
            "-delete",
            "-exec",
            "-execdir",
            "-ok",
            "-okdir",
            "-fprint",
            "-fprint0",
            "-fprintf",
            "-fls",
        ),
        argv,
    )
    return argv


def _rule_journalctl(argv: list[str]) -> list[str]:
    _deny_options(
        "journalctl",
        ("--vacuum*", "--rotate", "--flush", "--sync", "--relinquish-var",
         "--smart-relinquish-var", "--setup-keys", "--update-catalog", "--follow", "-f",
         "--directory", "-D", "--file", "--root", "--image", "--namespace", "--verify",
         "--interval", "--force", "--new-id128", "--disk-usage"),
        argv,
    )  # fmt: skip
    for arg in argv[1:]:
        if _short_cluster_has(arg, "f") and not arg.startswith("-n"):
            raise _bad("journalctl", "-f", "following never ends")
    out = list(argv)
    if "--no-pager" not in out:
        out.insert(1, "--no-pager")
    limiting = ("-n", "--lines", "--since", "-S", "--until", "-U", "-b", "--boot", "-r",
                "--cursor", "--after-cursor", "--list-boots", "--fields", "-F", "-N",
                "--show-cursor", "--header", "--output-fields")  # fmt: skip
    if not any(a.split("=")[0] in limiting or a.startswith("-n") for a in out[1:]):
        out[1:1] = ["-n", "200"]
    return out


SYSTEMCTL_VERBS = frozenset(
    {"status", "show", "cat", "list-units", "list-unit-files", "list-timers", "list-sockets",
     "list-dependencies", "list-jobs", "is-active", "is-enabled", "is-failed", "is-system-running",
     "get-default", "list-machines"}
)  # fmt: skip


def _rule_systemctl(argv: list[str]) -> list[str]:
    _deny_options("systemctl", ("--host", "-H", "--machine", "-M", "--force", "--now"), argv)
    verb = next((a for a in argv[1:] if not a.startswith("-")), None)
    if verb is None:
        return [*argv, "list-units", "--no-pager"]
    if verb not in SYSTEMCTL_VERBS:
        raise Refused(
            f"`systemctl {verb}` is not allowed: read-only verbs only "
            f"({', '.join(sorted(SYSTEMCTL_VERBS))})"
        )
    out = list(argv)
    if "--no-pager" not in out:
        out.insert(1, "--no-pager")
    return out


def _rule_sort(argv: list[str]) -> list[str]:
    for arg in argv[1:]:
        if arg == "-o" or arg.startswith("--output") or _short_cluster_has(arg, "o"):
            raise _bad("sort", "-o", "writes a file")
    return argv


def _rule_tail(argv: list[str]) -> list[str]:
    for arg in argv[1:]:
        if arg in ("--follow", "--retry") or arg.startswith("--follow="):
            raise _bad("tail", "-f", "following never ends")
        if _short_cluster_has(arg, "fF") and not arg.lstrip("-").isdigit():
            raise _bad("tail", "-f", "following never ends")
    return argv


def _rule_dmesg(argv: list[str]) -> list[str]:
    _deny_options(
        "dmesg",
        ("-c", "-C", "--clear", "--read-clear", "-D", "--console-off", "-E", "--console-on",
         "-n", "--console-level", "-w", "--follow", "-W", "--follow-new"),
        argv,
    )  # fmt: skip
    return argv


def _rule_top(argv: list[str]) -> list[str]:
    allowed = {"-b", "-n", "1", "-bn1", "-c", "-w", "-o", "%MEM", "%CPU", "-w512", "-w200"}
    for arg in argv[1:]:
        if arg not in allowed:
            raise Refused("`top` only works as `top -bn1` (one batch snapshot); try `ps aux`")
    if "-b" not in argv and "-bn1" not in argv:
        return [argv[0], "-bn1", *argv[1:]]
    return argv


_IP_OBJECTS = frozenset({"addr", "address", "a", "route", "r", "link", "l", "neigh", "neighbour",
                         "n", "rule", "maddr", "netns"})  # fmt: skip


_IP_MUTATING = frozenset({"add", "del", "delete", "set", "flush", "change", "replace",
                          "append", "prepend", "exec", "monitor"})  # fmt: skip


def _rule_ip(argv: list[str]) -> list[str]:
    rest = [a for a in argv[1:] if not a.startswith("-")]
    if not rest:
        raise Refused("`ip` needs an object, e.g. `ip addr show`, `ip route show`, `ip link show`")
    if rest[0] not in _IP_OBJECTS:
        raise Refused(f"`ip {rest[0]}` is not allowed: use addr, route, link or neigh with `show`")
    for arg in rest[1:]:
        if arg in _IP_MUTATING:
            raise Refused(f"`ip ... {arg}` changes the system: only `show` is allowed")
    return argv


def _rule_mount(argv: list[str]) -> list[str]:
    if any(a not in ("-l",) for a in argv[1:]):
        raise Refused("`mount` is only allowed without arguments (or `-l`); try `findmnt`")
    return argv


def _rule_hostname(argv: list[str]) -> list[str]:
    if any(not a.startswith("-") for a in argv[1:]) or any(
        a in ("-F", "--file", "-b", "--boot") for a in argv[1:]
    ):
        raise Refused("`hostname` can only print, not set")
    return argv


def _rule_date(argv: list[str]) -> list[str]:
    _deny_options("date", ("-s", "--set"), argv)
    return argv


def _rule_getent(argv: list[str]) -> list[str]:
    db = next((a for a in argv[1:] if not a.startswith("-")), "")
    if db in ("shadow", "gshadow"):
        raise Refused(f"`getent {db}` is on the deny list")
    return argv


_NVIDIA_MUTATING = ("-r", "--gpu-reset", "-pm", "--persistence-mode", "-e", "--ecc-config", "-c",
                    "--compute-mode", "-pl", "--power-limit", "-ac", "-rac", "-lgc", "-rgc",
                    "-lmc", "-rmc", "-mig", "-gom", "--applications-clocks", "--lock-gpu-clocks",
                    "--auto-boost-default", "-acp", "--accounting-mode", "-am", "-pm")  # fmt: skip


def _rule_nvidia(argv: list[str]) -> list[str]:
    _deny_options("nvidia-smi", _NVIDIA_MUTATING, argv)
    return argv


def _rule_file(argv: list[str]) -> list[str]:
    _deny_options("file", ("-C", "--compile", "-m", "--magic-file"), argv)
    return argv


def _rule_lsof(argv: list[str]) -> list[str]:
    _deny_options("lsof", ("+D", "+d"), argv)  # directory walks are slow and rarely needed
    return argv


RULES: dict[str, Rule] = {
    "find": _rule_find,
    "journalctl": _rule_journalctl,
    "systemctl": _rule_systemctl,
    "sort": _rule_sort,
    "tail": _rule_tail,
    "dmesg": _rule_dmesg,
    "top": _rule_top,
    "ip": _rule_ip,
    "mount": _rule_mount,
    "hostname": _rule_hostname,
    "date": _rule_date,
    "getent": _rule_getent,
    "nvidia-smi": _rule_nvidia,
    "file": _rule_file,
    "lsof": _rule_lsof,
}


# --------------------------------------------------------------------------- planning


@dataclass
class Plan:
    stages: list[list[str]]
    notes: list[str] = field(default_factory=list)


def _path_positions(command: str, argv: list[str]) -> list[int]:
    """Indexes of the arguments that are file paths (vs. options and patterns)."""
    if command == "tr" or command not in READERS:
        return []
    positions: list[int] = []
    if command == "find":
        for i, arg in enumerate(argv[1:], 1):
            if arg.startswith("-") or arg in ("(", ")", "!", ","):
                break
            positions.append(i)
        return positions
    skip_pattern = command in GREPS
    skip_next = False
    for i, arg in enumerate(argv[1:], 1):
        if skip_next:
            skip_next = False
            continue
        if command in GREPS and arg in ("-e", "--regexp"):
            skip_pattern, skip_next = False, True
            continue
        if command in GREPS and (arg.startswith("--regexp=") or arg == "-f"):
            skip_pattern = False
        if arg == "--":
            continue
        if arg.startswith("-"):
            if "=" in arg and arg.startswith("--") and arg.split("=", 1)[1].startswith("/"):
                positions.append(i)  # --file=/etc/x: check the value
            continue
        if skip_pattern:
            skip_pattern = False
            continue
        positions.append(i)
    return positions


def _grep_excludes(argv: list[str]) -> list[str]:
    recursive = any(
        a in ("-r", "-R", "--recursive", "--dereference-recursive") or _short_cluster_has(a, "rR")
        for a in argv[1:]
        if a.startswith("-")
    )
    if not recursive:
        return argv
    extra = [f"--exclude={p}" for p in (*DENY_PATTERNS, *sorted(DENY_BASENAMES))]
    extra += [f"--exclude-dir={d}" for d in sorted(DENY_DIR_NAMES)]
    return [argv[0], *extra, *argv[1:]]


CONTAINER_CLIENTS = frozenset(
    {"docker", "podman", "nerdctl", "crictl", "kubectl", "docker-compose"}
)
CONTAINER_HINT = (
    "Containers are not reachable with a command here, but with tools: container logs -> "
    "`container_logs`; container state, config, mounts, ports -> `container_inspect` (no name "
    "lists all containers); a configured service (qBittorrent, *arr...) that cannot be reached "
    "-> `diagnose_service`."
)


def refusal_for(command: str) -> str:
    """Why `command` is not run, and what to use instead."""
    if command in CONTAINER_CLIENTS:
        return f"`{command}` is not available. {CONTAINER_HINT}"
    return (
        f"`{command}` is not available. Allowed commands: {', '.join(sorted(ALLOWED))}. "
        f"{CONTAINER_HINT}"
    )


def parse_pipeline(line: str, guard: PathGuard | None = None) -> Plan:
    """Validate `line` and return the stages to execute (argument rules applied, globs
    expanded, defaults added) or raise Refused."""
    guard = guard or PathGuard()
    plan = Plan(stages=[])
    for words in split_stages(line):
        command = words[0]
        if "=" in command and command.split("=")[0].replace("_", "").isalnum():
            raise Refused("environment assignments (VAR=value) are not allowed")
        if "/" in command:
            raise Refused("use the bare command name (e.g. `ls`, not `/bin/ls`)")
        if command not in ALLOWED:
            raise Refused(refusal_for(command))
        argv = RULES[command](words) if command in RULES else list(words)
        positions = set(_path_positions(command, argv))
        final: list[str] = []
        dropped_total = 0
        for i, arg in enumerate(argv):
            if i not in positions:
                final.append(arg)
                continue
            value = arg.split("=", 1)[1] if arg.startswith("--") and "=" in arg else arg
            if value != arg:
                if why := guard.reason(value):
                    raise Refused(f"refused: {why}")
                final.append(arg)
                continue
            if command != "find":
                matches, dropped = guard.expand(arg)
                dropped_total += dropped
                for match in matches:
                    if why := guard.reason(match):
                        raise Refused(f"refused: {why}")
                final.extend(matches)
            else:
                if why := guard.reason(arg):
                    raise Refused(f"refused: {why}")
                final.append(arg)
        if dropped_total:
            plan.notes.append(f"{dropped_total} matches skipped (deny list: keys, secrets)")
        if command in GREPS:
            final = _grep_excludes(final)
        plan.stages.append(final)
    return plan
