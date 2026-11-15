"""qBittorrent WebUI API v2 client: cookie login, one transparent re-login on 403.

Login failures are reported as what happened (HTTP status, body, headers sent, what it means)
and paused with a back-off shared across all clients: see `qbit_auth`."""

from __future__ import annotations

from typing import Any

import httpx

from . import qbit_auth
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
        self._url = url.strip().rstrip("/")
        self._key = qbit_auth.backoff_key(url, username, password)
        # qBittorrent rejects state-changing requests whose Referer/Origin is not its own.
        self._http = HttpClient(
            url,
            headers=qbit_auth.request_headers(url),
            timeout=timeout,
            transport=transport,
        )
        self._logged_in = False

    async def close(self) -> None:
        await self._http.close()

    async def _login(self) -> None:
        if paused := qbit_auth.paused_message(self._key):
            raise ArrError(paused, status=401)
        response = await self._http.send(
            "POST",
            "/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
        )
        diagnosis = qbit_auth.diagnose_login(response, self._url)
        if diagnosis.kind == "unverified":  # 2xx, no body, no cookie: does a request work?
            probe = await self._http.send("GET", "/api/v2/app/version")
            if (failure := qbit_auth.after_probe(diagnosis, probe.status_code)) is None:
                qbit_auth.record_success(self._key)
                self._logged_in = True
                return
            diagnosis = failure
        if diagnosis.ok:
            qbit_auth.record_success(self._key)
            self._logged_in = True
            return
        until = qbit_auth.record_failure(self._key, diagnosis)
        status = 403 if diagnosis.kind == "banned" else 401
        raise ArrError(diagnosis.message() + qbit_auth.pause_note(until), status=status)

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
