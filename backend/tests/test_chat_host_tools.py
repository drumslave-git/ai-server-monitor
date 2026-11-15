"""The chat tools built on hostinspect: run_command, disk_usage, container_inspect and
metric_history, called the way the chat engine calls them (validated args → handler) with a
fake Docker API, fake *arr app and a scratch tree in place of the host."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ai_server_monitor.chat import host_tools
from ai_server_monitor.chat.safety import Scrubber
from ai_server_monitor.chat.tools import ToolContext, ToolError, build_tools
from ai_server_monitor.collectors.base import HostEnv
from ai_server_monitor.config import Settings
from ai_server_monitor.db import Database
from ai_server_monitor.hostinspect import disk

posix_only = pytest.mark.skipif(os.name != "posix", reason="needs Linux")


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
                    "Names": ["/radarr"],
                    "Image": "lscr.io/linuxserver/radarr",
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
        if path in ("/containers/radarr/json", f"/containers/{'a' * 64}/json"):
            return {
                "Id": "a" * 64,
                "Name": "/radarr",
                "State": {"Status": "running", "Running": True},
                "Config": {"Image": "radarr", "Env": ["API_KEY=hunter2value", "TZ=UTC"]},
                "HostConfig": {},
                "Mounts": [],
                "NetworkSettings": {},
            }
        request = httpx.Request("GET", "http://docker" + path)
        raise httpx.HTTPStatusError("nope", request=request, response=httpx.Response(404))


class FakeArr:
    async def root_folders(self) -> list[dict[str, Any]]:
        return [{"path": "/data/movies", "freeSpace": 1}]


def make_ctx(tmp_path: Path, db: Database, docker: Any = None, settings: Settings | None = None):  # type: ignore[no-untyped-def]
    settings = settings or Settings()
    app = SimpleNamespace(
        settings=settings,
        db=db,
        host_env=HostEnv(root=Path("/")),
        collectors=SimpleNamespace(
            collectors={"docker": SimpleNamespace(api=docker)} if docker else {}
        ),
        actions=SimpleNamespace(clients=SimpleNamespace(apps={("radarr", "main"): FakeArr()})),
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
    audit = await db.kv_get(host_tools.AUDIT_KEY)
    assert audit[-1]["cmd"] == cmd and "refused" in audit[-1]
    assert audit[-1]["session"] == "sess-1"


@posix_only
async def test_run_command_runs_audits_and_scrubs(tmp_path: Path, db: Database) -> None:
    (tmp_path / "app.conf").write_text("name = demo\ndb_password = hunter2hunter2\nport = 8080\n")
    ctx = make_ctx(tmp_path, db)
    result = await call("run_command", ctx, cmd=f"cat {tmp_path}/app.conf | sort")
    assert result["exit_code"] == 0
    assert "port = 8080" in result["output"]
    assert "hunter2" not in result["output"]
    audit = await db.kv_get(host_tools.AUDIT_KEY)
    assert audit[-1]["cmd"].startswith("cat ") and audit[-1]["exit"] == 0
    assert audit[-1]["bytes"] > 0 and "seconds" in audit[-1]


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


async def test_run_command_can_be_switched_off(
    tmp_path: Path, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_tools, "SHELL_ENABLED", False)
    assert "run_command" not in build_tools(Settings())
    ctx = make_ctx(tmp_path, db)
    with pytest.raises(ToolError, match="switched off"):
        await host_tools._run_command(ctx, host_tools.RunCommandArgs(cmd="ls"))


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
    assert any("bind mount of container 'radarr'" in o for o in largest[str(media)]["owner"])
    assert any("inside bind mount" in o for o in largest[str(media / "movies")]["owner"])
    assert "approximate" not in result
    assert any(
        "radarr 'main' root folder /data/movies" in row["owner"] for row in result["owners_below"]
    )
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
    assert listing["containers"][0]["name"] == "radarr"
    assert listing["containers"][0]["ports"] == ["7878->7878/tcp"]
    one = await call("container_inspect", ctx, name="radarr")
    assert one["env_names"] == ["API_KEY", "TZ"]
    assert "hunter2value" not in json.dumps(one)
    # partial name resolves through the list when unique
    again = await call("container_inspect", ctx, name="rada")
    assert again["name"] == "radarr"
    with pytest.raises(ToolError, match="no container 'zzz'"):
        await call("container_inspect", ctx, name="zzz")


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
    for name in ("metric_history", "container_inspect"):
        assert tools[name].kind == "read"
    if os.name == "posix":
        assert tools["run_command"].kind == "read" and tools["disk_usage"].kind == "read"
    size = sum(
        len(json.dumps(tools[n].definition(), separators=(",", ":")))
        for n in tools
        if n in ("run_command", "disk_usage", "container_inspect", "metric_history")
    )
    assert size < 2600  # ~650 tokens: a 4B model has to read all of it on every call
