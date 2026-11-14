"""Shared HTTP plumbing for the *arr and qBittorrent clients.

Secrets (API keys, passwords) only ever travel in request headers/bodies; they are never
put in URLs, exceptions or logs, and error text comes from the server's answer, not from
the request.
"""

from __future__ import annotations

from typing import Any

import httpx

MAX_ERROR_LENGTH = 300


class ArrError(Exception):
    """A request to an *arr app / qBittorrent failed.

    `unreachable` = no HTTP answer at all; `status` = the HTTP status when there was one.
    """

    def __init__(self, message: str, *, unreachable: bool = False, status: int | None = None):
        super().__init__(message)
        self.unreachable = unreachable
        self.status = status


def error_text(response: httpx.Response) -> str:
    """Short, human-readable reason from an error response (JSON message or plain text)."""
    try:
        body = response.json()
    except ValueError:
        body = None
    message: Any = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or body.get("errorMessage")
    elif isinstance(body, list) and body and isinstance(body[0], dict):
        # Validation errors: [{"propertyName": ..., "errorMessage": ...}]
        message = "; ".join(str(b.get("errorMessage") or b) for b in body[:3])
    text = str(message) if message else response.text.strip()
    return text[:MAX_ERROR_LENGTH] or response.reason_phrase


class HttpClient:
    """A thin JSON client for one base URL."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = url.strip().rstrip("/")
        self._http = httpx.AsyncClient(
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, self.base_url + path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ArrError(f"{self.base_url}: timed out", unreachable=True) from exc
        except httpx.HTTPError as exc:
            raise ArrError(
                f"cannot reach {self.base_url}: {type(exc).__name__}", unreachable=True
            ) from exc

    async def request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.send(method, path, **kwargs)
        if response.status_code in (401, 403):
            raise ArrError(
                f"{self.base_url}: access denied (HTTP {response.status_code}); "
                "check the API key / credentials",
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise ArrError(
                f"{self.base_url}: HTTP {response.status_code}: {error_text(response)}",
                status=response.status_code,
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ArrError(f"{self.base_url}: reply is not JSON (is the URL correct?)") from exc
