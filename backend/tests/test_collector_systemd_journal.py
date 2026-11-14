from __future__ import annotations

import json
from typing import Any

import pytest

from servermon.collectors import journal as journal_mod
from servermon.collectors.base import HostEnv
from servermon.collectors.journal import JournalCollector, normalize_message, summarize_journal
from servermon.collectors.systemd import (
    MANAGER_IFACE,
    SERVICE_IFACE,
    UNIT_IFACE,
    DbusError,
    SystemdCollector,
)
from servermon.config import JournalCollectorConfig, SystemdCollectorConfig


def unit(name: str, active: str, sub: str, load: str = "loaded") -> list[Any]:
    path = "/org/freedesktop/systemd1/unit/" + name.replace(".", "_2e").replace("-", "_2d")
    return [name, f"{name} desc", load, active, sub, "", path, 0, "", "/"]


class FakeBus:
    def __init__(self) -> None:
        self.units = [
            unit("ssh.service", "active", "running"),
            unit("backup.service", "failed", "failed"),
            unit("flappy.service", "active", "running"),
            unit("dev-sda.device", "active", "plugged"),
            unit("old.service", "inactive", "dead", load="not-found"),
        ]
        self.restarts = {"ssh.service": 0, "backup.service": 3, "flappy.service": 5}
        self.closed = False

    async def list_units(self) -> list[list[Any]]:
        return self.units

    async def get_property(self, path: str, interface: str, name: str) -> Any:
        if interface == MANAGER_IFACE and name == "SystemState":
            return "degraded"
        unit_name = next(u[0] for u in self.units if u[6] == path)
        if interface == SERVICE_IFACE and name == "NRestarts":
            return self.restarts[unit_name]
        if interface == SERVICE_IFACE and name == "Result":
            return "exit-code"
        if interface == UNIT_IFACE and name == "StateChangeTimestamp":
            return 1_794_600_000_000_000
        raise DbusError("org.freedesktop.DBus.Error.UnknownProperty")

    async def close(self) -> None:
        self.closed = True


async def test_systemd_collect() -> None:
    bus = FakeBus()
    collector = SystemdCollector(SystemdCollectorConfig(), HostEnv(), bus=bus)
    first = await collector.collect()

    assert first["system_state"] == "degraded"
    assert first["units_total"] == 5
    assert first["by_active_state"] == {"active": 3, "failed": 1, "inactive": 1}
    assert first["failed"] == [
        {
            "name": "backup.service",
            "description": "backup.service desc",
            "sub": "failed",
            "since": 1_794_600_000,
            "result": "exit-code",
        }
    ]
    assert {r["name"] for r in first["restarting"]} == {"flappy.service", "backup.service"}
    assert all(r["delta"] == 0 for r in first["restarting"])

    bus.restarts["flappy.service"] = 9
    second = await collector.collect()
    assert second["restarting"][0] == {"name": "flappy.service", "n_restarts": 9, "delta": 4}


def test_systemd_default_bus_address_uses_host_root(tmp_path) -> None:  # type: ignore[no-untyped-def]
    collector = SystemdCollector(SystemdCollectorConfig(), HostEnv(root=tmp_path), bus=FakeBus())
    assert collector._address() == f"unix:path={tmp_path / 'run/dbus/system_bus_socket'}"
    custom = SystemdCollector(
        SystemdCollectorConfig(bus_address="unix:path=/x"), HostEnv(), bus=FakeBus()
    )
    assert custom._address() == "unix:path=/x"


def entry(msg: Any, prio: int = 3, **fields: Any) -> dict[str, Any]:
    return {"MESSAGE": msg, "PRIORITY": str(prio), "__REALTIME_TIMESTAMP": "1794600000000000",
            "__CURSOR": f"c-{msg}", **fields}  # fmt: skip


def test_normalize_message() -> None:
    assert normalize_message(
        "pid 1234 failed at 0xdeadbeef in 550e8400-e29b-41d4-a716-446655440000"
    ) == ("pid # failed at <hex> in <uuid>")


def test_summarize_journal() -> None:
    entries = [
        entry("Connection from 10.0.0.1 port 5000 refused", 4, _SYSTEMD_UNIT="ssh.service"),
        entry("Connection from 10.0.0.2 port 6000 refused", 3, _SYSTEMD_UNIT="ssh.service"),
        entry("Out of memory: Killed process 4242 (python3)", 3, _TRANSPORT="kernel"),
        entry("blk_update_request: I/O error, dev sdb, sector 42", 3, _TRANSPORT="kernel"),
        entry(list(b"binary \xff message"), 4, SYSLOG_IDENTIFIER="weird"),
        entry(
            "db connection lost", 3, CONTAINER_NAME="immich_server", _SYSTEMD_UNIT="docker.service"
        ),
    ]
    summary = summarize_journal(entries, limit=100)

    assert summary["entries"] == 6 and summary["truncated"] is False
    assert summary["by_priority"] == {"warning": 2, "err": 4}
    counts = {s["source"]: s["count"] for s in summary["top_sources"]}
    assert counts["kernel"] == 2 and counts["ssh.service"] == 2 and counts["immich_server"] == 1
    ssh = next(g for g in summary["top_messages"] if g["source"] == "ssh.service")
    assert ssh["count"] == 2 and ssh["priority"] == "err"  # grouped, worst priority kept
    assert summary["notable"]["oom"]["count"] == 1
    assert summary["notable"]["disk_io"]["samples"][0].startswith("kernel: blk_update_request")
    assert any(g["source"] == "immich_server" for g in summary["top_messages"])
    assert any("binary" in g["sample"] for g in summary["top_messages"])


async def test_journal_collector_uses_cursor(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    journal_dir = tmp_path / "var/log/journal"
    (journal_dir / "machine").mkdir(parents=True)
    calls: list[list[str]] = []
    outputs = [
        "\n".join(json.dumps(e) for e in [entry("first"), entry("second")]),
        "",
    ]

    async def fake_run(args: list[str], **kwargs: Any) -> str:
        calls.append(args)
        return outputs.pop(0)

    monkeypatch.setattr(journal_mod.shutil, "which", lambda name: "/usr/bin/journalctl")
    monkeypatch.setattr(journal_mod, "run_command", fake_run)
    collector = JournalCollector(JournalCollectorConfig(), HostEnv(root=tmp_path))
    await collector.check()

    first = await collector.collect()
    assert first["entries"] == 2
    assert f"--directory={journal_dir}" in calls[0]
    assert any(a.startswith("--since=@") for a in calls[0])
    assert "--priority=warning" in calls[0]

    second = await collector.collect()
    assert second["entries"] == 0
    assert "--after-cursor=c-second" in calls[1]
    assert not any(a.startswith("--since") for a in calls[1])
