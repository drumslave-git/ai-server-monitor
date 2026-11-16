"""The cold-start step of a run: one request that gets the model loaded before the first attempt.

When the user switches the model, the backend has to load it first (seconds to minutes); that
must not count against the model's speed or accuracy. So every run starts with a warm-up request
(the real chat system prompt plus "Reply with OK", no tools, a few tokens) that is timed and
recorded as the run's *cold start* and is left out of every score, median and rate.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from ..chat.prompts import system_prompt
from ..chat.tools import build_tools
from ..config import LLMSettings, Settings
from ..llm import LLMClient, LLMEmptyAnswerError, LLMError
from ..tracing import TraceScope, trace_scope
from .probe import ollama_loaded

log = logging.getLogger(__name__)

WARMUP_TIMEOUT_S = 300.0
"""Loading a big model can take a minute or more; one retry follows a timeout."""
WARMUP_MAX_TOKENS = 16
RELOAD_FACTOR = 5.0
"""An attempt whose first token takes more than this many times the run's median is suspect."""
RELOAD_MIN_TTFT_MS = 2000
"""...but only when it is also slow in absolute terms (100 ms → 600 ms is noise)."""
RELOAD_MIN_ATTEMPTS = 4


def warmup_messages() -> list[dict[str, str]]:
    names = list(build_tools(Settings(), assume_linux=True))
    system = system_prompt("en", tools=True, tool_names=names)
    return [{"role": "system", "content": system}, {"role": "user", "content": "Reply with OK"}]


async def warm_up(
    llm: LLMClient,
    cfg: LLMSettings,
    *,
    ps_transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = WARMUP_TIMEOUT_S,
) -> tuple[dict[str, Any], TraceScope]:
    """Run the warm-up → (cold-start record, its trace scope; the call ids fill in once the
    tracing has stored them: flush first).

    Raises LLMError when the model cannot be reached even after the retry."""
    before = await ollama_loaded(cfg, transport=ps_transport)
    record: dict[str, Any] = {
        "supported": before is not None,
        "loaded_before": None if before is None else before["loaded"],
        "size_vram_before": None if before is None else before.get("size_vram"),
    }
    last: Exception | None = None
    with trace_scope("bench") as scope:
        for attempt in (1, 2):
            started = time.perf_counter()
            ttft_ms: int | None = None
            text = ""
            try:
                async with asyncio.timeout(timeout_s):
                    async for chunk in llm.stream(
                        warmup_messages(),
                        temperature=0,
                        max_tokens=WARMUP_MAX_TOKENS,
                        reasoning_effort="none",
                    ):
                        if not chunk.done:
                            if ttft_ms is None:
                                ttft_ms = round((time.perf_counter() - started) * 1000)
                            text += chunk.text
                last = None
            except TimeoutError:
                last = LLMError(f"no answer to the warm-up request within {timeout_s:.0f}s")
            except LLMEmptyAnswerError:
                last = None  # it thought through the tiny limit: loaded and responding is enough
            except LLMError as exc:
                last = exc
            if last is None:
                record.update(
                    ok=True,
                    total_ms=round((time.perf_counter() - started) * 1000),
                    ttft_ms=ttft_ms,
                    reply=text.strip()[:40],
                    tries=attempt,
                )
                break
            log.info("bench warm-up try %d failed: %s", attempt, last)
    if last is not None:
        raise last
    after = await ollama_loaded(cfg, transport=ps_transport)
    if after is not None:
        record["size_vram"] = after.get("size_vram")
        record["loaded_after"] = after["loaded"]
        record["model"] = {k: after.get(k) for k in ("size", "size_vram", "context_length")}
    return record, scope


def reload_suspects(attempts: list[dict[str, Any]]) -> dict[int, str]:
    """attempt id → why a model reload is suspected: first token slower than RELOAD_FACTOR × the
    run's median (and at least RELOAD_MIN_TTFT_MS). `attempts` carry `id` and `ttft_ms`."""
    times = sorted(a["ttft_ms"] for a in attempts if a.get("ttft_ms") is not None)
    if len(times) < RELOAD_MIN_ATTEMPTS:
        return {}
    median = (
        times[len(times) // 2]
        if len(times) % 2
        else (times[len(times) // 2 - 1] + times[len(times) // 2]) / 2
    )
    out = {}
    for a in attempts:
        ttft = a.get("ttft_ms")
        if ttft is not None and ttft > RELOAD_FACTOR * median and ttft >= RELOAD_MIN_TTFT_MS:
            out[a["id"]] = (
                f"first token took {ttft / 1000:.1f}s, more than {RELOAD_FACTOR:g}× the run's "
                f"median ({median / 1000:.1f}s): the model may have been reloaded"
            )
    return out
