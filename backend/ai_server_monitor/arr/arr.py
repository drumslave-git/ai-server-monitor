"""Radarr / Sonarr / Whisparr (API v3) and Prowlarr (API v1) clients."""

from __future__ import annotations

from typing import Any

import httpx

from .base import ArrError, HttpClient, error_text

PAGE_SIZE = 200
MAX_PAGES = 10


class ArrApp:
    """Radarr, Sonarr or Whisparr: same v3 API, `X-Api-Key` header."""

    api_prefix = "/api/v3"

    def __init__(
        self,
        kind: str,
        name: str,
        url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.kind = kind
        self.name = name
        self._http = HttpClient(
            url, headers={"X-Api-Key": api_key}, timeout=timeout, transport=transport
        )

    async def close(self) -> None:
        await self._http.close()

    async def _get(self, path: str, **params: Any) -> Any:
        return await self._http.request_json("GET", self.api_prefix + path, params=params or None)

    # ---- reads

    async def status(self) -> dict[str, Any]:
        return await self._get("/system/status")

    async def health(self) -> list[dict[str, Any]]:
        return await self._get("/health")

    async def queue(self, *, max_items: int = 1000) -> tuple[list[dict[str, Any]], int]:
        """(items, total). Items are capped at `max_items`; total is the real queue size."""
        items: list[dict[str, Any]] = []
        total = 0
        for page in range(1, MAX_PAGES + 1):
            data = await self._get(
                "/queue", page=page, pageSize=PAGE_SIZE, includeUnknownMovieItems="true"
            )
            records = data.get("records") or []
            total = int(data.get("totalRecords") or 0)
            items.extend(records)
            if len(items) >= min(total, max_items) or not records:
                break
        return items[:max_items], total

    async def wanted_total(self, kind: str) -> int:
        """Total missing (kind='missing') or cutoff-unmet (kind='cutoff') items."""
        data = await self._get(f"/wanted/{kind}", page=1, pageSize=1, monitored="true")
        return int(data.get("totalRecords") or 0)

    # ---- writes (only ever called through the action layer)

    async def remove_queue_item(
        self, queue_id: int, *, remove_from_client: bool, blocklist: bool, skip_redownload: bool
    ) -> None:
        await self._http.request_json(
            "DELETE",
            f"{self.api_prefix}/queue/{queue_id}",
            params={
                "removeFromClient": str(remove_from_client).lower(),
                "blocklist": str(blocklist).lower(),
                "skipRedownload": str(skip_redownload).lower(),
            },
        )

    async def command(self, name: str, **body: Any) -> dict[str, Any]:
        result = await self._http.request_json(
            "POST", f"{self.api_prefix}/command", json={"name": name, **body}
        )
        return result if isinstance(result, dict) else {}


# Search command and id field per app: Radarr works on movies, Sonarr/Whisparr on episodes.
SEARCH_COMMANDS: dict[str, tuple[str, str]] = {
    "radarr": ("MoviesSearch", "movieIds"),
    "sonarr": ("EpisodeSearch", "episodeIds"),
    "whisparr": ("EpisodeSearch", "episodeIds"),
}


class Prowlarr:
    api_prefix = "/api/v1"

    def __init__(
        self,
        name: str,
        url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.name = name
        self._http = HttpClient(
            url, headers={"X-Api-Key": api_key}, timeout=timeout, transport=transport
        )

    async def close(self) -> None:
        await self._http.close()

    async def _get(self, path: str) -> Any:
        return await self._http.request_json("GET", self.api_prefix + path)

    async def status(self) -> dict[str, Any]:
        return await self._get("/system/status")

    async def health(self) -> list[dict[str, Any]]:
        return await self._get("/health")

    async def indexers(self) -> list[dict[str, Any]]:
        return await self._get("/indexer")

    async def indexer_status(self) -> list[dict[str, Any]]:
        return await self._get("/indexerstatus")

    async def test_indexer(self, indexer_id: int) -> tuple[bool, list[str]]:
        """Run Prowlarr's own indexer test. Returns (ok, failure messages)."""
        indexer = await self._get(f"/indexer/{indexer_id}")
        response = await self._http.send("POST", f"{self.api_prefix}/indexer/test", json=indexer)
        if response.status_code < 300:
            return True, []
        if response.status_code in (401, 403):
            raise ArrError(
                f"access denied (HTTP {response.status_code})", status=response.status_code
            )
        return False, [error_text(response)]
