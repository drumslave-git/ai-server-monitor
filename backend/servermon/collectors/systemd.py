"""systemd unit health over D-Bus: failed units, restart loops, overall system state.

Uses read-only D-Bus method calls (ListUnits, Properties.Get) on the host's system bus,
reached through the read-only host mount (<host_root>/run/dbus/system_bus_socket).
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import Any, Protocol

from ..config import SystemdCollectorConfig
from .base import Collector, CollectorUnavailable, HostEnv

SYSTEMD = "org.freedesktop.systemd1"
MANAGER_PATH = "/org/freedesktop/systemd1"
MANAGER_IFACE = "org.freedesktop.systemd1.Manager"
UNIT_IFACE = "org.freedesktop.systemd1.Unit"
SERVICE_IFACE = "org.freedesktop.systemd1.Service"


class SystemdBus(Protocol):
    async def list_units(self) -> list[list[Any]]: ...
    async def get_property(self, path: str, interface: str, name: str) -> Any: ...
    async def close(self) -> None: ...


class DbusError(Exception):
    pass


class DbusSystemdBus:
    """dbus-fast implementation. Imported lazily: dbus-fast's asyncio module only loads
    on platforms with unix-socket ancillary data (Linux)."""

    def __init__(self, address: str):
        self.address = address
        self._bus: Any = None

    async def _connected(self) -> Any:
        if self._bus is None or not self._bus.connected:
            from dbus_fast.aio import MessageBus

            try:
                self._bus = await MessageBus(bus_address=self.address).connect()
            except (OSError, EOFError) as exc:
                self._bus = None
                raise CollectorUnavailable(
                    f"cannot connect to D-Bus {self.address}: {exc}"
                ) from exc
        return self._bus

    async def _call(self, **kwargs: Any) -> list[Any]:
        from dbus_fast import Message, MessageType

        bus = await self._connected()
        reply = await bus.call(Message(destination=SYSTEMD, **kwargs))
        if reply.message_type == MessageType.ERROR:
            raise DbusError(f"{reply.error_name}: {reply.body}")
        return list(reply.body)

    async def list_units(self) -> list[list[Any]]:
        body = await self._call(path=MANAGER_PATH, interface=MANAGER_IFACE, member="ListUnits")
        return body[0]

    async def get_property(self, path: str, interface: str, name: str) -> Any:
        body = await self._call(
            path=path,
            interface="org.freedesktop.DBus.Properties",
            member="Get",
            signature="ss",
            body=[interface, name],
        )
        value = body[0]
        return getattr(value, "value", value)

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None


class SystemdCollector(Collector):
    name = "systemd"
    timeout = timedelta(seconds=45)
    cfg: SystemdCollectorConfig

    def __init__(self, cfg: SystemdCollectorConfig, env: HostEnv, *, bus: SystemdBus | None = None):
        super().__init__(cfg, env)
        self.bus: SystemdBus = bus or DbusSystemdBus(self._address())
        self._prev_restarts: dict[str, int] = {}

    def _address(self) -> str:
        if self.cfg.bus_address:
            return self.cfg.bus_address
        socket = self.env.path("/run/dbus/system_bus_socket")
        return f"unix:path={socket}"

    async def check(self) -> None:
        if os.name != "posix":
            raise CollectorUnavailable("systemd collector requires Linux")
        try:
            await self.bus.get_property(MANAGER_PATH, MANAGER_IFACE, "SystemState")
        except DbusError as exc:
            raise CollectorUnavailable(f"systemd not reachable over D-Bus: {exc}") from exc

    async def close(self) -> None:
        await self.bus.close()

    async def _prop(self, path: str, iface: str, name: str) -> Any:
        try:
            return await self.bus.get_property(path, iface, name)
        except DbusError:
            return None

    async def collect(self) -> dict[str, Any]:
        try:
            raw_units = await self.bus.list_units()
        except (DbusError, OSError):
            await self.bus.close()  # reconnect next time
            raise
        units = [
            {
                "name": u[0],
                "description": u[1],
                "load": u[2],
                "active": u[3],
                "sub": u[4],
                "path": u[6],
            }
            for u in raw_units
        ]
        by_active: dict[str, int] = {}
        for unit in units:
            by_active[unit["active"]] = by_active.get(unit["active"], 0) + 1

        failed = []
        for unit in (u for u in units if u["active"] == "failed"):
            entry = {"name": unit["name"], "description": unit["description"],
                     "sub": unit["sub"]}  # fmt: skip
            changed = await self._prop(unit["path"], UNIT_IFACE, "StateChangeTimestamp")
            entry["since"] = round(changed / 1e6) if changed else None
            if unit["name"].endswith(".service"):
                entry["result"] = await self._prop(unit["path"], SERVICE_IFACE, "Result")
            failed.append(entry)

        restarting = await self._restarts(units)
        return {
            "system_state": await self._prop(MANAGER_PATH, MANAGER_IFACE, "SystemState"),
            "units_total": len(units),
            "by_active_state": by_active,
            "failed": failed,
            "restarting": restarting,
            "activating": [u["name"] for u in units if u["active"] == "activating"][:20],
        }

    async def _restarts(self, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        services = [
            u
            for u in units
            if u["name"].endswith(".service")
            and u["load"] == "loaded"
            and u["active"] in ("active", "activating", "reloading", "failed")
        ]
        sem = asyncio.Semaphore(16)

        async def nrestarts(unit: dict[str, Any]) -> tuple[str, int | None]:
            async with sem:
                value = await self._prop(unit["path"], SERVICE_IFACE, "NRestarts")
                return unit["name"], int(value) if value is not None else None

        results = await asyncio.gather(*(nrestarts(u) for u in services))
        out: list[dict[str, Any]] = []
        current: dict[str, int] = {}
        for name, count in results:
            if count is None:
                continue
            current[name] = count
            prev = self._prev_restarts.get(name)
            delta = max(count - prev, 0) if prev is not None else 0
            if count > 0:
                out.append({"name": name, "n_restarts": count, "delta": delta})
        self._prev_restarts = current
        out.sort(key=lambda r: (-r["delta"], -r["n_restarts"]))
        return out[:20]
