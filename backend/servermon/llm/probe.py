"""Detect how (and whether) the configured model can call tools."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from .client import LLMClient, LLMStatusError
from .jsontools import json_tool_system_prompt, parse_tool_request

if TYPE_CHECKING:
    from ..db import Database

log = logging.getLogger(__name__)

ResolvedToolMode = Literal["native", "json", "none"]

_KV_KEY = "llm.tool_mode"
_CACHE_TTL = timedelta(days=7)

PROBE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_magic_number",
        "description": "Returns today's magic number. The only way to learn the magic number.",
        "parameters": {
            "type": "object",
            "properties": {"seed": {"type": "integer", "description": "Any integer."}},
            "required": [],
        },
    },
}
_QUESTION = "What is today's magic number? Call the get_magic_number tool to find out."
# Statuses that mean "this endpoint/model does not accept tools" rather than "broken".
_TOOLS_UNSUPPORTED = {400, 404, 422, 500, 501}


async def probe_tool_mode(client: LLMClient) -> ResolvedToolMode:
    """Probe native tool calling first, then the JSON-in-text fallback.

    Connection/auth errors propagate so the caller can retry later.
    """
    native_messages = [
        {"role": "system", "content": "You are an assistant with tools. Use them when needed."},
        {"role": "user", "content": _QUESTION},
    ]
    try:
        result = await client.complete(
            native_messages, tools=[PROBE_TOOL], tool_choice="auto", temperature=0, max_tokens=300
        )
        if any(call.name == "get_magic_number" for call in result.tool_calls):
            return "native"
        log.info("model did not use native tool call; trying JSON fallback")
    except LLMStatusError as exc:
        if exc.status_code not in _TOOLS_UNSUPPORTED:
            raise
        log.info("endpoint rejected native tools (%s); trying JSON fallback", exc.status_code)

    json_messages = [
        {"role": "system", "content": json_tool_system_prompt([PROBE_TOOL])},
        {"role": "user", "content": _QUESTION},
    ]
    result = await client.complete(json_messages, temperature=0, max_tokens=300)
    if parse_tool_request(result.content, {"get_magic_number"}):
        return "json"
    return "none"


class ToolModeResolver:
    """Resolves the effective tool mode once: from config, a cached probe, or a new probe.

    `resolve()` is safe to run repeatedly as a scheduler job; it is a no-op once resolved.
    """

    def __init__(self, client: LLMClient, db: Database, configured: str):
        self.client = client
        self.db = db
        self.configured = configured
        self.mode: ResolvedToolMode | None = None
        self.source: Literal["config", "cache", "probe"] | None = None
        self.resolved_at: float | None = None

    def _cache_matches(self, cached: Any) -> bool:
        return (
            isinstance(cached, dict)
            and cached.get("base_url") == self.client.cfg.base_url
            and cached.get("model") == self.client.cfg.model
            and cached.get("mode") in ("native", "json", "none")
            and time.time() - float(cached.get("ts", 0)) < _CACHE_TTL.total_seconds()
        )

    async def resolve(self) -> ResolvedToolMode:
        if self.mode is not None:
            return self.mode
        if self.configured != "auto":
            self._set(self.configured, "config")  # type: ignore[arg-type]
            return self.mode  # type: ignore[return-value]
        cached = await self.db.kv_get(_KV_KEY)
        if self._cache_matches(cached):
            self._set(cached["mode"], "cache")
            return self.mode  # type: ignore[return-value]
        mode = await probe_tool_mode(self.client)
        await self.db.kv_set(
            _KV_KEY,
            {
                "base_url": self.client.cfg.base_url,
                "model": self.client.cfg.model,
                "mode": mode,
                "ts": time.time(),
            },
        )
        self._set(mode, "probe")
        return mode

    def _set(self, mode: ResolvedToolMode, source: Literal["config", "cache", "probe"]) -> None:
        self.mode = mode
        self.source = source
        self.resolved_at = time.time()
        log.info("LLM tool mode: %s (from %s)", mode, source)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "effective": self.mode,
            "source": self.source,
            "resolved_at": self.resolved_at,
        }
