"""Radarr / Sonarr / Whisparr (API v3) and Prowlarr (API v1) clients."""

from __future__ import annotations

from typing import Any

import httpx

from .base import ArrError, HttpClient, denied_text, error_text

PAGE_SIZE = 200
MAX_PAGES = 10

# Radarr and Whisparr v3 (Radarr-based) work on movies; Sonarr on series/episodes.
MOVIE_LIKE = ("radarr", "whisparr")
# Parameters differ per app (checked against the apps' openapi.json / source).
UNKNOWN_ITEMS_PARAM = {
    "radarr": "includeUnknownMovieItems",
    "whisparr": "includeUnknownMovieItems",
    "sonarr": "includeUnknownSeriesItems",
}
LAST_SEARCH_SORT_KEY = {
    "radarr": "movies.lastSearchTime",
    "whisparr": "movies.lastSearchTime",
    "sonarr": "episodes.lastSearchTime",
}
LOOKUP_PATH = {"radarr": "/movie/lookup", "whisparr": "/movie/lookup", "sonarr": "/series/lookup"}
ADD_PATH = {"radarr": "/movie", "whisparr": "/movie", "sonarr": "/series"}


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
            url,
            headers={"X-Api-Key": api_key},
            timeout=timeout,
            transport=transport,
            label=kind.capitalize(),
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
                "/queue",
                page=page,
                pageSize=PAGE_SIZE,
                **{UNKNOWN_ITEMS_PARAM[self.kind]: "true"},
            )
            records = data.get("records") or []
            total = int(data.get("totalRecords") or 0)
            items.extend(records)
            if len(items) >= min(total, max_items) or not records:
                break
        return items[:max_items], total

    async def wanted(
        self, kind: str, *, page: int = 1, page_size: int = 1, least_recently_searched: bool = False
    ) -> tuple[list[dict[str, Any]], int | None]:
        """One page of missing (kind='missing') or cutoff-unmet (kind='cutoff') items and
        the total. Radarr/Whisparr return movies, Sonarr episodes (with their series).
        `total` is None if this app does not offer the endpoint."""
        params: dict[str, Any] = {"page": page, "pageSize": page_size, "monitored": "true"}
        if least_recently_searched:
            params.update(sortKey=LAST_SEARCH_SORT_KEY[self.kind], sortDirection="ascending")
        if self.kind == "sonarr":
            params["includeSeries"] = "true"
        try:
            data = await self._get(f"/wanted/{kind}", **params)
        except ArrError as exc:
            if exc.status == 404:
                return [], None
            raise
        return data.get("records") or [], int(data.get("totalRecords") or 0)

    async def wanted_total(self, kind: str) -> int | None:
        return (await self.wanted(kind))[1]

    # ---- add media (lookup is read-only)

    async def lookup(self, term: str) -> list[dict[str, Any]]:
        """Search for a movie/series to add. `term` may be 'tmdb:123' / 'tvdb:456'."""
        return await self._get(LOOKUP_PATH[self.kind], term=term)

    async def quality_profiles(self) -> list[dict[str, Any]]:
        return await self._get("/qualityprofile")

    async def root_folders(self) -> list[dict[str, Any]]:
        return await self._get("/rootfolder")

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

    async def add(
        self,
        candidate: dict[str, Any],
        *,
        quality_profile_id: int,
        root_folder_path: str,
        search: bool,
    ) -> dict[str, Any]:
        """Add a lookup result to the library (monitored; optionally search right away)."""
        body = {
            **candidate,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": True,
        }
        if self.kind == "sonarr":
            body["seasonFolder"] = True
            body["addOptions"] = {"monitor": "all", "searchForMissingEpisodes": search}
        else:
            body.setdefault("minimumAvailability", "released")
            body["addOptions"] = {"monitor": "movieOnly", "searchForMovie": search}
        result = await self._http.request_json(
            "POST", f"{self.api_prefix}{ADD_PATH[self.kind]}", json=body
        )
        return result if isinstance(result, dict) else {}

    async def command(self, name: str, **body: Any) -> dict[str, Any]:
        result = await self._http.request_json(
            "POST", f"{self.api_prefix}/command", json={"name": name, **body}
        )
        return result if isinstance(result, dict) else {}


# Search command and id field per app (from each app's *SearchCommand source): Radarr and
# Whisparr v3 search movies, Sonarr searches episodes.
SEARCH_COMMANDS: dict[str, tuple[str, str]] = {
    "radarr": ("MoviesSearch", "movieIds"),
    "sonarr": ("EpisodeSearch", "episodeIds"),
    "whisparr": ("MoviesSearch", "movieIds"),
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
            url,
            headers={"X-Api-Key": api_key},
            timeout=timeout,
            transport=transport,
            label="Prowlarr",
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
            raise ArrError(denied_text(response, "Prowlarr"), status=response.status_code)
        return False, [error_text(response)]
