"""What an LLM call is for and what it belongs to, set by callers through context variables.

    with trace_scope("chat", link_kind="chat", link_id=session_id, turn=turn_id, step=2):
        await llm.stream(...)

Scopes nest: an inner scope inherits whatever it does not set. Every call recorded inside a
scope is appended to `scope.call_ids` (and to its parents'), which lets a caller link the
calls to something that only gets an id later (a report row): `await tracing.attach(scope,
"report", report_id)`.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceScope:
    purpose: str = "other"
    link_kind: str | None = None
    link_id: str | None = None
    step: int | None = None
    turn: str | None = None
    parent: TraceScope | None = None
    call_ids: list[int] = field(default_factory=list)

    def add_call(self, call_id: int) -> None:
        scope: TraceScope | None = self
        while scope is not None:
            scope.call_ids.append(call_id)
            scope = scope.parent


_scope: ContextVar[TraceScope | None] = ContextVar("llm_trace_scope", default=None)


def current_scope() -> TraceScope | None:
    return _scope.get()


@contextmanager
def trace_scope(
    purpose: str | None = None,
    *,
    link_kind: str | None = None,
    link_id: str | int | None = None,
    step: int | None = None,
    turn: str | None = None,
) -> Iterator[TraceScope]:
    parent = _scope.get()
    base = parent or TraceScope()
    scope = TraceScope(
        purpose=purpose or base.purpose,
        link_kind=link_kind if link_kind is not None else base.link_kind,
        link_id=str(link_id) if link_id is not None else base.link_id,
        step=step if step is not None else base.step,
        turn=turn if turn is not None else base.turn,
        parent=parent,
    )
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


@dataclass
class CallGroup:
    """One logical LLM call; each HTTP attempt (retries!) is a separate trace row of it."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    attempt: int = 0
    started: float = field(default_factory=time.perf_counter)
    last_end: float | None = None


_group: ContextVar[CallGroup | None] = ContextVar("llm_trace_group", default=None)


def current_group() -> CallGroup | None:
    return _group.get()


@contextmanager
def trace_call() -> Iterator[CallGroup]:
    """Wrap one logical call (including its retries) so attempts are numbered and the wait
    between them shows up as queue time. Optional: without it every request is its own call."""
    group = CallGroup()
    token = _group.set(group)
    try:
        yield group
    finally:
        _group.reset(token)


async def scoped_iter(
    source: AsyncIterator[Any],
    purpose: str | None = None,
    *,
    call: bool = False,
    **scope: Any,
) -> AsyncIterator[Any]:
    """Iterate `source` (an async generator that talks to the LLM) with a trace scope and/or
    a call group active only while it runs, never while the consumer holds the item.

    Scopes set with `with` inside an async generator would stay set across its `yield`s, in
    the consumer's context, and could even be reset from another one when the generator is
    closed. This brackets each step of the source instead: the variables are set and reset in
    the same frame, around one `await`."""
    group = CallGroup() if call else None
    iterator = source.__aiter__()
    try:
        while True:
            token = _group.set(group) if group is not None else None
            try:
                with trace_scope(purpose, **scope):
                    item = await iterator.__anext__()
            except StopAsyncIteration:
                return
            finally:
                if token is not None:
                    _group.reset(token)
            yield item
    finally:
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
