"""`Tracing`: the one object the app holds. Wires the store, the transport, the app log and the
event bus together.

Phase-2 wiring, in short:

    tracing = Tracing(db, config, scrubber=lambda: Scrubber(settings), events=bus)
    tracing.applog.install(); tracing.applog.start()
    httpx.AsyncClient(transport=tracing.wrap_transport(inner))  # every request is recorded
    app.state.tracing = tracing; app.include_router(tracing.api.router)  # tracing/api.py
    scheduler job: tracing.prune()
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from ..db import Database
from .applog import AppLog
from .config import TraceConfig
from .scope import TraceScope, current_scope
from .store import ToolRecord, TraceStore
from .transport import PendingCall, TracingTransport

log = logging.getLogger(__name__)


class _Bus(Protocol):
    def publish(self, type: str, data: dict[str, Any] | None = None) -> Any: ...


class ToolTimer:
    """Started when the chat engine begins a tool call, finished when it has the result."""

    def __init__(self, tracing: Tracing, tool: str, args: Any):
        self._tracing = tracing
        self.scope = current_scope()
        self.tool = tool
        self.args = args
        self.ts = time.time()
        self._t0 = time.perf_counter()
        self._done = False

    def finish(
        self,
        *,
        ok: bool,
        result: Any = None,
        error: str | None = None,
        result_size: int | None = None,
    ) -> None:
        if self._done:
            return
        self._done = True
        scope = self.scope
        if result_size is None and result is not None:
            result_size = len(result) if isinstance(result, str) else len(repr(result))
        self._tracing._submit_tool(
            ToolRecord(
                ts=self.ts,
                tool=self.tool,
                ok=ok,
                link_kind=scope.link_kind if scope else None,
                link_id=scope.link_id if scope else None,
                link_step=scope.step if scope else None,
                turn=scope.turn if scope else None,
                duration_ms=round((time.perf_counter() - self._t0) * 1000),
                result_size=result_size,
                error=error,
                args=self.args,
                result=result,
            )
        )


class Tracing:
    def __init__(
        self,
        db: Database,
        config: TraceConfig | None = None,
        *,
        scrubber: Callable[[], Any] | None = None,
        events: _Bus | None = None,
    ):
        self.config = config or TraceConfig()
        self.store = TraceStore(db, self.config, scrubber)
        self.events = events
        self.applog = AppLog(self.store, self.config)
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------ recording

    def wrap_transport(self, inner: httpx.AsyncBaseTransport | None = None) -> TracingTransport:
        return TracingTransport(inner, self._submit_call, lambda: self.config.enabled)

    def start_tool(self, tool: str, args: Any) -> ToolTimer:
        return ToolTimer(self, tool, args)

    def scrubber(self) -> Any | None:
        return self.store._scrub()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _submit_call(self, call: PendingCall) -> None:
        try:
            record = call.record()
            self._spawn(self._save_call(record, call.scope))
        except Exception:
            log.warning("could not record an LLM call", exc_info=True)

    def _submit_tool(self, record: ToolRecord) -> None:
        if not self.config.enabled:
            return
        try:
            self._spawn(self._save_tool(record))
        except Exception:
            log.warning("could not record a tool call", exc_info=True)

    async def _save_call(self, record: Any, scope: TraceScope | None) -> None:
        try:
            call_id = await self.store.insert_call(record)
        except Exception:
            log.warning("could not store an LLM call", exc_info=True)
            return
        if scope is not None:
            scope.add_call(call_id)
        if self.events is not None:
            self.events.publish(
                "llm.call",
                {
                    "id": call_id,
                    "purpose": record.purpose,
                    "status": record.status,
                    "total_ms": record.total_ms,
                    "prompt_tokens": record.prompt_tokens,
                    "completion_tokens": record.completion_tokens,
                    "error_type": None
                    if record.status == "ok"
                    else (f"http_{record.http_status}" if record.http_status else record.status),
                },
            )

    async def _save_tool(self, record: ToolRecord) -> None:
        try:
            await self.store.insert_tool(record)
        except Exception:
            log.warning("could not store a tool call", exc_info=True)

    async def flush(self) -> None:
        """Wait until everything submitted so far is stored."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
            # Finished tasks leave the set through a done-callback; awaiting only finished
            # tasks never yields, so without this the loop could spin forever.
            await asyncio.sleep(0)

    async def attach(
        self, target: TraceScope | list[int], link_kind: str, link_id: str | int
    ) -> int:
        """Link the calls recorded in a scope to something that got its id afterwards."""
        await self.flush()
        ids = target.call_ids if isinstance(target, TraceScope) else target
        return await self.store.attach(list(ids), link_kind, link_id)

    # ------------------------------------------------------------------ lifecycle

    def apply_settings(self, logs: Any) -> None:
        """Take over the `logs` settings section (a `config.LogsSettings`)."""
        cfg = self.config
        cfg.enabled = logs.llm_trace
        cfg.store_bodies = logs.store_bodies
        cfg.retention_days = logs.retention.total_seconds() / 86400
        cfg.max_total_mb = logs.max_total_mb
        self.applog.keep_stored(logs.keep_app_log)

    async def prune(self) -> dict[str, int]:
        return await self.store.prune()

    async def stop(self) -> None:
        await self.flush()
        await self.applog.stop()
