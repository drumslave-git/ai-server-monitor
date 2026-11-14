"""qBittorrent WebUI API v2 client: cookie login, one transparent re-login on 403."""

from __future__ import annotations

from typing import Any

import httpx

from .base import ArrError, HttpClient, error_text


class QBittorrent:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._username = username
        self._password = password
        # qBittorrent rejects state-changing requests whose Referer is not its own origin.
        self._http = HttpClient(
            url,
            headers={"Referer": url.strip().rstrip("/")},
            timeout=timeout,
            transport=transport,
        )
        self._logged_in = False

    async def close(self) -> None:
        await self._http.close()

    async def _login(self) -> None:
        response = await self._http.send(
            "POST",
            "/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
        )
        if response.status_code == 403:
            raise ArrError(
                "qBittorrent refused the login (IP banned after failed attempts?)", status=403
            )
        if response.status_code != 200 or response.text.strip() != "Ok.":
            raise ArrError("qBittorrent login failed: wrong username or password", status=401)
        self._logged_in = True

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._logged_in:
            await self._login()
        response = await self._http.send(method, path, **kwargs)
        if response.status_code == 403:  # session expired (or never valid): log in once more
            self._logged_in = False
            await self._login()
            response = await self._http.send(method, path, **kwargs)
        if response.status_code >= 400:
            raise ArrError(
                f"qBittorrent: HTTP {response.status_code}: {error_text(response)}",
                status=response.status_code,
            )
        return response

    async def version(self) -> str:
        return (await self._request("GET", "/api/v2/app/version")).text.strip()

    async def transfer_info(self) -> dict[str, Any]:
        return (await self._request("GET", "/api/v2/transfer/info")).json()

    async def torrents(self, hashes: str | None = None) -> list[dict[str, Any]]:
        params = {"hashes": hashes} if hashes else None
        return (await self._request("GET", "/api/v2/torrents/info", params=params)).json()

    # ---- writes (only ever called through the action layer)

    async def delete_torrent(self, torrent_hash: str, *, delete_files: bool) -> None:
        await self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": torrent_hash, "deleteFiles": str(delete_files).lower()},
        )
