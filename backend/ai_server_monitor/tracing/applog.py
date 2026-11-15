"""The app's own log: an in-memory ring buffer on the root logger, an optional SQLite sink for
WARNING and above, and a live tail.

The ring is read by sequence number (`seq`, per process), the SQLite rows by id; the two
never mix. The tail polls the ring instead of being pushed to from `emit` (which may run on any
thread), which also makes it naturally rate-limited: one frame per interval, at most `batch`
records, with a count of what was skipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sys
import threading
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

from .config import TraceConfig
from .store import TraceStore

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
MAX_MESSAGE = 8000
MAX_EXC = 12000
SINK_QUEUE_LIMIT = 2000
SINK_FLUSH_S = 2.0


def levelno(name: str | None) -> int:
    return LEVELS.get((name or "").upper(), 0)


def _match(rec: dict[str, Any], min_levelno: int, logger: str | None, q: str | None) -> bool:
    if rec["levelno"] < min_levelno:
        return False
    if logger and not (rec["logger"] == logger or rec["logger"].startswith(logger + ".")):
        return False
    if q:
        needle = q.lower()
        return needle in rec["message"].lower() or needle in (rec.get("exc") or "").lower()
    return True


class RingHandler(logging.Handler):
    def __init__(self, size: int = 5000, sink: queue.SimpleQueue[dict[str, Any]] | None = None):
        super().__init__(level=logging.NOTSET)
        self._ring: deque[dict[str, Any]] = deque(maxlen=size)
        self._lock = threading.Lock()
        self._seq = 0
        self._busy = threading.local()
        self.sink = sink
        self.sink_levelno = logging.WARNING

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._busy, "on", False):  # a record produced while formatting one
            return
        self._busy.on = True
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            exc = None
            if record.exc_info and record.exc_info[0] is not None:
                exc = logging.Formatter().formatException(record.exc_info)[-MAX_EXC:]
            rec: dict[str, Any] = {
                "ts": record.created,
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "message": message[:MAX_MESSAGE],
                "exc": exc,
            }
            with self._lock:
                self._seq += 1
                rec["seq"] = self._seq
                self._ring.append(rec)
            if (
                self.sink is not None
                and record.levelno >= self.sink_levelno
                and self.sink.qsize() < SINK_QUEUE_LIMIT
            ):
                self.sink.put(rec)
        except Exception:
            self.handleError(record)
        finally:
            self._busy.on = False

    def query(
        self,
        *,
        limit: int = 200,
        min_levelno: int = 0,
        logger: str | None = None,
        q: str | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        """Newest first."""
        with self._lock:
            snapshot = list(self._ring)
        out: list[dict[str, Any]] = []
        for rec in reversed(snapshot):
            if before is not None and rec["seq"] >= before:
                continue
            if _match(rec, min_levelno, logger, q):
                out.append(rec)
                if len(out) >= limit:
                    break
        return out

    def since(self, seq: int) -> list[dict[str, Any]]:
        """Records newer than `seq`, oldest first."""
        with self._lock:
            return [r for r in self._ring if r["seq"] > seq]

    def loggers(self) -> list[str]:
        with self._lock:
            return sorted({r["logger"] for r in self._ring})

    def clear(self) -> None:
        with self._lock:
            self._ring.clear()


class AppLog:
    """Ring buffer + SQLite sink + tail, attached to the root logger by `install()`."""

    def __init__(self, store: TraceStore, config: TraceConfig):
        self.store = store
        self.config = config
        self._queue: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self.ring = RingHandler(config.ring_size, self._queue)
        self.ring.sink_levelno = levelno(config.store_level) or logging.WARNING
        self._task: asyncio.Task[None] | None = None
        self._installed = False
        self._root = logging.getLogger()

    def install(self, root: logging.Logger | None = None) -> None:
        if not self._installed:
            (root or logging.getLogger()).addHandler(self.ring)
            self._installed = True
        self._root = root or logging.getLogger()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="applog-sink")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.flush()
        if self._installed:
            self._root.removeHandler(self.ring)
            self._installed = False

    async def flush(self) -> int:
        """Write what the sink has queued. Returns the number of records stored."""
        batch: list[dict[str, Any]] = []
        while len(batch) < 1000:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        try:
            await self.store.insert_logs(batch)
        except Exception as exc:  # not logged: that would feed the sink again
            print(f"app log sink: could not store {len(batch)} records: {exc}", file=sys.stderr)
            return 0
        return len(batch)

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(SINK_FLUSH_S)
            await self.flush()

    # ------------------------------------------------------------------ live tail

    async def tail(
        self,
        *,
        min_levelno: int = 0,
        logger: str | None = None,
        q: str | None = None,
        interval: float = 0.5,
        batch: int = 100,
        keepalive_s: float = 15.0,
        after: int | None = None,
        scrub: Callable[[str], str] | None = None,
    ) -> AsyncIterator[str]:
        """SSE frames: `event: log` with {"records": [...], "skipped": n}, oldest first.
        Starts after the current end of the ring unless `after` (a seq) is given."""
        seq = self.ring.last_seq if after is None else after
        yield "retry: 3000\n\n"
        idle = 0.0
        while True:
            fresh = self.ring.since(seq)
            if fresh:
                seq = fresh[-1]["seq"]
                wanted = [r for r in fresh if _match(r, min_levelno, logger, q)]
                skipped = max(0, len(wanted) - batch)
                wanted = wanted[-batch:]
                if scrub is not None:
                    wanted = [{**r, "message": scrub(r["message"])} for r in wanted]
                if wanted:
                    payload = {"records": wanted, "skipped": skipped}
                    yield f"event: log\ndata: {json.dumps(payload, default=str)}\n\n"
                    idle = 0.0
            await asyncio.sleep(interval)
            idle += interval
            if idle >= keepalive_s:
                idle = 0.0
                yield ": keepalive\n\n"
