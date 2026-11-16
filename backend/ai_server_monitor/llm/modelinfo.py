"""What the selected model says about itself, read from Ollama's `/api/show`: its own sampling
defaults (the `parameters` of its template), its capabilities and its context length.

Only Ollama answers this; any other endpoint (OpenAI, llama.cpp, vLLM, OpenRouter...) gives
None, quietly. The answer is cached per (base URL, model), so a changed model is read again and
the same one is not asked about on every page view."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from ..config import LLMSettings

log = logging.getLogger(__name__)

CACHE_SECONDS = 600
MISS_SECONDS = 60
"""A model that could not be read (not Ollama, unreachable) is not asked again for this long."""
SHOW_TIMEOUT_S = 8.0


@dataclass
class ModelInfo:
    parameters: dict[str, float] = field(default_factory=dict)
    """The model's own sampling defaults, e.g. {"temperature": 1, "top_k": 64, "top_p": 0.95}."""
    capabilities: list[str] = field(default_factory=list)
    context_length: int | None = None

    @property
    def thinks(self) -> bool:
        return "thinking" in self.capabilities

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_cache: dict[tuple[str, str], tuple[float, ModelInfo | None]] = {}


def ollama_root(base_url: str) -> str:
    """`http://host:11434/v1` -> `http://host:11434`."""
    root = base_url.strip().rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


def parse_show(data: Any) -> ModelInfo | None:
    """A `/api/show` reply as a ModelInfo; None when it is not one."""
    if not isinstance(data, dict) or not (
        "parameters" in data or "capabilities" in data or "model_info" in data
    ):
        return None
    params: dict[str, float] = {}
    for line in str(data.get("parameters") or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or parts[0] == "stop":
            continue
        try:
            params[parts[0]] = float(parts[1])
        except ValueError:
            continue
    caps = data.get("capabilities")
    context = None
    info = data.get("model_info")
    if isinstance(info, dict):
        for key, value in info.items():
            if key.endswith(".context_length") and isinstance(value, int):
                context = value
                break
    return ModelInfo(
        parameters={k: int(v) if v == int(v) else v for k, v in params.items()},
        capabilities=[c for c in caps if isinstance(c, str)] if isinstance(caps, list) else [],
        context_length=context,
    )


async def read_model_info(
    cfg: LLMSettings, *, transport: httpx.AsyncBaseTransport | None = None, force: bool = False
) -> ModelInfo | None:
    """The model's info, or None when the endpoint is not Ollama or cannot be reached."""
    if not cfg.configured:
        return None
    key = (cfg.base_url.strip().rstrip("/"), cfg.model.strip())
    hit = _cache.get(key)
    if hit and not force and time.monotonic() < hit[0]:
        return hit[1]
    info: ModelInfo | None = None
    headers = {}
    api_key = cfg.api_key.get_secret_value()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(
            timeout=SHOW_TIMEOUT_S, transport=transport, headers=headers
        ) as http:
            response = await http.post(
                ollama_root(cfg.base_url) + "/api/show", json={"model": cfg.model.strip()}
            )
        if response.status_code < 400:
            info = parse_show(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("no model info for %s: %s", key, exc)
    _cache[key] = (time.monotonic() + (CACHE_SECONDS if info else MISS_SECONDS), info)
    return info


def clear_cache() -> None:
    _cache.clear()
