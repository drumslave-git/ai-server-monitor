"""The two model profiles as Pydantic AI models: `main` (Settings → Main model, the stored
`llm` section) and `worker` (Settings → Worker model; falls back to the main model).

Every request goes through our own httpx client whose transport is the tracing transport, so
each call lands in `llm_calls` (bodies, tokens, timing) whatever agent made it.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.profiles import ModelProfile, merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai_compatible import OpenAICompatibleProvider
from pydantic_ai.settings import ModelSettings

from ..config import LLMSettings, Settings

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
    tool schemas; Ollama's own profile (reasoning field, schema quirks) when it is Ollama."""
    base = OllamaProvider.model_profile(cfg.model) if _is_ollama(cfg.base_url) else None
    local = OpenAIModelProfile(
        openai_chat_supports_max_completion_tokens=False,
        openai_supports_strict_tool_definition=False,
    )
    return merge_profile(base, local)


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
        transport = EffortGuard(transport or httpx.AsyncHTTPTransport())
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
        return OpenAIChatModel(cfg.model, provider=provider, settings=model_settings(cfg))

    async def aclose(self) -> None:
        for client in self._clients:
            await client.aclose()
        self._clients.clear()
        self._models.clear()
