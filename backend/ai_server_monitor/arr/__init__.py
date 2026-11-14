"""Clients for the *arr apps, Prowlarr and qBittorrent."""

from __future__ import annotations

import httpx

from ..config import ArrSettings
from .arr import SEARCH_COMMANDS, ArrApp, Prowlarr
from .base import ArrError
from .qbit import QBittorrent

ARR_KINDS = ("radarr", "sonarr", "whisparr")


class Clients:
    """All configured clients for one settings snapshot."""

    def __init__(
        self, cfg: ArrSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        timeout = cfg.timeout.total_seconds()
        self.apps: dict[tuple[str, str], ArrApp] = {
            (kind, inst.name): ArrApp(
                kind,
                inst.name,
                inst.url,
                inst.api_key.get_secret_value(),
                timeout=timeout,
                transport=transport,
            )
            for kind in ARR_KINDS
            for inst in getattr(cfg, kind)
        }
        self.prowlarr = (
            Prowlarr(
                cfg.prowlarr.name,
                cfg.prowlarr.url,
                cfg.prowlarr.api_key.get_secret_value(),
                timeout=timeout,
                transport=transport,
            )
            if cfg.prowlarr
            else None
        )
        self.qbit = (
            QBittorrent(
                cfg.qbittorrent.url,
                cfg.qbittorrent.username,
                cfg.qbittorrent.password.get_secret_value(),
                timeout=timeout,
                transport=transport,
            )
            if cfg.qbittorrent
            else None
        )

    @property
    def empty(self) -> bool:
        return not (self.apps or self.prowlarr or self.qbit)

    async def close(self) -> None:
        for client in (*self.apps.values(), self.prowlarr, self.qbit):
            if client is not None:
                await client.close()


__all__ = [
    "ARR_KINDS",
    "SEARCH_COMMANDS",
    "ArrApp",
    "ArrError",
    "Clients",
    "Prowlarr",
    "QBittorrent",
]
