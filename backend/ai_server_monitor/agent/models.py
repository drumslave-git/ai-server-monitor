"""The two model profiles as Pydantic AI models: `main` (Settings → Main model, the stored
`llm` section) and `worker` (Settings → Worker model; falls back to the main model).

Every request goes through our own httpx client whose transport is the tracing transport, so
each call lands in `llm_calls` (bodies, tokens, timing) whatever agent made it.

Retries are the OpenAI SDK's own (`max_retries` of the profile, the SDK's waits between them).
Its retry loop ends when the response headers are in, so `WholeBody` reads the whole answer
before handing the response up: a streamed answer that breaks off is then a failed request the
SDK repeats like any other. Only the chat's own answer is passed on as it arrives (`live`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from pydantic_ai import NativeOutput
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai_compatible import OpenAICompatibleProvider
from pydantic_ai.settings import ModelSettings

from ..config import LLMSettings, Settings
from ..tracing.scope import trace_call

log = logging.getLogger(__name__)

Profile = Literal["main", "worker"]
NO_KEY = "not-needed"
"""The OpenAI SDK insists on a key; local servers ignore it."""


def worker_config(settings: Settings) -> LLMSettings:
    """The worker model's effective settings: its own when a model is set (endpoint and key
    taken from the main model when its base URL is empty), else the main model's."""
    worker = settings.worker
    if not worker.model.strip():
        return settings.llm
    data = worker.model_dump()
    if not worker.base_url.strip():
        data["base_url"] = settings.llm.base_url
        data["api_key"] = settings.llm.api_key
    return LLMSettings.model_validate(data)


def _is_ollama(base_url: str) -> bool:
    parts = urlsplit(base_url)
    return parts.port == 11434 or "ollama" in (parts.hostname or "")


def model_profile(cfg: LLMSettings) -> ModelProfile:
    """Local OpenAI-compatible servers: `max_tokens` (not `max_completion_tokens`), no strict
    tool schemas; Ollama's own profile (reasoning field, schema quirks) when it is Ollama.
    `supports_json_schema_output` lets `NativeOutput` through (Pydantic AI refuses it on a
    profile without the flag; Ollama's profile already has it). It changes nothing else: an
    agent without `NativeOutput` still gets its result through the output tool."""
    base = OllamaProvider.model_profile(cfg.model) if _is_ollama(cfg.base_url) else None
    local = OpenAIModelProfile(
        openai_chat_supports_max_completion_tokens=False,
        openai_supports_strict_tool_definition=False,
        supports_json_schema_output=True,
    )
    return merge_profile(base, local)


class HubModel(OpenAIChatModel):
    """A model of the hub, knowing its profile's Settings "Structured output".

    Each request is one call group of the trace: the SDK's retries of it are logged as
    attempt 2, 3, ... of the same call."""

    structured_output: str = "tool_call"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        with trace_call():
            return await super().request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncIterator[StreamedResponse]:
        async with AsyncExitStack() as stack:
            # The HTTP request, with its retries, is made when the stream is opened; the group
            # is set only around that, never while the caller holds the stream.
            with trace_call():
                stream = await stack.enter_async_context(
                    super().request_stream(
                        messages, model_settings, model_request_parameters, run_context
                    )
                )
            yield stream


def helper_output(model: Any, output_type: Any) -> Any:
    """The `output_type` of a helper call (domain classification, command marks, tool sorting)
    on `model`: `NativeOutput` (the request carries `response_format` with the JSON schema, so
    the model cannot answer in prose) when the model's "Structured output" is json_schema, else
    `output_type` itself (the `final_result` tool). Models not built by the hub: the tool."""
    if getattr(model, "structured_output", None) == "json_schema":
        return NativeOutput(output_type)
    return output_type


def model_settings(cfg: LLMSettings) -> ModelSettings:
    out: dict[str, Any] = {"max_tokens": cfg.max_output_tokens}
    for name in ("temperature", "top_p", "top_k"):
        value = getattr(cfg, name)
        if value != "auto":
            out[name] = value
    if cfg.reasoning_effort != "auto":
        # Sent as the API's `reasoning_effort`. (Pydantic AI's generic `thinking` setting sends
        # nothing for OpenAI-compatible or Ollama profiles: measured.)
        out["openai_reasoning_effort"] = cfg.reasoning_effort
    if cfg.extra_body:
        out["extra_body"] = dict(cfg.extra_body)
    return OpenAIChatModelSettings(**out)  # type: ignore[typeddict-item]


_live: ContextVar[bool] = ContextVar("asm_live_answer", default=False)


@contextmanager
def live_answer(live: bool = True) -> Iterator[None]:
    """Streamed model requests made inside pass their answer on as it arrives (`live`, the
    chat shows it word by word), or only once it is complete (not `live`, see `WholeBody`)."""
    token = _live.set(live)
    try:
        yield
    finally:
        _live.reset(token)


class WholeBody(httpx.AsyncBaseTransport):
    """Reads the whole response body before returning the response, so an answer whose body
    breaks off fails inside the OpenAI SDK's retry loop and is repeated like a request that got
    no headers. Not under `live_answer()`: there the stream is passed on untouched (the chat's
    answer), and a break ends the request with the error, as before. A request without
    streaming is read whole by httpx anyway; this only reads it a step earlier."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if _live.get():
            return response
        try:
            body = b"".join([chunk async for chunk in response.stream])  # type: ignore[union-attr]
        finally:
            await response.aclose()
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=httpx.ByteStream(body),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


class EffortGuard(httpx.AsyncBaseTransport):
    """Leaves out `reasoning_effort` for an endpoint that rejects it: the first HTTP 400 that
    names the field is retried once without it, and every later request of this model profile
    goes without it (until the settings change)."""

    def __init__(self, inner: httpx.AsyncBaseTransport):
        self._inner = inner
        self.rejected = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.rejected:
            request = await _without_effort(request) or request
        response = await self._inner.handle_async_request(request)
        if self.rejected or response.status_code != 400:
            return response
        await response.aread()
        if "reasoning_effort" not in response.text:
            return response
        retry = await _without_effort(request)
        if retry is None:
            return response
        self.rejected = True
        log.warning("%s rejects reasoning_effort: it is no longer sent", request.url.host)
        return await self._inner.handle_async_request(retry)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _without_effort(request: httpx.Request) -> httpx.Request | None:
    """The same request without `reasoning_effort` in its JSON body; None when it has none."""
    body = await request.aread()
    if b"reasoning_effort" not in body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.pop("reasoning_effort", None) is None:
        return None
    headers = httpx.Headers(request.headers)
    headers.pop("content-length", None)
    return httpx.Request(
        request.method,
        request.url,
        headers=headers,
        content=json.dumps(data).encode(),
        extensions=request.extensions,
    )


class ModelHub:
    """Builds and owns the models (and their HTTP clients) of one settings generation."""

    def __init__(
        self,
        settings: Settings,
        *,
        tracing: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._configs: dict[Profile, LLMSettings] = {
            "main": settings.llm,
            "worker": worker_config(settings),
        }
        self._tracing = tracing
        self._transport = transport
        self._models: dict[Profile, OpenAIChatModel] = {}
        self._clients: list[httpx.AsyncClient] = []

    def config(self, profile: Profile) -> LLMSettings:
        return self._configs[profile]

    def configured(self, profile: Profile = "main") -> bool:
        return self._configs[profile].configured

    def name(self, profile: Profile) -> str:
        return self._configs[profile].model

    def model(self, profile: Profile) -> OpenAIChatModel:
        if profile not in self._models:
            self._models[profile] = self._build(self._configs[profile], profile)
        return self._models[profile]

    def settings(self, profile: Profile) -> ModelSettings:
        return model_settings(self._configs[profile])

    def _build(self, cfg: LLMSettings, profile: Profile) -> OpenAIChatModel:
        transport = self._transport
        if self._tracing is not None:
            transport = self._tracing.wrap_transport(transport)
        transport = WholeBody(EffortGuard(transport or httpx.AsyncHTTPTransport()))
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout.total_seconds(), connect=10.0),
            transport=transport,
        )
        self._clients.append(client)
        openai_client = AsyncOpenAI(
            base_url=cfg.base_url.rstrip("/"),
            api_key=cfg.api_key.get_secret_value() or NO_KEY,
            http_client=client,
            max_retries=cfg.max_retries,
        )
        provider = OpenAICompatibleProvider(
            f"asm-{profile}", openai_client=openai_client, profile=model_profile(cfg)
        )
        model = HubModel(cfg.model, provider=provider, settings=model_settings(cfg))
        model.structured_output = cfg.structured_output
        return model

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
        self._clients.clear()
        self._models.clear()
