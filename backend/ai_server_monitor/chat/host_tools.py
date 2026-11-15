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
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field

from ..collectors.base import CollectorUnavailable
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
ATTRIBUTION_TIMEOUT_S = 8.0

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
    budget = chat.disk_scan_budget.total_seconds()
    if args.top_files:
        found = await asyncio.to_thread(
            disk.largest_files, sandbox.run, path, args.top_files, 100, budget
        )
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
        scanned = await asyncio.to_thread(disk.scan, sandbox.run, path, args.depth, budget)
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


def host_tools() -> list[Tool]:
    linux = lambda s: os.name == "posix"  # noqa: E731
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
            "metric_history",
            "How a collected number changed over the last hours/days (e.g. disk used), "
            "with a trend sentence.",
            MetricHistoryArgs,
            "read",
            _metric_history_tool,
        ),
    ]


__all__ = ["NoArgs", "host_tools"]
