"""`ApiGateway`: the one place that turns (service, method, path, query, body) into an HTTP
call, applies the safety rules and shapes the answer.

- GET (except the indexer-querying ones) runs directly, rate limited per service.
- Everything else is refused here unless `approved=True`, which only the action layer sets
  after the owner approved the request; that is what keeps writes out of the model's hands.
- Credentials and the base URL come from the settings; every call is logged (service, method,
  path, status, bytes, duration) through `on_call` and kept in `recent`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import struct
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from .errors import ApiAccessError
from .redact import Redactor
from .shaping import DEFAULT_LIMIT, MAX_RESULT_CHARS, paged_meta, shape
from .targets import Prepared, Target, build_targets, prepare

log = logging.getLogger(__name__)

# TODO setting (Settings → *arr & downloads → API access); module constants until then.
GET_TIMEOUT_S = 20.0
TOTAL_TIMEOUT_S = 40.0
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
GET_RATE_LIMIT = 60
GET_RATE_WINDOW_S = 60.0
MAX_PAGES = 10
ALL_PAGES_SIZE = 100
RECENT_CALLS = 200
MAX_ERROR_LENGTH = 300

AuditHook = Callable[[dict[str, Any]], Any]
"""Called with one record per call: ts, service, method, path, status, bytes, ms, source."""


@dataclass
class ApiRequest:
    service: str
    path: str
    method: str = "GET"
    query: dict[str, Any] | None = None
    body: Any = None
    select: str | None = None
    limit: int = DEFAULT_LIMIT
    all_pages: bool = False


@dataclass
class _Raw:
    status: int
    data: Any
    size: int
    ms: int
    note: str | None = None


class ApiGateway:
    def __init__(
        self,
        settings: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        on_call: AuditHook | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.targets: dict[str, Target] = build_targets(settings)
        self.redactor = Redactor.from_settings(settings)
        self.on_call = on_call
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_CALLS)
        self._transport = transport
        self._clock = clock
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._logged_in: set[str] = set()
        self._login_lock = asyncio.Lock()
        self._get_times: dict[str, deque[float]] = {}

    # ------------------------------------------------------------------ services

    @property
    def services(self) -> dict[str, str]:
        """name -> label (Radarr, qBittorrent, ...)."""
        return {name: t.label for name, t in self.targets.items()}

    def target(self, service: str) -> Target:
        target = self.targets.get(service)
        if target is None:
            names = ", ".join(self.targets) or "none configured"
            raise ApiAccessError(f"unknown service {service!r}; available: {names}")
        return target

    def prepare(self, service: str, method: str, path: str, query: Any = None) -> Prepared:
        """Validate a request; `Prepared.write_like` says whether it needs approval."""
        return prepare(self.target(service), method, path, query)

    async def close(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------------ the call

    async def execute(
        self,
        request: ApiRequest,
        *,
        approved: bool = False,
        source: str = "chat",
        max_chars: int = MAX_RESULT_CHARS,
    ) -> dict[str, Any]:
        """Run a request and return the shaped answer. Raises ApiAccessError.

        Writes and indexer-querying GETs (`Prepared.write_like`) only run with
        `approved=True`."""
        prepared = self.prepare(request.service, request.method, request.path, request.query)
        if prepared.write_like and not approved:
            raise ApiAccessError(
                f"{prepared.display} changes something or queries every indexer, so it needs the "
                "owner's approval: it is requested, not run directly"
            )
        raw = await self._run(prepared, request, source)
        keep_tail = bool(re.search(r"/containers/[^/]+/logs$", prepared.path))
        shaped = shape(
            raw.data,
            select=request.select,
            limit=request.limit,
            max_chars=max_chars,
            keep_tail=keep_tail,
        )
        out: dict[str, Any] = {
            "service": request.service,
            "call": prepared.display,
            "status": raw.status,
        }
        if shaped.total is not None:
            out["total"] = shaped.total
            out["showing"] = shaped.showing
        if shaped.paged:
            out["paged"] = shaped.paged
        notes = [*shaped.notes, *([raw.note] if raw.note else [])]
        if notes:
            out["note"] = "; ".join(notes)
        out["data"] = shaped.data
        return out

    async def _run(self, prepared: Prepared, request: ApiRequest, source: str) -> _Raw:
        paging = request.all_pages and prepared.method == "GET"
        query = list(prepared.query)
        if paging and not any(k == "pageSize" for k, _ in query):
            query.append(("pageSize", str(ALL_PAGES_SIZE)))
        first = await self._send(prepared, query, request.body, source)
        data = first.data
        if not paging or paged_meta(data) is None:
            return first
        records = list(data["records"])
        total = int(data.get("totalRecords") or 0)
        size = int(data.get("pageSize") or len(records) or 1)
        page = int(data.get("page") or 1)
        pages = 1
        while len(records) < total and pages < MAX_PAGES:
            page += 1
            pages += 1
            nxt_query = [(k, v) for k, v in query if k != "page"] + [("page", str(page))]
            more = await self._send(prepared, nxt_query, None, source)
            batch = more.data.get("records") if isinstance(more.data, dict) else None
            if not batch:
                break
            records.extend(batch)
            first.size += more.size
            first.ms += more.ms
        merged = {**data, "records": records, "page": 1, "pageSize": size}
        first.data = merged
        if len(records) < total:
            first.note = f"stopped after {pages} pages ({len(records)} of {total} records)"
        return first

    # ------------------------------------------------------------------ HTTP

    def _client(self, target: Target) -> httpx.AsyncClient:
        client = self._clients.get(target.name)
        if client is None:
            transport = self._transport
            if transport is None and target.uds:
                transport = httpx.AsyncHTTPTransport(uds=target.uds)
            client = httpx.AsyncClient(
                headers=target.headers,
                auth=target.basic_auth,
                transport=transport,
                timeout=httpx.Timeout(GET_TIMEOUT_S, connect=8.0),
                follow_redirects=False,
            )
            self._clients[target.name] = client
        return client

    def _throttle(self, target: Target) -> None:
        now = self._clock()
        times = self._get_times.setdefault(target.name, deque())
        while times and now - times[0] > GET_RATE_WINDOW_S:
            times.popleft()
        if len(times) >= GET_RATE_LIMIT:
            wait = max(int(GET_RATE_WINDOW_S - (now - times[0])) + 1, 1)
            raise ApiAccessError(
                f"rate limit: {GET_RATE_LIMIT} calls per minute to {target.name} reached; "
                f"wait {wait}s, and narrow the calls (select, query) instead of repeating them"
            )
        times.append(now)

    async def _send(
        self, prepared: Prepared, query: list[tuple[str, str]], body: Any, source: str
    ) -> _Raw:
        target = prepared.target
        if prepared.method == "GET":
            self._throttle(target)
        started = time.monotonic()
        status: int | None = None
        size = 0
        error: str | None = None
        try:
            async with asyncio.timeout(TOTAL_TIMEOUT_S):
                status, content_type, payload = await self._request(prepared, query, body)
            size = len(payload)
            if status >= 400:
                error = _error_message(self, target, status, payload, prepared)
                raise ApiAccessError(error, status=status)
            data = self._decode(target, prepared, content_type, payload)
            return _Raw(status, data, size, int((time.monotonic() - started) * 1000))
        except TimeoutError:
            error = f"{target.name} did not answer within {TOTAL_TIMEOUT_S:.0f}s"
            raise ApiAccessError(error) from None
        except httpx.TimeoutException:
            error = f"{target.name} timed out after {GET_TIMEOUT_S:.0f}s"
            raise ApiAccessError(error) from None
        except httpx.HTTPError as exc:
            error = f"cannot reach {target.name} ({type(exc).__name__})"
            raise ApiAccessError(error) from None
        finally:
            await self._audit(
                {
                    "ts": time.time(),
                    "service": target.name,
                    "method": prepared.method,
                    "path": prepared.path,
                    "query": sorted({k for k, _ in query}),
                    "status": status,
                    "bytes": size,
                    "ms": int((time.monotonic() - started) * 1000),
                    "source": source,
                    "error": error,
                }
            )

    async def _request(
        self, prepared: Prepared, query: list[tuple[str, str]], body: Any
    ) -> tuple[int, str, bytes]:
        target = prepared.target
        if target.login is not None:
            await self._qbit_login(target)
        result = await self._once(prepared, query, body)
        if target.login is not None and result[0] == 403:  # session expired: log in again
            self._logged_in.discard(target.name)
            await self._qbit_login(target)
            result = await self._once(prepared, query, body)
        return result

    async def _once(
        self, prepared: Prepared, query: list[tuple[str, str]], body: Any
    ) -> tuple[int, str, bytes]:
        target = prepared.target
        kwargs: dict[str, Any] = {"params": query or None}
        if body is not None and prepared.method != "GET":
            if target.kind == "qbittorrent" and isinstance(body, dict):
                kwargs["data"] = {k: _form_value(v) for k, v in body.items()}
            else:
                kwargs["json"] = body
        client = self._client(target)
        chunks: list[bytes] = []
        size = 0
        async with client.stream(
            prepared.method, target.base_url + prepared.path, **kwargs
        ) as response:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ApiAccessError(
                        f"the answer is larger than {MAX_RESPONSE_BYTES // 1_000_000} MB: "
                        "narrow the call with query parameters"
                    )
                chunks.append(chunk)
            return response.status_code, response.headers.get("content-type", ""), b"".join(chunks)

    async def _qbit_login(self, target: Target) -> None:
        if target.name in self._logged_in:
            return
        assert target.login is not None
        async with self._login_lock:
            if target.name in self._logged_in:
                return
            client = self._client(target)
            response = await client.post(
                target.base_url + "/api/v2/auth/login",
                data={"username": target.login[0], "password": target.login[1]},
            )
            if response.status_code == 403:
                raise ApiAccessError("qBittorrent refused the login (IP banned?)", status=403)
            if response.status_code != 200 or response.text.strip() != "Ok.":
                raise ApiAccessError(
                    "qBittorrent login failed: check the user name and password in Settings",
                    status=401,
                )
            self._logged_in.add(target.name)

    # ------------------------------------------------------------------ answers

    def _decode(self, target: Target, prepared: Prepared, content_type: str, payload: bytes) -> Any:
        if not payload:
            return {"result": "empty answer (success)"}
        text: str
        if target.kind == "docker" and prepared.path.endswith("/logs"):
            text = _docker_log_text(payload)
            return self.redactor.clean(text.splitlines())
        lowered = content_type.lower()
        if "json" in lowered or payload[:1] in (b"{", b"["):
            try:
                return self.redactor.clean(json.loads(payload))
            except ValueError:
                pass
        if "octet-stream" in lowered or "tar" in lowered or b"\x00" in payload[:1024]:
            raise ApiAccessError(f"binary answer ({len(payload)} bytes) cannot be shown")
        text = payload.decode("utf-8", errors="replace")
        return self.redactor.text(text.strip())

    async def _audit(self, record: dict[str, Any]) -> None:
        self.recent.append(record)
        log.info(
            "api call %s %s %s -> %s %sB %sms (%s)",
            record["service"],
            record["method"],
            record["path"],
            record["status"] if record["status"] is not None else "no answer",
            record["bytes"],
            record["ms"],
            record["source"],
        )
        if self.on_call is None:
            return
        try:
            outcome = self.on_call(record)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            log.exception("api access audit hook failed")


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return "|".join(_form_value(v) for v in value)
    return str(value)


def _error_message(
    gateway: ApiGateway, target: Target, status: int, payload: bytes, prepared: Prepared
) -> str:
    text = payload.decode("utf-8", errors="replace").strip()
    message = text
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("error") or body.get("errorMessage") or text)
    elif isinstance(body, list) and body and isinstance(body[0], dict):
        message = "; ".join(str(b.get("errorMessage") or b.get("message") or b) for b in body[:3])
    message = gateway.redactor.text(" ".join(message.split()))[:MAX_ERROR_LENGTH]
    if status in (401, 403):
        extra = (
            "denied by the Docker socket proxy (it allows only some GET endpoints)"
            if target.kind == "docker"
            else "access denied: the API key or credentials in Settings may be wrong"
        )
        return f"HTTP {status}: {extra}" + (f" ({message})" if message else "")
    hint = (
        "; no such endpoint or id: check the path with api_endpoints"
        if status in (404, 405)
        else ""
    )
    return f"HTTP {status}: {message or 'error'}{hint}"


def _docker_log_text(payload: bytes) -> str:
    """Docker logs of a container without TTY are framed: 8-byte header (stream, 0, 0, 0,
    big-endian length) before every chunk. Falls back to plain text."""
    out: list[str] = []
    pos = 0
    while pos + 8 <= len(payload):
        stream, size = payload[pos], struct.unpack(">I", payload[pos + 4 : pos + 8])[0]
        if stream not in (0, 1, 2) or payload[pos + 1 : pos + 4] != b"\x00\x00\x00":
            return payload.decode("utf-8", errors="replace")
        out.append(payload[pos + 8 : pos + 8 + size].decode("utf-8", errors="replace"))
        pos += 8 + size
    return (
        "".join(out) if out and pos == len(payload) else payload.decode("utf-8", errors="replace")
    )
