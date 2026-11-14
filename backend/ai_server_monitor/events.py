"""Minimal in-process pub/sub.

Event types published today (exposed to browsers by GET /api/stream):

    report.started | report.token | report.done | report.failed   (report engine)
    metrics.tick                                                    (system/gpu collectors)
    collector.status                                                (collector state changes)

Reserved for the action layer (M9), which should publish them on this bus:

    approval.pending   a write action is waiting for a decision
    approval.decided   an approval was approved/rejected/expired
    action.result      an action finished (executed/failed/dry_run)

`publish` never blocks and never fails: every subscriber has a bounded queue, and when it
is full the oldest event is dropped (a slow consumer loses history, never stalls the
publisher). Consumers use `async for event in subscription`.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "type": self.type, "ts": self.ts, "data": self.data}


class Subscription:
    def __init__(self, bus: EventBus, maxsize: int, types: frozenset[str] | None):
        self._bus = bus
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize)
        self._types = types
        self.dropped = 0
        self.closed = False

    def matches(self, event: Event) -> bool:
        return self._types is None or any(
            event.type == t or event.type.startswith(t + ".") for t in self._types
        )

    def _put(self, event: Event | None) -> None:
        while True:
            try:
                self._queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:  # pragma: no cover - single-threaded, cannot happen
                    return

    async def get(self) -> Event:
        """Next event. Raises StopAsyncIteration once the subscription is closed."""
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._bus._remove(self)
            self._put(None)  # wakes a waiting consumer

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        return await self.get()

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []
        self._ids = itertools.count(1)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def subscribe(self, *types: str, maxsize: int = 256) -> Subscription:
        """Subscribe to everything, or to the given event types (a prefix such as
        'report' also matches 'report.token')."""
        sub = Subscription(self, maxsize, frozenset(types) or None)
        self._subs.append(sub)
        return sub

    def publish(self, type: str, data: dict[str, Any] | None = None) -> Event:
        event = Event(type=type, data=data or {}, id=next(self._ids))
        for sub in list(self._subs):
            if sub.matches(event):
                sub._put(event)
        return event

    def _remove(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def close(self) -> None:
        for sub in list(self._subs):
            sub.close()
