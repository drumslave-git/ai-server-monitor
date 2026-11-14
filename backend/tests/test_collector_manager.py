from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any, ClassVar

from ai_server_monitor.collectors.base import Collector, CollectorUnavailable, HostEnv
from ai_server_monitor.collectors.manager import CollectorManager
from ai_server_monitor.config import Settings
from ai_server_monitor.db import Database
from ai_server_monitor.scheduler import Scheduler


class OkCollector(Collector):
    name = "system"
    calls: ClassVar[int] = 0

    async def collect(self) -> dict[str, Any]:
        OkCollector.calls += 1
        return {"value": OkCollector.calls, "heavy": "x" * 10}

    def persist_view(self, data: dict[str, Any]) -> dict[str, Any]:
        return {"value": data["value"]}


class FailingCollector(Collector):
    name = "docker"

    async def collect(self) -> dict[str, Any]:
        raise RuntimeError("socket exploded")


class MissingAutoCollector(Collector):
    name = "gpu"

    async def check(self) -> None:
        raise CollectorUnavailable("no GPU")

    async def collect(self) -> dict[str, Any]:  # pragma: no cover - never scheduled
        raise AssertionError


class MissingExplicitCollector(Collector):
    name = "systemd"

    async def check(self) -> None:
        raise CollectorUnavailable("no dbus")

    async def collect(self) -> dict[str, Any]:
        raise CollectorUnavailable("no dbus")


class SlowCollector(Collector):
    name = "journal"
    timeout = timedelta(milliseconds=50)

    async def collect(self) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {}


class DisabledCollector(Collector):
    name = "apt"

    async def collect(self) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError


ALL = (OkCollector, FailingCollector, MissingAutoCollector, MissingExplicitCollector,
       SlowCollector, DisabledCollector)  # fmt: skip
REGISTRY = {c.name: c for c in ALL}


class CrashingCheckCollector(Collector):
    """Like the D-Bus EOFError seen on a real host: check() raises something unexpected."""

    name = "systemd"

    async def check(self) -> None:
        raise EOFError

    async def collect(self) -> dict[str, Any]:
        raise EOFError


class BrokenInitCollector(Collector):
    name = "docker"

    def __init__(self, cfg: Any, env: HostEnv):
        raise ValueError("unsupported docker url: ftp://x")

    async def collect(self) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError


class HangingCheckCollector(Collector):
    name = "journal"
    timeout = timedelta(milliseconds=50)

    async def check(self) -> None:
        await asyncio.sleep(5)

    async def collect(self) -> dict[str, Any]:
        return {}


async def test_setup_survives_any_collector_failure(db: Database) -> None:
    settings = Settings(collectors={"journal": {"enabled": "auto"}})
    scheduler = Scheduler()
    registry = {c.name: c for c in (CrashingCheckCollector, BrokenInitCollector,
                                    HangingCheckCollector, OkCollector)}  # fmt: skip
    manager = CollectorManager(settings, db, scheduler, registry=registry, env=HostEnv())
    await manager.setup()  # must not raise

    status = manager.status()
    assert status["systemd"]["state"] == "active"  # explicitly enabled: keeps retrying
    assert status["systemd"]["reason"] == "check failed: EOFError"
    assert status["docker"]["state"] == "unavailable"
    assert "unsupported docker url" in (status["docker"]["reason"] or "")
    assert status["journal"]["state"] == "unavailable"  # auto + hung check
    assert status["system"]["state"] == "active"
    result = await manager.run("systemd")  # and running it reports instead of crashing
    assert not result.ok and result.error == "EOFError"
    await manager.close()


async def test_manager_lifecycle(db: Database) -> None:
    settings = Settings(collectors={"gpu": {"enabled": "auto"}, "apt": {"enabled": False}})
    scheduler = Scheduler()
    manager = CollectorManager(settings, db, scheduler, registry=REGISTRY, env=HostEnv())
    await manager.setup()

    assert manager.states == {
        "system": "active",
        "docker": "active",
        "gpu": "unavailable",
        "systemd": "active",  # explicitly enabled: kept, reports errors
        "journal": "active",
        "apt": "disabled",
    }
    assert set(scheduler.jobs) == {
        "collect:system", "collect:docker", "collect:systemd", "collect:journal"}  # fmt: skip

    ok = await manager.run("system")
    assert ok.ok and ok.data["value"] >= 1
    failed = await manager.run("docker")
    assert not failed.ok and failed.error == "RuntimeError: socket exploded"
    missing = await manager.run("systemd")
    assert missing.unavailable and manager.status()["systemd"]["reason"] == "no dbus"
    slow = await manager.run("journal")
    assert not slow.ok and "timed out" in (slow.error or "")

    status = manager.status()
    assert status["gpu"] == {"state": "unavailable", "reason": "no GPU", "last_ts": None,
                             "ok": None, "error": None, "duration_s": None}  # fmt: skip
    assert status["system"]["ok"] is True

    rows = await db.fetchall("SELECT collector, ok, data FROM collector_samples ORDER BY id")
    system_row = next(r for r in rows if r["collector"] == "system")
    assert json.loads(system_row["data"]) == {"value": ok.data["value"]}  # persist_view applied
    assert next(r for r in rows if r["collector"] == "docker")["ok"] == 0
    await manager.close()


async def test_persist_spacing(db: Database) -> None:
    settings = Settings(storage={"sample_interval": "5m"})
    manager = CollectorManager(
        settings, db, Scheduler(), registry={"system": OkCollector}, env=HostEnv()
    )
    await manager.setup()
    for _ in range(3):
        await manager.run("system")
    count = await db.fetchone("SELECT COUNT(*) FROM collector_samples")
    assert count[0] == 1  # later samples within 5 minutes are only kept in memory
    assert manager.latest["system"].data["value"] == OkCollector.calls
