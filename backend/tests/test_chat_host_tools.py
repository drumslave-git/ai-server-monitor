"""The chat tools built on hostinspect: run_command, disk_usage, container_inspect and
metric_history, called the way the chat engine calls them (validated args → handler) with a
fake Docker API, and a scratch tree in place of the host."""

from __future__ import annotations

import json
import os
import struct
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ai_server_monitor.chat import audit, host_tools
from ai_server_monitor.chat.safety import Scrubber
from ai_server_monitor.chat.tools import ToolContext, ToolError, build_tools
from ai_server_monitor.collectors.base import HostEnv
from ai_server_monitor.config import Settings
from ai_server_monitor.db import Database
from ai_server_monitor.hostinspect import disk

posix_only = pytest.mark.skipif(os.name != "posix", reason="needs Linux")


NGINX_LOG = [
    "2026-11-15T10:00:00Z info: started",
    "2026-11-15T10:00:05Z warn: login failed from 10.0.0.5",
    "2026-11-15T10:00:06Z warn: Password=hunter2value was rejected",
    "2026-11-15T10:00:09Z info: all quiet",
]


def docker_log_frames(lines: list[str]) -> bytes:
    out = b""
    for line in lines:
        data = (line + "\n").encode()
        out += bytes([1, 0, 0, 0]) + struct.pack(">I", len(data)) + data
    return out


class FakeDocker:
    def __init__(self, tree: Path):
        self.tree = tree
        self.paths: list[str] = []

    async def get(self, path: str, **params: Any) -> Any:
        self.paths.append(path)
        if path == "/containers/json":
            return [
                {
                    "Id": "a" * 64,
                    "Names": ["/nginx"],
                    "Image": "lscr.io/linuxserver/nginx",
                    "State": "running",
                    "Status": "Up 2 days",
                    "Ports": [{"PublicPort": 7878, "PrivatePort": 7878, "Type": "tcp"}],
                    "Labels": {},
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": str(self.tree / "media"),
                            "Destination": "/data",
                            "RW": True,
                        }
                    ],
                }
            ]
        if path == "/info":
            return {"DockerRootDir": "/var/lib/docker"}
        if path == "/containers/nginx/logs":
            self.log_params = params
            return docker_log_frames(NGINX_LOG)
        if path in ("/containers/nginx/json", f"/containers/{'a' * 64}/json"):
            return {
                "Id": "a" * 64,
                "Name": "/nginx",
                "State": {"Status": "running", "Running": True},
                "Config": {"Image": "nginx", "Env": ["API_KEY=hunter2value", "TZ=UTC"]},
                "HostConfig": {},
                "Mounts": [],
                "NetworkSettings": {},
            }
        request = httpx.Request("GET", "http://docker" + path)
        raise httpx.HTTPStatusError("nope", request=request, response=httpx.Response(404))


def make_ctx(tmp_path: Path, db: Database, docker: Any = None, settings: Settings | None = None):  # type: ignore[no-untyped-def]
    settings = settings or Settings()
    app = SimpleNamespace(
        settings=settings,
        db=db,
        host_env=HostEnv(root=Path("/")),
        collectors=SimpleNamespace(
            collectors={"docker": SimpleNamespace(api=docker)} if docker else {}
        ),
    )
    return ToolContext(app, "sess-1")  # type: ignore[arg-type]


def call(tool_name: str, ctx: ToolContext, **args: Any):  # type: ignore[no-untyped-def]
    tool = {t.name: t for t in host_tools.host_tools()}[tool_name]
    return tool.handler(ctx, tool.args_model.model_validate(args))


@pytest.fixture(autouse=True)
def _fresh() -> None:
    disk.clear_cache()


# --------------------------------------------------------------------------- run_command


@pytest.mark.parametrize(
    "cmd",
    [
        "ls; id",
        "ls && id",
        "echo $(id)",
        "ls > /tmp/x",
        "find / -delete",
        "journalctl --vacuum-size=1M",
        "cat /etc/shadow",
        "cat /srv/app/.env",
        "curl http://x",
        "bash -c id",
        "python3 -c 1",
    ],
)
async def test_run_command_refuses_and_audits(cmd: str, tmp_path: Path, db: Database) -> None:
    ctx = make_ctx(tmp_path, db)
    with pytest.raises(ToolError, match="refused"):
        await call("run_command", ctx, cmd=cmd)
    rows = await db.kv_get(audit.AUDIT_KEY)
    assert rows[-1]["cmd"] == cmd and "refused" in rows[-1]
    assert rows[-1]["session"] == "sess-1" and rows[-1]["kind"] == "shell"


@pytest.mark.parametrize("cmd", ["docker logs whoami", "podman ps", "kubectl logs x"])
async def test_container_clients_are_refused_with_the_replacement_tools(
    cmd: str, tmp_path: Path, db: Database
) -> None:
    ctx = make_ctx(tmp_path, db)
    with pytest.raises(ToolError) as caught:
        await call("run_command", ctx, cmd=cmd)
    for tool in ("container_logs", "container_inspect"):
        assert f"`{tool}`" in str(caught.value)


@posix_only
async def test_run_command_runs_audits_and_scrubs(tmp_path: Path, db: Database) -> None:
    (tmp_path / "app.conf").write_text("name = demo\ndb_password = hunter2hunter2\nport = 8080\n")
    ctx = make_ctx(tmp_path, db)
    result = await call("run_command", ctx, cmd=f"cat {tmp_path}/app.conf | sort")
    assert result["exit_code"] == 0
    assert "port = 8080" in result["output"]
    assert "hunter2" not in result["output"]
    rows = await db.kv_get(audit.AUDIT_KEY)
    assert rows[-1]["cmd"].startswith("cat ") and rows[-1]["exit"] == 0
    assert rows[-1]["bytes"] > 0 and "seconds" in rows[-1]


@posix_only
async def test_run_command_failure_carries_stderr(tmp_path: Path, db: Database) -> None:
    ctx = make_ctx(tmp_path, db)
    result = await call("run_command", ctx, cmd=f"ls {tmp_path}/missing")
    assert result["exit_code"] != 0
    assert "No such file" in result["stderr"]


@posix_only
async def test_run_command_output_fits_the_result_limit(tmp_path: Path, db: Database) -> None:
    (tmp_path / "big.log").write_text("".join(f"line {i} some text here\n" for i in range(20000)))
    settings = Settings()
    ctx = make_ctx(tmp_path, db, settings=settings)
    result = await call("run_command", ctx, cmd=f"cat {tmp_path}/big.log")
    text = json.dumps(result, separators=(",", ":"))
    assert len(text) <= settings.chat.max_tool_result_chars
    assert "lines cut" in result["output"]
    assert any("too long" in n for n in result["notes"])
    assert "line 0 " in result["output"] and "line 19999 " in result["output"]


async def test_run_command_can_be_switched_off(tmp_path: Path, db: Database) -> None:
    off = Settings.model_validate({"chat": {"shell": False}})
    assert "run_command" not in build_tools(off)
    ctx = make_ctx(tmp_path, db, settings=off)
    with pytest.raises(ToolError, match="switched off"):
        await host_tools._run_command(ctx, host_tools.RunCommandArgs(cmd="ls"))
    if os.name == "posix":
        assert "run_command" in build_tools(Settings())  # on by default


async def test_engine_scrubber_still_applies_to_command_output() -> None:
    # (the engine runs every tool result through Scrubber; the command output goes through it too)
    cleaned = Scrubber(Settings()).clean({"output": "Authorization: Bearer abcdefgh12345678"})
    assert "abcdefgh12345678" not in json.dumps(cleaned)


# --------------------------------------------------------------------------- disk_usage


@posix_only
async def test_disk_usage_breakdown_with_attribution(tmp_path: Path, db: Database) -> None:
    tree = tmp_path / "tree"
    media = tree / "media"
    for sub, size in (("movies", 300_000), ("tv", 100_000)):
        (media / sub).mkdir(parents=True)
        (media / sub / "file.bin").write_bytes(b"x" * size)
    (tree / "downloads").mkdir()
    (tree / "downloads" / "a.iso").write_bytes(b"y" * 50_000)
    ctx = make_ctx(tmp_path, db, docker=FakeDocker(tree))
    result = await call("disk_usage", ctx, path=str(tree), depth=2)
    largest = {row["path"]: row for row in result["largest"]}
    assert result["largest"][0]["path"] == str(media)
    assert str(media / "movies") in largest
    assert any("bind mount of container 'nginx'" in o for o in largest[str(media)]["owner"])
    assert any("inside bind mount" in o for o in largest[str(media / "movies")]["owner"])
    assert "approximate" not in result
    # second call is served from the cache
    again = await call("disk_usage", ctx, path=str(tree), depth=2)
    assert "cached_age_s" in again


@posix_only
async def test_disk_usage_largest_files(tmp_path: Path, db: Database) -> None:
    tree = tmp_path / "tree"
    (tree / "media").mkdir(parents=True)
    (tree / "media" / "big.mkv").write_bytes(b"x" * 2_000_000)
    (tree / "small.txt").write_text("hi")
    ctx = make_ctx(tmp_path, db, docker=FakeDocker(tree))
    result = await host_tools._disk_usage(
        ctx, host_tools.DiskUsageArgs(path=str(tree), top_files=3)
    )
    assert result["files"] == []  # the threshold is 100 MB: nothing that big in a test
    assert "no files over 100 MB" in result["note"]


async def test_disk_usage_refuses_denied_and_relative_paths(tmp_path: Path, db: Database) -> None:
    ctx = make_ctx(tmp_path, db)
    with pytest.raises(ToolError, match="absolute"):
        await call("disk_usage", ctx, path="mnt/disk")
    with pytest.raises(ToolError, match="deny list"):
        await call("disk_usage", ctx, path="/home/a/.ssh")
    with pytest.raises(ValueError, match="less than or equal to 3"):  # the engine reports this
        await call("disk_usage", ctx, path="/x", depth=9)


@posix_only
async def test_disk_usage_without_docker_still_answers(tmp_path: Path, db: Database) -> None:
    tree = tmp_path / "tree"
    (tree / "a").mkdir(parents=True)
    (tree / "a" / "f").write_bytes(b"z" * 10_000)
    ctx = make_ctx(tmp_path, db, docker=None)
    result = await call("disk_usage", ctx, path=str(tree))
    assert result["largest"][0]["path"] == str(tree / "a")
    assert result["owner_lookup_problems"][0].startswith("docker:")


# --------------------------------------------------------------------------- containers


async def test_container_inspect_tool(tmp_path: Path, db: Database) -> None:
    ctx = make_ctx(tmp_path, db, docker=FakeDocker(tmp_path))
    listing = await call("container_inspect", ctx)
    assert listing["containers"][0]["name"] == "nginx"
    assert listing["containers"][0]["ports"] == ["7878->7878/tcp"]
    one = await call("container_inspect", ctx, name="nginx")
    assert one["env_names"] == ["API_KEY", "TZ"]
    assert "hunter2value" not in json.dumps(one)
    # partial name resolves through the list when unique
    again = await call("container_inspect", ctx, name="ngin")
    assert again["name"] == "nginx"
    with pytest.raises(ToolError, match="no container 'zzz'"):
        await call("container_inspect", ctx, name="zzz")


async def test_container_logs_demuxed_scrubbed_and_audited(tmp_path: Path, db: Database) -> None:
    docker = FakeDocker(tmp_path)
    ctx = make_ctx(tmp_path, db, docker=docker)
    out = await call("container_logs", ctx, name="ngin", tail=3)  # partial name resolves
    assert out["container"] == "nginx" and out["lines_read"] == 4
    assert len(out["lines"]) == 3 and out["lines"][-1].endswith("all quiet")
    assert "hunter2value" not in json.dumps(out) and "login failed" in out["lines"][0]
    assert docker.log_params["tail"] == "3" and docker.log_params["timestamps"] == "1"
    rows = await db.kv_get(audit.AUDIT_KEY)
    assert rows[-1]["cmd"] == "container_logs nginx"


async def test_container_logs_filter_since_and_errors(tmp_path: Path, db: Database) -> None:
    docker = FakeDocker(tmp_path)
    ctx = make_ctx(tmp_path, db, docker=docker)
    out = await call("container_logs", ctx, name="nginx", contains="LOGIN", since="30m")
    assert out["lines"] == [NGINX_LOG[1]]
    assert docker.log_params["tail"] == "2000" and int(docker.log_params["since"]) > 1_000_000_000
    none = await call("container_logs", ctx, name="nginx", contains="zebra")
    assert none["lines"] == [] and "none of the 4 lines" in none["notes"][0]
    with pytest.raises(ToolError, match="since must look like"):
        await call("container_logs", ctx, name="nginx", since="yesterday")
    with pytest.raises(ToolError, match=r"no container 'zzz'.*nginx"):
        await call("container_logs", ctx, name="zzz")


async def test_container_logs_output_fits_the_result_limit(tmp_path: Path, db: Database) -> None:
    class Many(FakeDocker):
        async def get(self, path: str, **params: Any) -> Any:
            if path == "/containers/nginx/logs":
                return docker_log_frames([f"line {i} " + "x" * 200 for i in range(500)])
            return await super().get(path, **params)

    ctx = make_ctx(tmp_path, db, docker=Many(tmp_path))
    out = await call("container_logs", ctx, name="nginx", tail=500)
    limit = ctx.settings.chat.max_tool_result_chars
    assert len(json.dumps(out)) <= limit
    assert out["lines"][-1].startswith("line 499") and "oldest" in out["notes"][0]


async def test_container_logs_without_docker(tmp_path: Path, db: Database) -> None:
    ctx = make_ctx(tmp_path, db, docker=None)
    with pytest.raises(ToolError, match="Docker API is not available"):
        await call("container_logs", ctx, name="nginx")


class PortsDocker(FakeDocker):
    async def get(self, path: str, **params: Any) -> Any:
        if path != "/containers/json":
            return await super().get(path, **params)
        extra = [
            {
                "Names": ["/whoami"],
                "Image": "lscr.io/linuxserver/whoami",
                "State": "running",
                "Ports": [
                    {"IP": "0.0.0.0", "PrivatePort": 8080, "PublicPort": 8080, "Type": "tcp"}
                ],
            },
            {"Names": ["/plex"], "Image": "plex", "HostConfig": {"NetworkMode": "host"}},
        ]
        return [*await super().get(path, **params), *extra]


SS_OUTPUT = (
    "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
    "LISTEN 0      4096         0.0.0.0:8080      0.0.0.0:*\n"
    "LISTEN 0      128          0.0.0.0:22        0.0.0.0:*     "
    'users:(("sshd",pid=801,fd=3))\n'
    "LISTEN 0      4096   127.0.0.53%lo:53        0.0.0.0:*\n"
    "LISTEN 0      4096            [::]:7878         [::]:*\n"
)


def test_ss_output_is_parsed() -> None:
    rows = host_tools._parse_ss(SS_OUTPUT)
    assert [(r["port"], r["bind"], r["process"]) for r in rows] == [
        (8080, "0.0.0.0", None),
        (22, "0.0.0.0", "sshd"),
        (53, "127.0.0.53%lo", None),
        (7878, "[::]", None),
    ]


async def test_listening_ports_names_the_container_behind_a_port(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_ss(ctx: ToolContext) -> Any:
        return host_tools._parse_ss(SS_OUTPUT), None

    monkeypatch.setattr(host_tools, "_ss_listeners", fake_ss)
    ctx = make_ctx(tmp_path, db, docker=PortsDocker(tmp_path))
    out = await call("listening_ports", ctx, port=8080)
    (row,) = out["listening"]
    assert (
        row["port"] == 8080
        and "whoami (container: published 8080→8080/tcp)" in (row["container"][0])
    )
    everything = await call("listening_ports", ctx)
    by_port = {r["port"]: r for r in everything["listening"]}
    assert by_port[22]["process"] == "sshd"
    assert "nginx" in by_port[7878]["container"][0]
    assert "unknown" in by_port[53]["owner"]
    assert everything["host_network_containers"] == ["plex"]
    assert any("host-network" in n for n in everything["notes"])
    nothing = await call("listening_ports", ctx, port=9999)
    assert nothing["listening"] == [] and "nothing was found" in nothing["notes"][0]


async def test_listening_ports_without_docker_or_ss_says_so(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_ss(ctx: ToolContext) -> Any:
        return [], "host sockets not readable: needs Linux"

    monkeypatch.setattr(host_tools, "_ss_listeners", no_ss)
    out = await call("listening_ports", make_ctx(tmp_path, db, docker=None))
    assert out["listening"] == [] and len(out["notes"]) == 2


async def test_gpu_default_view_lists_the_top_processes_by_vram(db: Database) -> None:
    from ai_server_monitor.collectors.base import CollectorResult

    procs = [{"pid": i, "name": f"p{i}", "mem_used": (i + 1) * 1024**3} for i in range(7)]
    data = {
        "driver": "560",
        "gpus": [
            {
                "index": 0,
                "name": "RTX 4090",
                "util_percent": 6,
                "mem_used": 16 * 1024**3,
                "mem_total": 24 * 1024**3,
                "uuid": "GPU-secret-looking",
                "processes": procs,
            }
        ],
    }
    manager = SimpleNamespace(
        registry={"gpu": object()},
        status=lambda: {"gpu": {"state": "ok", "reason": None}},
        latest={"gpu": CollectorResult("gpu", time.time(), True, data=data)},
    )
    ctx = ToolContext(SimpleNamespace(settings=Settings(), db=db, collectors=manager), "s")  # type: ignore[arg-type]
    tool = build_tools(Settings(), assume_linux=True)["get_collector_data"]
    out = await tool.handler(ctx, tool.args_model.model_validate({"collector": "gpu"}))
    (gpu,) = out["default_view"]["gpus"]
    top = gpu["top_processes_by_vram"]
    assert [p["name"] for p in top] == ["p6", "p5", "p4", "p3", "p2"]
    assert top[0]["mem_used"] == 7 * 1024**3 and gpu["more_processes"] == 2
    assert "uuid" not in gpu
    assert "gpus" in out["available_sections"] and "gpus" in out["sections"]


async def test_failed_tools_get_a_fallback_hint(tmp_path: Path) -> None:
    from ai_server_monitor.chat.engine import ChatEngine
    from ai_server_monitor.chat.tools import failure_hint

    assert "df -h" in failure_hint("disk_usage") and failure_hint("no_such_tool") == ""

    async def boom(ctx: Any, args: Any) -> Any:
        raise ToolError("cannot read /mnt/disks/backup: scan failed")

    tool = {t.name: t for t in host_tools.host_tools(True)}["disk_usage"]
    failing = type(tool)(
        tool.name, tool.description, tool.args_model, "read", boom, enabled=lambda s: True
    )
    engine = ChatEngine.__new__(ChatEngine)
    turn = SimpleNamespace(
        scrub=Scrubber(Settings()),
        timeout=5.0,
        ctx=None,
        limit_chars=5000,
        tools={},
        route=None,
        visible=list,
    )
    ok, text, _ = await engine._execute(turn, failing, "disk_usage", {"path": "/x"}, None)  # type: ignore[arg-type]
    assert ok is False and "scan failed" in text and "Do not stop here: try run_command" in text

    async def refuse(ctx: Any, args: Any) -> Any:
        raise ToolError("refused: nope")

    refused = type(tool)(
        tool.name, tool.description, tool.args_model, "read", refuse, enabled=lambda s: True
    )
    _, text, _ = await engine._execute(turn, refused, "disk_usage", {"path": "/x"}, None)  # type: ignore[arg-type]
    assert text == "refused: nope"  # a refusal is not a failure to work around


def test_prompt_rules_for_failures_writes_and_ports() -> None:
    from ai_server_monitor.chat.prompts import system_prompt

    text = system_prompt("en")
    assert 'do not answer "unable to retrieve"' in text and "`listening_ports`" in text
    assert "safe default" in text


async def test_container_inspect_without_docker(tmp_path: Path, db: Database) -> None:
    ctx = make_ctx(tmp_path, db, docker=None)
    with pytest.raises(ToolError, match="Docker API is not available"):
        await call("container_inspect", ctx)


# --------------------------------------------------------------------------- history


async def test_metric_history_tool(tmp_path: Path, db: Database) -> None:
    import time

    now = time.time()
    for i in range(10):
        data = {"filesystems": [{"mount": "/d", "used": 1000 * (i + 1)}]}
        await db.execute(
            "INSERT INTO collector_samples (ts, collector, ok, data) VALUES (?, 'system', 1, ?)",
            (now - (10 - i) * 3600, json.dumps(data)),
        )
    ctx = make_ctx(tmp_path, db)
    out = await call(
        "metric_history", ctx, collector="system", metric="filesystems.used", where="mount=/d"
    )
    assert out["samples"] == 10 and out["trend"].startswith("Grew")
    with pytest.raises(ToolError, match="key=value"):
        await call("metric_history", ctx, collector="system", metric="x", where="oops")


# --------------------------------------------------------------------------- registry / size


def test_tools_are_registered_as_read_tools_with_small_schemas() -> None:
    tools = build_tools(Settings())
    for name in ("metric_history", "container_inspect", "container_logs"):
        assert tools[name].kind == "read"
    if os.name == "posix":
        assert tools["run_command"].kind == "read" and tools["disk_usage"].kind == "read"
    size = sum(
        len(json.dumps(tools[n].definition(), separators=(",", ":")))
        for n in tools
        if n
        in ("run_command", "disk_usage", "container_inspect", "container_logs", "metric_history")
    )
    assert size < 3400  # ~850 tokens: a 4B model has to read all of it on every call
