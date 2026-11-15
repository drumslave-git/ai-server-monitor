"""What is behind the LLM endpoint: backend kind and version, model size, quantization.

A benchmark result is meaningless without it ("gemma4:e4b" at Q4 on Ollama 0.13 is not the same
measurement as at Q8), so every run stores what the endpoint says about itself. Probing never
raises: whatever cannot be found stays empty. Plain HTTP, no tracing (these are not LLM calls).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import LLMSettings

log = logging.getLogger(__name__)

TIMEOUT_S = 8.0


def _root(base_url: str) -> str:
    """The server root of an OpenAI-style base URL (`…/v1` → `…`)."""
    url = base_url.rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


async def _get(client: httpx.AsyncClient, url: str) -> Any:
    try:
        response = await client.get(url)
        if response.status_code != 200:
            return None
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _context_from_info(info: dict[str, Any]) -> int | None:
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int):
            return value
    return None


async def _ollama(client: httpx.AsyncClient, root: str, model: str) -> dict[str, Any] | None:
    version = await _get(client, f"{root}/api/version")
    if not isinstance(version, dict) or "version" not in version:
        return None
    out: dict[str, Any] = {"kind": "ollama", "version": str(version["version"])}
    try:
        response = await client.post(f"{root}/api/show", json={"model": model})
        shown = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        shown = {}
    details = shown.get("details") or {}
    info = shown.get("model_info") or {}
    out.update(
        params=details.get("parameter_size"),
        quantization=details.get("quantization_level"),
        family=details.get("family"),
        format=details.get("format"),
        context_length=_context_from_info(info),
        architecture=info.get("general.architecture"),
        parameter_count=info.get("general.parameter_count"),
        capabilities=shown.get("capabilities"),
    )
    return out


async def _llamacpp(client: httpx.AsyncClient, root: str, model: str) -> dict[str, Any] | None:
    props = await _get(client, f"{root}/props")
    if not isinstance(props, dict) or "default_generation_settings" not in props:
        return None
    gen = props.get("default_generation_settings") or {}
    out: dict[str, Any] = {
        "kind": "llama.cpp",
        "version": props.get("build_info"),
        "context_length": gen.get("n_ctx"),
        "model_path": props.get("model_path"),
    }
    models = await _get(client, f"{root}/v1/models")
    for entry in (models or {}).get("data", []) if isinstance(models, dict) else []:
        meta = entry.get("meta") or {}
        if meta:
            out["parameter_count"] = meta.get("n_params")
            out["size_bytes"] = meta.get("size")
            out["train_context"] = meta.get("n_ctx_train")
            break
    path = str(out.get("model_path") or "")
    stem = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    for token in ("Q8_0", "Q6_K", "Q5_K_M", "Q5_K_S", "Q4_K_M", "Q4_K_S", "Q4_0", "Q3_K_M", "Q2_K"):
        if token.lower() in stem.lower():
            out["quantization"] = token
            break
    return out


async def _vllm(client: httpx.AsyncClient, root: str, model: str) -> dict[str, Any] | None:
    version = await _get(client, f"{root}/version")
    if not isinstance(version, dict) or "version" not in version:
        return None
    return {"kind": "vllm", "version": str(version["version"])}


async def _models_entry(client: httpx.AsyncClient, base: str, model: str) -> dict[str, Any] | None:
    listing = await _get(client, f"{base.rstrip('/')}/models")
    data = listing.get("data") if isinstance(listing, dict) else None
    if not isinstance(data, list):
        return None
    for entry in data:
        if isinstance(entry, dict) and entry.get("id") == model:
            return entry
    return None


async def probe_backend(
    cfg: LLMSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """{kind, version, params, quantization, context_length, family, ...} for the configured
    endpoint and model. `kind` is ollama | llama.cpp | vllm | openai-compatible."""
    base = cfg.base_url.strip()
    headers = {}
    key = cfg.api_key.get_secret_value().strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    result: dict[str, Any] = {"kind": "openai-compatible", "version": None}
    if not base:
        return result
    root = _root(base)
    async with httpx.AsyncClient(
        headers=headers, timeout=TIMEOUT_S, transport=transport, follow_redirects=True
    ) as client:
        found: dict[str, Any] | None = None
        for probe in (_ollama, _llamacpp, _vllm):
            try:
                found = await probe(client, root, cfg.model)
            except Exception:  # a probe must never fail the benchmark
                log.debug("backend probe %s failed", probe.__name__, exc_info=True)
                found = None
            if found:
                break
        if found:
            result.update({k: v for k, v in found.items() if v is not None})
        entry = await _models_entry(client, base, cfg.model)
        if entry:
            result["models_entry"] = {
                k: entry[k]
                for k in ("id", "owned_by", "object", "max_model_len", "created")
                if k in entry
            }
            if "context_length" not in result and entry.get("max_model_len"):
                result["context_length"] = entry["max_model_len"]
    return result


async def ollama_loaded(
    cfg: LLMSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any] | None:
    """Is the configured model in memory right now? Ollama's `/api/ps` → {loaded, size,
    size_vram}; None when the backend has no such endpoint (not Ollama) or does not answer."""
    base = cfg.base_url.strip()
    if not base:
        return None
    headers = {}
    key = cfg.api_key.get_secret_value().strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(
        headers=headers, timeout=TIMEOUT_S, transport=transport, follow_redirects=True
    ) as client:
        data = await _get(client, f"{_root(base)}/api/ps")
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    wanted = cfg.model.strip()
    for entry in data["models"]:
        names = {str(entry.get("name", "")), str(entry.get("model", ""))}
        if wanted in names or f"{wanted}:latest" in names:
            return {
                "loaded": True,
                "size": entry.get("size"),
                "size_vram": entry.get("size_vram"),
            }
    return {"loaded": False, "size": None, "size_vram": None}
