"""Small async client for OpenAI-compatible chat completion APIs.

Deliberately not the OpenAI SDK: local servers (Ollama, llama.cpp, vLLM, LM Studio…)
differ in small ways (missing usage, reasoning fields, odd tool-call shapes, error
payloads mid-stream) and this keeps those quirks in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import LLMConfig
from .text import estimate_tokens

log = logging.getLogger(__name__)

Message = dict[str, Any]

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(Exception):
    """Base error for LLM calls."""


class LLMConnectionError(LLMError):
    """Network failure or timeout: the endpoint could not be reached."""


class LLMStatusError(LLMError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = False

    @classmethod
    def from_api(cls, data: Any) -> Usage | None:
        if not isinstance(data, dict):
            return None
        return cls(
            prompt_tokens=int(data.get("prompt_tokens") or 0),
            completion_tokens=int(data.get("completion_tokens") or 0),
        )


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str
    """Raw JSON string, as sent by the model (may be invalid)."""

    def parsed_arguments(self) -> dict[str, Any]:
        if not self.arguments.strip():
            return {}
        value = json.loads(self.arguments)
        if not isinstance(value, dict):
            raise ValueError("tool arguments must be a JSON object")
        return value


@dataclass
class Completion:
    content: str
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: Usage
    model: str | None
    latency_s: float


@dataclass
class StreamChunk:
    text: str = ""
    done: bool = False
    usage: Usage | None = None
    finish_reason: str | None = None
    latency_s: float | None = None


@dataclass
class LLMStats:
    requests: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_latency_s: float = 0.0
    last_latency_s: float | None = None
    last_error: str | None = None
    last_success: float | None = None
    by_error: dict[str, int] = field(default_factory=dict)

    def record_success(self, usage: Usage, latency_s: float) -> None:
        self.requests += 1
        self.prompt_tokens += usage.prompt_tokens
        self.completion_tokens += usage.completion_tokens
        self.total_latency_s += latency_s
        self.last_latency_s = latency_s
        self.last_success = time.time()

    def record_error(self, exc: Exception) -> None:
        self.requests += 1
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        key = type(exc).__name__
        self.by_error[key] = self.by_error.get(key, 0) + 1


def _messages_text(messages: Sequence[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return "\n".join(parts)


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if not isinstance(raw, list):
        return calls
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        name = fn.get("name") or item.get("name")
        if not name:
            continue
        args = fn.get("arguments", item.get("arguments", ""))
        if not isinstance(args, str):  # some servers send an object instead of a string
            args = json.dumps(args)
        calls.append(ToolCall(id=item.get("id") or f"call_{index}", name=name, arguments=args))
    return calls


class LLMClient:
    def __init__(self, cfg: LLMConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.stats = LLMStats()
        headers = {"Content-Type": "application/json"}
        api_key = cfg.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(
            base_url=cfg.base_url.rstrip("/") + "/",
            headers=headers,
            timeout=httpx.Timeout(cfg.timeout.total_seconds(), connect=10.0),
            transport=transport,
        )
        self._stream_usage_supported = cfg.stream_usage

    @property
    def model(self) -> str:
        return self.cfg.model

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ requests

    def _payload(self, messages: Sequence[Message], **kwargs: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            **self.cfg.extra_body,
            "model": self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_output_tokens,
        }
        payload.update({k: v for k, v in kwargs.items() if v is not None})
        return payload

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(2**attempt, 10))

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                response = await self._http.post("chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = LLMConnectionError(f"{type(exc).__name__}: {exc}")
            else:
                if response.status_code < 400:
                    try:
                        data = response.json()
                    except ValueError as exc:
                        raise LLMError(f"invalid JSON response: {response.text[:200]}") from exc
                    if isinstance(data, dict) and data.get("error") and not data.get("choices"):
                        raise LLMStatusError(response.status_code, json.dumps(data["error"]))
                    return data
                last_exc = LLMStatusError(response.status_code, response.text)
                if response.status_code not in _RETRY_STATUS:
                    raise last_exc
            if attempt < self.cfg.max_retries:
                log.warning("LLM request failed (%s), retrying", last_exc)
                await self._backoff(attempt)
        assert last_exc is not None
        raise last_exc

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Completion:
        payload = self._payload(
            messages,
            tools=tools or None,
            tool_choice=tool_choice if tools else None,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stream=False,
        )
        started = time.perf_counter()
        try:
            data = await self._post(payload)
            choices = data.get("choices") or []
            if not choices:
                raise LLMError(f"response has no choices: {json.dumps(data)[:200]}")
            choice = choices[0]
            message = choice.get("message") or {}
            content = message.get("content") or ""
            if not isinstance(content, str):
                content = json.dumps(content)
            usage = Usage.from_api(data.get("usage")) or Usage(
                prompt_tokens=estimate_tokens(_messages_text(messages)),
                completion_tokens=estimate_tokens(content),
                estimated=True,
            )
        except LLMError as exc:
            self.stats.record_error(exc)
            raise
        latency = time.perf_counter() - started
        self.stats.record_success(usage, latency)
        return Completion(
            content=content,
            tool_calls=_parse_tool_calls(message.get("tool_calls")),
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            model=data.get("model"),
            latency_s=latency,
        )

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a completion. Yields text chunks, then one final chunk with done=True.

        Retries only happen before any text has been yielded.
        """
        started = time.perf_counter()
        text_parts: list[str] = []
        try:
            async for chunk in self._stream_with_retries(messages, temperature, max_tokens):
                if chunk.done:
                    chunk.latency_s = time.perf_counter() - started
                    if chunk.usage is None:
                        chunk.usage = Usage(
                            prompt_tokens=estimate_tokens(_messages_text(messages)),
                            completion_tokens=estimate_tokens("".join(text_parts)),
                            estimated=True,
                        )
                    self.stats.record_success(chunk.usage, chunk.latency_s)
                else:
                    text_parts.append(chunk.text)
                yield chunk
        except LLMError as exc:
            self.stats.record_error(exc)
            raise

    async def _stream_with_retries(
        self, messages: Sequence[Message], temperature: float | None, max_tokens: int | None
    ) -> AsyncIterator[StreamChunk]:
        attempt = 0
        while True:
            payload = self._payload(
                messages, temperature=temperature, max_tokens=max_tokens, stream=True
            )
            if self._stream_usage_supported:
                payload["stream_options"] = {"include_usage": True}
            yielded = False
            try:
                async for chunk in self._stream_once(payload):
                    yielded = True
                    yield chunk
                return
            except LLMStatusError as exc:
                if exc.status_code in (400, 422) and "stream_options" in payload and not yielded:
                    log.info("endpoint rejected stream_options; disabling usage in streams")
                    self._stream_usage_supported = False
                    continue
                if yielded or exc.status_code not in _RETRY_STATUS:
                    raise
                last_exc: LLMError = exc
            except LLMConnectionError as exc:
                if yielded:
                    raise
                last_exc = exc
            if attempt >= self.cfg.max_retries:
                raise last_exc
            log.warning("LLM stream failed (%s), retrying", last_exc)
            await self._backoff(attempt)
            attempt += 1

    async def _stream_once(self, payload: dict[str, Any]) -> AsyncIterator[StreamChunk]:
        usage: Usage | None = None
        finish_reason: str | None = None
        try:
            async with self._http.stream("POST", "chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise LLMStatusError(response.status_code, body)
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except ValueError:
                        log.debug("skipping undecodable stream line: %.200s", data_str)
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("error"):
                        raise LLMStatusError(response.status_code, json.dumps(data["error"]))
                    usage = Usage.from_api(data.get("usage")) or usage
                    for choice in data.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        # reasoning_content / reasoning deltas are intentionally ignored
                        if isinstance(content, str) and content:
                            yield StreamChunk(text=content)
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMConnectionError(f"{type(exc).__name__}: {exc}") from exc
        yield StreamChunk(done=True, usage=usage, finish_reason=finish_reason)

    async def list_models(self) -> list[str]:
        """GET /models — used as a cheap reachability check."""
        try:
            response = await self._http.get("models")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMConnectionError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise LLMStatusError(response.status_code, response.text)
        data = response.json()
        return [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"
