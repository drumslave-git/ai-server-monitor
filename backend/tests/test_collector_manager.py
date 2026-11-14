from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any, ClassVar

from servermon.collectors.base import Collector, CollectorUnavailable, HostEnv
from servermon.collectors.manager import CollectorManager
from servermon.config import Settings
from servermon.db import Database
from servermon.scheduler import Scheduler


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
