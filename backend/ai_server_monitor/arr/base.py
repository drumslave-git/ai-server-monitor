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


def looks_like_html(response: httpx.Response) -> bool:
    return (
        "html" in response.headers.get("content-type", "").lower()
        or response.text.lstrip()[:1] == "<"
    )


def denied_text(response: httpx.Response, label: str, *, key_sent: bool = True) -> str:
    """What a 401/403 from an *arr app means, from the answer itself: the app refusing the
    API key looks different from a reverse proxy / SSO page in front of it."""
    status = response.status_code
    body = " ".join(response.text.split())[:200] or "(empty)"
    fact = f'HTTP {status}, body "{body}"'
    if looks_like_html(response):
        why = (
            f"the answer is a web page, so a reverse proxy / SSO login in front of {label} "
            "refused the request; the API key was probably never checked. Let the proxy pass "
            "API calls (or use the internal URL, e.g. http://<container>:<port>)"
        )
    elif status == 401:
        why = (
            f"{label} itself rejected the API key"
            + (" we sent in the X-Api-Key header" if key_sent else "")
            + f": copy it again from {label} → Settings → General → API Key"
        )
    else:
        why = (
            f"403 Forbidden: {label}'s authentication, or a proxy/IP allow-list in front of it, "
            "refused this client; the API key is only one possible cause"
        )
    return f"{label} refused access ({fact}): {why}."


class HttpClient:
    """A thin JSON client for one base URL."""

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        label: str = "the service",
    ):
        self.label = label
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
                f"{self.base_url}: " + denied_text(response, self.label),
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
