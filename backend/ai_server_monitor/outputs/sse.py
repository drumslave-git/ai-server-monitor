"""Server-sent events over the in-process event bus."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

from ..events import Event, EventBus

KEEPALIVE_S = 15.0
RETRY_MS = 3000
QUEUE_SIZE = 512
"""Per client. A client that cannot keep up loses its oldest events; publishers never wait."""

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",  # nginx/traefik: do not buffer the stream
    "Connection": "keep-alive",
}


def format_event(event: Event) -> str:
    data = json.dumps({"ts": event.ts, **event.data}, separators=(",", ":"), default=str)
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


def parse_types(raw: str | None) -> tuple[str, ...]:
    """'report,metrics' -> ('report', 'metrics'). A type matches itself and its children."""
    return tuple(t.strip() for t in (raw or "").split(",") if t.strip())


async def event_stream(
    bus: EventBus,
    types: tuple[str, ...] = (),
    *,
    keepalive_s: float = KEEPALIVE_S,
    is_disconnected: Callable[[], bool] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames until the client goes away (the generator is then closed, which
    unsubscribes) or the bus shuts down."""
    sub = bus.subscribe(*types, maxsize=QUEUE_SIZE)
    try:
        yield f"retry: {RETRY_MS}\n\n"
        while True:
            try:
                event = await asyncio.wait_for(sub.get(), keepalive_s)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:
                return
            yield format_event(event)
    finally:
        sub.close()
