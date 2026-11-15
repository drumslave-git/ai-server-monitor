"""Chat tools that look at the host itself: a sandboxed read-only command runner, disk usage
with owner attribution, container inspection and metric history.

`run_command` is the general tool: the model runs ordinary read-only commands (`journalctl`,
`systemctl status`, `ss`, `du`, `ps`, ...) on the host and reads the output. How that stays
safe is described in `hostinspect/commands.py` (what may be run) and `hostinspect/sandbox.py`
(how: chrooted into the read-only host root as an unprivileged user, no capabilities,
resource limits, a deadline, capped output). Every command is recorded in an audit list.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field

from ..collectors.base import CollectorUnavailable
from ..collectors.docker import demux_logs
from ..hostinspect import attribution, disk
from ..hostinspect.commands import PathGuard, Refused, monitor_data_dirs, parse_pipeline
from ..hostinspect.containers import summarize_inspect, summarize_list
from ..hostinspect.history import MetricError
from ..hostinspect.history import metric_history as _metric_history
from ..hostinspect.output import scrub_output
from ..hostinspect.sandbox import Sandbox
from .audit import record_call
from .tools import NoArgs, Tool, ToolContext, ToolError, _Args

log = logging.getLogger(__name__)

RESULT_SHARE = 0.7
"""Share of `chat.max_tool_result_chars` a command's output may use (the rest is JSON framing
and escaping), so the engine's own trimming never has to cut command output."""
ATTRIBUTION_TIMEOUT_S = 60.0
"""Owner lookup (Docker / *arr API calls) next to a disk scan: it may only add information, so
it never fails the answer; generous because the scan may be long anyway."""
SCAN_CPU_S = 3600
"""CPU-time limit of one `du` of a disk scan (the sandbox default of 60 s is for ordinary
commands; a walk of a big disk can use minutes of CPU)."""
SCAN_RESERVE_S = 90.0
"""Left over from `chat.tool_timeout` for the owner lookup and the answer after the walk."""

_audit_lock = asyncio.Lock()
_sandboxes: dict[Path, Sandbox] = {}


def _sandbox(ctx: ToolContext) -> Sandbox:
    root = ctx.app.host_env.root
    if root not in _sandboxes:
        _sandboxes[root] = Sandbox(root)
    return _sandboxes[root]


def _guard(ctx: ToolContext) -> PathGuard:
    return PathGuard(ctx.app.host_env.root, extra_dirs=monitor_data_dirs())


def _require_linux() -> None:
    if os.name != "posix":
        raise ToolError("host commands need Linux; this monitor is not running on Linux")


async def _audit(ctx: ToolContext, kind: str, **fields: Any) -> None:
    await record_call(ctx.app.db, kind, ctx.session_id, **fields)


# --------------------------------------------------------------------------- run_command


class RunCommandArgs(_Args):
    cmd: str = Field(
        min_length=1,
        max_length=1000,
        description="One command or a `|` pipeline of allowed commands. No `;` `&&` `>` `$`.",
    )


async def _run_command(ctx: ToolContext, args: RunCommandArgs) -> Any:
    if not ctx.settings.chat.shell:
        raise ToolError("running commands is switched off in the settings")
    try:
        plan = parse_pipeline(args.cmd, _guard(ctx))
    except Refused as exc:
        await _audit(ctx, "shell", cmd=args.cmd, refused=str(exc)[:200])
        raise ToolError(f"refused: {exc}") from exc
    _require_linux()
    sandbox = _sandbox(ctx)
    limit = ctx.settings.chat.max_tool_result_chars
    timeout = max(5.0, ctx.settings.chat.tool_timeout.total_seconds() - 5)
    result = await asyncio.to_thread(
        sandbox.run, plan.stages, timeout=timeout, max_chars=int(limit * RESULT_SHARE)
    )
    await _audit(
        ctx,
        "shell",
        cmd=args.cmd,
        exit=result.exit_code,
        stopped=result.stopped,
        seconds=result.duration_s,
        bytes=result.output_bytes,
    )
    out: dict[str, Any] = {"exit_code": result.exit_code, "output": scrub_output(result.output)}
    if result.stderr.strip() and (result.exit_code != 0 or not result.output.strip()):
        out["stderr"] = scrub_output(result.stderr.strip())[-1500:]
    notes = list(plan.notes)
    if result.stopped == "timeout":
        notes.append(f"stopped after {timeout:.0f}s; output so far is shown (partial)")
    elif result.stopped:
        notes.append("stopped: far too much output; start/end shown")
    if result.truncated:
        notes.append("output too long: only the start and end are shown; narrow the command")
    if not sandbox.isolated:
        notes.append("not isolated (monitor is not root here); running unprivileged in-place")
    if notes:
        out["notes"] = notes
    return out


# --------------------------------------------------------------------------- disk_usage


class DiskUsageArgs(_Args):
    path: str = Field(description="Absolute host path, e.g. /mnt/disks/disk14TB.")
    depth: int = Field(default=1, ge=1, le=3, description="Directory levels to break down.")
    top_files: int = Field(
        default=0, ge=0, le=30, description="If > 0: list this many largest FILES instead."
    )


async def _docker_api(ctx: ToolContext) -> Any:
    collector = ctx.app.collectors.collectors.get("docker")
    api = getattr(collector, "api", None)
    if api is None:
        raise ToolError("the Docker API is not available (docker collector is off or unreachable)")
    return api


async def _locations(ctx: ToolContext) -> attribution.Locations:
    """Everything known about who owns which host path, from Docker and the *arr apps.
    Failures are noted, never raised: a breakdown without owners is still useful."""
    locations = attribution.Locations()
    containers: list[dict[str, Any]] = []
    try:
        api = await _docker_api(ctx)
        containers = await api.get("/containers/json", all="true")
        info = await api.get("/info")
        attribution.add_docker(locations, containers, info)
    except (ToolError, CollectorUnavailable, PermissionError, httpx.HTTPError) as exc:
        locations.errors.append(f"docker: {exc}"[:200])
    roots: list[dict[str, Any]] = []
    try:
        apps = ctx.app.actions.clients.apps
    except AttributeError:
        apps = {}
    for (kind, name), app in apps.items():
        try:
            for folder in await app.root_folders():
                if folder.get("path"):
                    roots.append({"app": kind, "instance": name, "path": folder["path"]})
        except Exception as exc:
            locations.errors.append(f"{kind} {name}: {exc}"[:200])
    attribution.add_arr(locations, roots, containers)
    return locations


def _scan_runner(sandbox: Sandbox) -> Any:
    """The sandbox as a disk scan uses it: enough CPU time, and an IO priority that is not
    starved for as long as anything else (a torrent client) keeps the disk busy."""

    def run(stages: list[list[str]], **kwargs: Any) -> Any:
        return sandbox.run(stages, cpu_s=SCAN_CPU_S, io_best_effort=True, **kwargs)

    return run


async def _disk_usage(ctx: ToolContext, args: DiskUsageArgs) -> Any:
    path = args.path.strip()
    if not path.startswith("/"):
        raise ToolError("path must be absolute, e.g. /mnt/disks/disk14TB")
    path = path.rstrip("/") or "/"
    if why := _guard(ctx).reason(path):
        raise ToolError(f"refused: {why}")
    _require_linux()
    sandbox = _sandbox(ctx)
    chat = ctx.settings.chat
    budget = max(chat.tool_timeout.total_seconds() - SCAN_RESERVE_S, 30.0)
    run = _scan_runner(sandbox)
    if args.top_files:
        found = await asyncio.to_thread(disk.largest_files, run, path, args.top_files, 100, budget)
        try:
            locations = await asyncio.wait_for(_locations(ctx), ATTRIBUTION_TIMEOUT_S)
            for row in found["files"]:
                if who := locations.owners(row["path"]):
                    row["owner"] = who
        except TimeoutError:
            pass
        await _audit(ctx, "shell", cmd=f"disk_usage largest_files {path}", found=found["found"])
        return found
    hit = disk.cached(path, args.depth, ttl=chat.disk_cache_ttl.total_seconds())
    if hit is not None:
        return hit
    try:
        scanned = await asyncio.to_thread(disk.scan, run, path, args.depth, budget)
    except FileNotFoundError as exc:
        raise ToolError(f"cannot read {path}: {exc}") from exc
    try:
        locations = await asyncio.wait_for(_locations(ctx), ATTRIBUTION_TIMEOUT_S)
    except TimeoutError:
        locations = attribution.Locations(errors=["owner lookup timed out"])
    result = disk.summarize(scanned, path, args.depth, locations.owners, locations.inside(path))
    if locations.errors:
        result["owner_lookup_problems"] = locations.errors[:3]
    await _audit(
        ctx,
        "shell",
        cmd=f"disk_usage {path} depth={args.depth}",
        seconds=scanned.elapsed_s,
        complete=scanned.complete,
    )
    if scanned.complete:
        disk.remember(path, args.depth, result)
    return result


# --------------------------------------------------------------------------- containers


class ContainerArgs(_Args):
    name: str | None = Field(
        default=None, description="Container name or id. Omit to list all containers."
    )


async def _container_inspect(ctx: ToolContext, args: ContainerArgs) -> Any:
    api = await _docker_api(ctx)
    try:
        if not args.name:
            listing = await api.get("/containers/json", all="true")
            return {"containers": summarize_list(listing)}
        try:
            raw = await api.get(f"/containers/{quote(args.name, safe='')}/json")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            listing = await api.get("/containers/json", all="true")
            like = [
                c
                for c in listing
                if any(args.name.lower() in n.lower() for n in c.get("Names", []))
            ]
            if len(like) != 1:
                names = ", ".join(n for c in listing for n in c.get("Names", [])[:1])
                raise ToolError(f"no container {args.name!r}; containers: {names[:400]}") from None
            raw = await api.get(f"/containers/{like[0]['Id']}/json")
    except (CollectorUnavailable, PermissionError, httpx.HTTPError) as exc:
        raise ToolError(f"Docker API: {exc}"[:300]) from exc
    return summarize_inspect(raw)


class ListeningPortsArgs(_Args):
    port: int | None = Field(default=None, ge=1, le=65535, description="Only this port.")


_SS_LINE = re.compile(
    r"^(?P<state>LISTEN|UNCONN) +\d+ +\d+ +(?P<local>\S+) +\S+ *(?P<rest>.*)$",
    re.M,
)
_SS_PROCESS = re.compile(r'\("(?P<name>[^"]+)",pid=(?P<pid>\d+)')


def _docker_port_owners(listing: list[dict[str, Any]]) -> tuple[dict[int, list[str]], list[str]]:
    """Host port -> the containers publishing it, and the host-network containers."""
    owners: dict[int, list[str]] = {}
    host_network: list[str] = []
    for c in listing:
        name = str((c.get("Names") or ["?"])[0]).lstrip("/")
        if (c.get("HostConfig") or {}).get("NetworkMode") == "host":
            host_network.append(name)
        for p in c.get("Ports") or []:
            if p.get("PublicPort"):
                owners.setdefault(int(p["PublicPort"]), []).append(
                    f"{name} (container: published {p['PublicPort']}→{p.get('PrivatePort')}"
                    f"/{p.get('Type', 'tcp')})"
                )
    return owners, host_network


def _parse_ss(output: str) -> list[dict[str, Any]]:
    out = []
    for match in _SS_LINE.finditer(output):
        bind, _, port = match["local"].rpartition(":")
        if not port.isdigit():
            continue
        proc = _SS_PROCESS.search(match["rest"])
        out.append({"port": int(port), "bind": bind, "process": proc["name"] if proc else None})
    return out


async def _ss_listeners(ctx: ToolContext) -> tuple[list[dict[str, Any]], str | None]:
    """The host's listening TCP sockets (`ss -tlnp`) as {port, bind, process}; a note on why
    there are none (not Linux, refused, failed)."""
    try:
        _require_linux()
        plan = parse_pipeline("ss -tlnp", _guard(ctx))
        limit = ctx.settings.chat.max_tool_result_chars
        result = await asyncio.to_thread(
            _sandbox(ctx).run,
            plan.stages,
            timeout=max(ctx.settings.chat.tool_timeout.total_seconds() - 5, 5.0),
            max_chars=limit,
            collect_all=True,
        )
    except (ToolError, Refused, RuntimeError, OSError) as exc:
        return [], f"host sockets not readable: {exc}"[:200]
    out = _parse_ss(result.output)
    return out, None if out or result.exit_code == 0 else f"ss exited {result.exit_code}"


async def _listening_ports(ctx: ToolContext, args: ListeningPortsArgs) -> Any:
    owners: dict[int, list[str]] = {}
    host_network: list[str] = []
    notes: list[str] = []
    try:
        api = await _docker_api(ctx)
        owners, host_network = _docker_port_owners(await api.get("/containers/json"))
    except (ToolError, CollectorUnavailable, PermissionError, httpx.HTTPError) as exc:
        notes.append(f"Docker published ports unknown: {exc}"[:200])
    sockets, ss_note = await _ss_listeners(ctx)
    if ss_note:
        notes.append(ss_note)
    rows: dict[int, dict[str, Any]] = {}
    for s in sockets:
        row = rows.setdefault(s["port"], {"port": s["port"], "bind": s["bind"]})
        if s["process"]:
            row["process"] = s["process"]
    for port, who in owners.items():
        rows.setdefault(port, {"port": port})["container"] = who
    for row in rows.values():
        if "container" not in row and "process" not in row:
            row["owner"] = "unknown (another user's process or a host-network container)"
    wanted = sorted(rows.values(), key=lambda r: r["port"])
    if args.port is not None:
        wanted = [r for r in wanted if r["port"] == args.port]
        if not wanted:
            notes.append(f"nothing was found listening on port {args.port}")
    out: dict[str, Any] = {"listening": wanted[:80]}
    if host_network:
        out["host_network_containers"] = host_network[:20]
        notes.append(
            "host-network containers open their ports directly on the host: an 'unknown' "
            "listener may belong to one of them (container_inspect NAME shows its config)"
        )
    if notes:
        out["notes"] = notes
    await _audit(ctx, "shell", cmd=f"listening_ports {args.port or ''}".strip(), rows=len(wanted))
    return out


class ContainerLogsArgs(_Args):
    name: str = Field(description="Container name or id.")
    tail: int = Field(default=200, ge=1, le=2000, description="Newest lines to return.")
    contains: str | None = Field(
        default=None,
        max_length=200,
        description="Only lines containing this text, e.g. 'login'.",
    )
    since: str | None = Field(
        default=None,
        max_length=20,
        description="Only the last 30m, 2h, 1d...",
    )


_SINCE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.I)
_SINCE_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
LOG_LINE_CHARS = 400


def _since_timestamp(text: str) -> int:
    match = _SINCE.match(text)
    if not match:
        raise ToolError(f"since must look like 30m, 2h or 1d, not {text!r}")
    return int(time.time()) - int(match.group(1)) * _SINCE_SECONDS[match.group(2).lower()]


async def _resolve_container(api: Any, name: str) -> str:
    """The name Docker knows: the given one, or the single container whose name contains it."""
    listing = await api.get("/containers/json", all="true")
    names = [n.lstrip("/") for c in listing for n in c.get("Names", [])[:1]]
    if name.lstrip("/") in names:
        return name.lstrip("/")
    like = [n for n in names if name.lower() in n.lower()]
    if len(like) == 1:
        return like[0]
    shown = ", ".join(like or names)[:400]
    raise ToolError(f"no container {name!r}; {'matches' if like else 'containers'}: {shown}")


async def _container_logs(ctx: ToolContext, args: ContainerLogsArgs) -> Any:
    api = await _docker_api(ctx)
    params: dict[str, Any] = {
        "stdout": "1",
        "stderr": "1",
        "timestamps": "1",
        # (a filter reads further back: the matches are what counts, not the newest lines)
        "tail": str(max(args.tail, 2000) if args.contains else args.tail),
    }
    if args.since:
        params["since"] = str(_since_timestamp(args.since))
    try:
        name = await _resolve_container(api, args.name)
        raw = await api.get(f"/containers/{quote(name, safe='')}/logs", **params)
    except (CollectorUnavailable, PermissionError, httpx.HTTPError) as exc:
        raise ToolError(f"Docker API: {exc}"[:300]) from exc
    lines = demux_logs(bytes(raw)) if isinstance(raw, bytes | bytearray) else []
    read = len(lines)
    if args.contains:
        needle = args.contains.lower()
        lines = [line for line in lines if needle in line.lower()]
    lines = lines[-args.tail :]
    budget = int(ctx.settings.chat.max_tool_result_chars * RESULT_SHARE)
    kept: list[str] = []
    used = 0
    for line in reversed(lines):  # the newest lines matter most: drop the oldest first
        text = scrub_output(line[:LOG_LINE_CHARS])
        if used + len(text) + 1 > budget:
            break
        kept.append(text)
        used += len(text) + 1
    kept.reverse()
    await _audit(ctx, "docker", cmd=f"container_logs {name}", lines=len(kept))
    out: dict[str, Any] = {"container": name, "lines_read": read, "lines": kept}
    notes = []
    if args.contains and not lines:
        notes.append(f"none of the {read} lines read contain {args.contains!r}")
    if len(kept) < len(lines):
        notes.append(f"output too long: the oldest {len(lines) - len(kept)} lines were left out")
    if notes:
        out["notes"] = notes
    return out


# --------------------------------------------------------------------------- history


class MetricHistoryArgs(_Args):
    collector: str = Field(description="system, docker, gpu, systemd, journal, arr, ...")
    metric: str = Field(
        description="Dotted path into the collector data, e.g. filesystems.used, "
        "memory.percent, load.load1."
    )
    where: str | None = Field(
        default=None,
        description="Picks one list item as key=value, e.g. mount=/mnt/disks/disk14TB.",
    )
    hours: int = Field(default=24, ge=1, le=720, description="How far back.")


async def _metric_history_tool(ctx: ToolContext, args: MetricHistoryArgs) -> Any:
    try:
        return await _metric_history(
            ctx.app.db, args.collector, args.metric, args.where, args.hours
        )
    except MetricError as exc:
        raise ToolError(str(exc)) from exc


# --------------------------------------------------------------------------- registry


def host_tools(assume_linux: bool = False) -> list[Tool]:
    """`assume_linux`: offer the tools whatever this machine is (the model benchmarks put the
    real tool set in front of the model on a development PC, with the executors swapped)."""
    linux = lambda s: assume_linux or os.name == "posix"  # noqa: E731
    return [
        Tool(
            "run_command",
            "Run a read-only command on the host (as an unprivileged user). Use it to look at "
            "logs, units, processes, ports, files and disks. One command or a `|` pipeline; "
            "no shell syntax. Examples: journalctl -u logrotate -n 50 --no-pager | "
            "systemctl status logrotate --no-pager | ss -tlnp | ps aux --sort=-rss | head | "
            "df -h | cat /etc/logrotate.conf | ls -la /var/log.",
            RunCommandArgs,
            "read",
            _run_command,
            enabled=lambda s: s.chat.shell and linux(s),
        ),
        Tool(
            "disk_usage",
            "What takes the space under a host path: biggest directories (or files) with sizes "
            "and what owns them (Docker volume or bind mount, *arr library, Docker data...). "
            "Use this for 'what is on that disk?'. Slow on huge disks: gives partial results.",
            DiskUsageArgs,
            "read",
            _disk_usage,
            enabled=linux,
        ),
        Tool(
            "container_inspect",
            "Docker container details: state, health, restarts, image, ports, mounts, networks, "
            "compose project, env var NAMES. Without a name: list of all containers.",
            ContainerArgs,
            "read",
            _container_inspect,
            enabled=lambda s: s.collectors.docker.enabled is not False,
        ),
        Tool(
            "listening_ports",
            "Which port is open and WHO owns it: the host's listening sockets joined with the "
            "Docker containers that publish them (e.g. 8080 -> qbittorrent). Use this for 'what "
            "is listening on port X' instead of `ss`, which cannot name other users' processes.",
            ListeningPortsArgs,
            "read",
            _listening_ports,
            enabled=lambda s: s.collectors.docker.enabled is not False or linux(s),
        ),
        Tool(
            "container_logs",
            "Newest log lines of a Docker container (errors, failed logins, bans). "
            "Use this instead of `docker logs`.",
            ContainerLogsArgs,
            "read",
            _container_logs,
            enabled=lambda s: s.collectors.docker.enabled is not False,
        ),
        Tool(
            "metric_history",
            "How a collected number changed over the last hours/days (e.g. disk used), "
            "with a trend sentence.",
            MetricHistoryArgs,
            "read",
            _metric_history_tool,
        ),
    ]


__all__ = ["NoArgs", "host_tools"]
