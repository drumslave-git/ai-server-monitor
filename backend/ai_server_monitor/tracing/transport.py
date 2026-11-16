"""The recording layer: an httpx transport that sits under the LLM client, so every request to
the model is captured no matter which caller made it.

    transport = tracing.wrap_transport(inner_or_None)
    httpx.AsyncClient(transport=transport, ...)

Only `POST …/chat/completions` is recorded. The response stream is passed through untouched
(same chunks, same timing) while a copy is parsed on the side; the row is written when the
response is closed, from a background task, so tracing never delays or breaks a call.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from .parse import (
    STREAM_DROPPED,
    Parsed,
    StreamParser,
    empty_answer_message,
    parse_json_response,
)
from .scope import CallGroup, TraceScope, current_group, current_scope
from .store import CallRecord, estimate_tokens

MAX_BUFFER = 16 * 1024 * 1024
ERROR_TEXT_CHARS = 4000
_ENDPOINT = "/chat/completions"


def public_base_url(url: httpx.URL) -> str:
    """The endpoint's base URL without credentials, query or fragment."""
    clean = url.copy_with(username=None, password=None, query=None, fragment=None)
    text = str(clean)
    return text[: -len(_ENDPOINT)] if text.endswith(_ENDPOINT) else text.rstrip("/")


class PendingCall:
    """One HTTP attempt in flight."""

    def __init__(self, request: httpx.Request, payload: dict[str, Any]):
        self.scope: TraceScope | None = current_scope()
        self.group: CallGroup | None = current_group()
        self.payload = payload
        self.base_url = public_base_url(request.url)
        self.request_bytes = len(request.content) if request.content else 0
        self.stream = bool(payload.get("stream"))
        self.ts = time.time()
        self.t_sent = time.perf_counter()
        if self.group is not None:
            self.group.attempt += 1
            waited_from = self.group.last_end or self.group.started
            self.queued_ms = max(0, round((self.t_sent - waited_from) * 1000))
            self.attempt = self.group.attempt
        else:
            self.queued_ms = 0
            self.attempt = 1
        self.http_status: int | None = None
        self.exc: BaseException | None = None
        self.eof = False
        self.finished = False
        self.t_end: float | None = None
        self.parser = StreamParser() if self.stream else None
        self._buffer = bytearray()
        self._buffer_overflow = False

    def got_headers(self, status: int) -> None:
        self.http_status = status
        if status >= 400:
            self.parser = None  # an error body is JSON even for a streaming request

    def on_chunk(self, chunk: bytes) -> None:
        if self.parser is not None:
            self.parser.feed(chunk)
        elif len(self._buffer) + len(chunk) <= MAX_BUFFER:
            self._buffer += chunk
        else:
            self._buffer_overflow = True

    def on_error(self, exc: BaseException) -> None:
        if isinstance(exc, Exception):
            self.exc = exc

    def finish(self) -> bool:
        """Mark the end; False if it already ended."""
        if self.finished:
            return False
        self.finished = True
        self.t_end = time.perf_counter()
        if self.group is not None:
            self.group.last_end = self.t_end
        return True

    # ------------------------------------------------------------------ result

    def record(self) -> CallRecord:
        payload = self.payload
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        tools = payload.get("tools") if isinstance(payload.get("tools"), list) else None
        params = {k: v for k, v in payload.items() if k not in ("messages", "tools")}
        scope = self.scope
        end = self.t_end or time.perf_counter()
        rec = CallRecord(
            ts=self.ts,
            purpose=scope.purpose if scope else "other",
            status="ok",
            link_kind=scope.link_kind if scope else None,
            link_id=scope.link_id if scope else None,
            link_step=scope.step if scope else None,
            turn=scope.turn if scope else None,
            group_id=self.group.id if self.group else None,
            attempt=self.attempt,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            base_url=self.base_url,
            stream=self.stream,
            params=params,
            http_status=self.http_status,
            queued_ms=self.queued_ms,
            total_ms=round((end - self.t_sent) * 1000),
            request_bytes=self.request_bytes,
            messages=messages,
            tools=tools,
        )
        parsed = self._parsed()
        if self.exc is not None:
            rec.status = "error"
            rec.error = f"{type(self.exc).__name__}: {self.exc}"
        elif self.http_status is not None and self.http_status >= 400:
            rec.status = "error"
            rec.error = self._error_text()
        elif parsed.error:
            rec.status = "error"
            rec.error = parsed.error
        elif self.stream and not (self.eof or (self.parser and self.parser.done)):
            rec.status = "cancelled"
            rec.error = "the client stopped reading before the response was complete"
        if self.http_status is None and self.exc is None:
            rec.status = "error"
            rec.error = rec.error or "no response"
        rec.content = parsed.content
        rec.reasoning = parsed.reasoning
        rec.tool_calls = parsed.tool_calls
        rec.finish_reason = parsed.finish_reason
        if (
            rec.status == "ok"
            and parsed.finish_reason == "length"
            and not parsed.content.strip()
            and not parsed.tool_calls
        ):  # HTTP 200, but the whole limit went on reasoning: that is no answer
            rec.status = "error"
            rec.error = empty_answer_message(params.get("max_tokens"))
        elif (
            rec.status == "ok"
            and self.stream
            and parsed.finish_reason is None
            and not parsed.content.strip()
            and not parsed.tool_calls
        ):  # the server dropped the reply (a crashed model runner does this)
            rec.status = "error"
            rec.error = STREAM_DROPPED
        if parsed.model:
            rec.params = {**rec.params, "response_model": parsed.model}
        if self.parser is not None and self.parser.first_token_at is not None:
            rec.ttft_ms = round((self.parser.first_token_at - self.t_sent) * 1000)
        if parsed.usage:
            rec.prompt_tokens = parsed.usage["prompt_tokens"]
            rec.completion_tokens = parsed.usage["completion_tokens"]
        elif rec.status == "ok":
            rec.usage_estimated = True
            rec.prompt_tokens = estimate_tokens(json.dumps(messages, ensure_ascii=False))
            answer = parsed.content + parsed.reasoning
            answer += "".join(c["arguments"] for c in parsed.tool_calls)
            rec.completion_tokens = estimate_tokens(answer)
        return rec

    def _parsed(self) -> Parsed:
        if self.parser is not None:
            return self.parser.result()
        if self.http_status is not None and self.http_status < 400 and not self._buffer_overflow:
            return parse_json_response(bytes(self._buffer))
        return Parsed()

    def _error_text(self) -> str:
        body = bytes(self._buffer[: ERROR_TEXT_CHARS * 4]).decode("utf-8", "replace").strip()
        head = f"HTTP {self.http_status}"
        return f"{head}: {body[:ERROR_TEXT_CHARS]}" if body else head


class _Tee(httpx.AsyncByteStream):
    def __init__(
        self, inner: httpx.AsyncByteStream, call: PendingCall, done: Callable[[PendingCall], None]
    ):
        self._inner = inner
        self._call = call
        self._done = done

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._inner:
                self._call.on_chunk(chunk)
                yield chunk
        except BaseException as exc:
            self._call.on_error(exc)
            raise
        self._call.eof = True

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        finally:
            if self._call.finish():
                self._done(self._call)


class TracingTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None,
        submit: Callable[[PendingCall], None],
        enabled: Callable[[], bool],
    ):
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._submit = submit
        self._enabled = enabled

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or not request.url.path.endswith(_ENDPOINT):
            return await self._inner.handle_async_request(request)
        payload = await self._payload(request)
        if payload is None or not self._enabled():
            return await self._inner.handle_async_request(request)
        call = PendingCall(request, payload)
        try:
            response = await self._inner.handle_async_request(request)
        except BaseException as exc:
            call.on_error(exc)
            if call.finish():
                self._submit(call)
            raise
        call.got_headers(response.status_code)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_Tee(response.stream, call, self._submit),  # type: ignore[arg-type]
            extensions=response.extensions,
        )

    @staticmethod
    async def _payload(request: httpx.Request) -> dict[str, Any] | None:
        try:
            body = await request.aread()
            data = json.loads(body)
        except (ValueError, httpx.StreamError):
            return None
        return data if isinstance(data, dict) else None

    async def aclose(self) -> None:
        await self._inner.aclose()
