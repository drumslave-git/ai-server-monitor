from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator

import pytest

from ai_server_monitor.db import Database
from ai_server_monitor.tracing.applog import AppLog, RingHandler

from .tracing_helpers import API_KEY, make_tracing


@pytest.fixture
def logger() -> Iterator[logging.Logger]:
    log = logging.getLogger("asm_test_applog")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    yield log
    log.handlers.clear()


def ring(logger: logging.Logger, size: int = 5000) -> RingHandler:
    handler = RingHandler(size)
    logger.addHandler(handler)
    return handler


def test_ring_keeps_only_the_last_n_records(logger: logging.Logger) -> None:
    handler = ring(logger, size=5)
    for i in range(12):
        logger.info("message %d", i)
    records = handler.query(limit=100)
    assert [r["message"] for r in records] == [f"message {i}" for i in range(11, 6, -1)]
    assert records[0]["seq"] == 12 and handler.last_seq == 12


def test_record_fields_and_exception_text(logger: logging.Logger) -> None:
    handler = ring(logger)
    try:
        raise ValueError("bad thing")
    except ValueError:
        logger.exception("worker %s failed", "docker")
    (rec,) = handler.query()
    assert rec["level"] == "ERROR" and rec["levelno"] == 40
    assert rec["logger"] == "asm_test_applog" and rec["message"] == "worker docker failed"
    assert "ValueError: bad thing" in rec["exc"] and "Traceback" in rec["exc"]
    assert abs(rec["ts"] - time.time()) < 5


def test_filters_by_level_logger_and_text(logger: logging.Logger) -> None:
    handler = ring(logger)
    other = logging.getLogger("asm_test_applog.workers.docker")
    other.setLevel(logging.DEBUG)
    logger.debug("quiet detail")
    logger.info("Server started")
    logger.warning("Disk almost full")
    other.error("docker socket missing")
    assert [r["message"] for r in handler.query(min_levelno=30)] == [
        "docker socket missing",
        "Disk almost full",
    ]
    assert [r["message"] for r in handler.query(logger="asm_test_applog.workers")] == [
        "docker socket missing"
    ]
    assert [r["message"] for r in handler.query(logger="asm_test_applog.coll")] == []  # whole names
    assert [r["message"] for r in handler.query(q="DISK")] == ["Disk almost full"]
    assert len(handler.query(limit=2)) == 2
    assert handler.loggers() == ["asm_test_applog", "asm_test_applog.workers.docker"]


def test_paging_back_with_before(logger: logging.Logger) -> None:
    handler = ring(logger)
    for i in range(10):
        logger.info("m%d", i)
    first = handler.query(limit=4)
    older = handler.query(limit=4, before=first[-1]["seq"])
    assert [r["message"] for r in first] == ["m9", "m8", "m7", "m6"]
    assert [r["message"] for r in older] == ["m5", "m4", "m3", "m2"]


def test_a_failing_format_does_not_break_logging(logger: logging.Logger) -> None:
    handler = ring(logger)
    logging.raiseExceptions = False
    try:
        logger.info("needs %s and %s", "one")  # wrong number of arguments
    finally:
        logging.raiseExceptions = True
    (rec,) = handler.query()
    assert "needs" in rec["message"]


def test_logging_from_many_threads(logger: logging.Logger) -> None:
    handler = ring(logger, size=10_000)

    def work(n: int) -> None:
        for i in range(200):
            logger.info("t%d-%d", n, i)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records = handler.query(limit=10_000)
    assert len(records) == 1600
    assert sorted(r["seq"] for r in records) == list(range(1, 1601))


def test_since_returns_newer_records_oldest_first(logger: logging.Logger) -> None:
    handler = ring(logger)
    for i in range(5):
        logger.info("m%d", i)
    assert [r["message"] for r in handler.since(3)] == ["m3", "m4"]
    assert handler.since(5) == []


# --------------------------------------------------------------------------- sqlite sink


@pytest.fixture
async def applog(db: Database, logger: logging.Logger) -> AsyncIterator[AppLog]:
    tracing = await make_tracing(db)
    log = tracing.applog
    log.install(logger)
    yield log
    await log.stop()


async def test_warnings_and_above_are_stored_info_is_not(
    applog: AppLog, logger: logging.Logger
) -> None:
    logger.info("chatter")
    logger.warning("slow disk")
    logger.error("worker failed")
    assert await applog.flush() == 2
    stored = await applog.store.list_logs()
    assert [(r["level"], r["message"]) for r in stored] == [
        ("ERROR", "worker failed"),
        ("WARNING", "slow disk"),
    ]
    assert len(applog.ring.query()) == 3  # the ring has everything


async def test_stored_log_filters_and_secret_scrubbing(
    applog: AppLog, logger: logging.Logger
) -> None:
    logger.error("request with %s failed", API_KEY)
    logger.warning("100%_sure [x]")
    await applog.flush()
    store = applog.store
    assert API_KEY not in json.dumps(await store.list_logs())
    assert [r["message"] for r in await store.list_logs(q="%_sure")] == ["100%_sure [x]"]
    assert len(await store.list_logs(min_levelno=40)) == 1
    assert len(await store.list_logs(logger="asm_test_applog")) == 2
    assert len(await store.list_logs(logger="asm_test")) == 0
    newest = (await store.list_logs())[0]["id"]
    assert len(await store.list_logs(before=newest)) == 1


async def test_background_sink_flushes_on_its_own(
    applog: AppLog, logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ai_server_monitor.tracing.applog.SINK_FLUSH_S", 0.05)
    applog.start()
    logger.error("background")
    for _ in range(40):
        if await applog.store.list_logs():
            break
        await asyncio.sleep(0.05)
    assert [r["message"] for r in await applog.store.list_logs()] == ["background"]


async def test_sink_failure_is_swallowed(
    applog: AppLog,
    logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def boom(records: object) -> None:
        raise RuntimeError("db locked")

    monkeypatch.setattr(applog.store, "insert_logs", boom)
    logger.error("will be lost")
    assert await applog.flush() == 0
    assert "db locked" in capsys.readouterr().err


async def test_sink_queue_is_bounded(applog: AppLog, logger: logging.Logger) -> None:
    from ai_server_monitor.tracing import applog as module

    for i in range(module.SINK_QUEUE_LIMIT + 50):
        logger.error("e%d", i)
    assert applog._queue.qsize() == module.SINK_QUEUE_LIMIT


async def test_stop_flushes_and_detaches(db: Database, logger: logging.Logger) -> None:
    tracing = await make_tracing(db)
    tracing.applog.install(logger)
    logger.error("last words")
    await tracing.stop()
    assert [r["message"] for r in await tracing.store.list_logs()] == ["last words"]
    logger.error("after")
    assert tracing.applog.ring.query(q="after") == []


# --------------------------------------------------------------------------- live tail


async def read_frames(gen: AsyncIterator[str], count: int, timeout: float = 3) -> list[str]:
    frames: list[str] = []

    async def collect() -> None:
        async for frame in gen:
            frames.append(frame)
            if len(frames) >= count:
                return

    await asyncio.wait_for(collect(), timeout)
    return frames


async def test_tail_streams_only_new_matching_records(
    applog: AppLog, logger: logging.Logger
) -> None:
    logger.info("before the tail")
    gen = applog.tail(min_levelno=30, interval=0.02)
    task = asyncio.create_task(read_frames(gen, 2))
    await asyncio.sleep(0.1)
    logger.info("not wanted")
    logger.warning("wanted one")
    logger.error("wanted two")
    frames = await task
    assert frames[0] == "retry: 3000\n\n"
    assert frames[1].startswith("event: log\ndata: ")
    payload = json.loads(frames[1].split("data: ", 1)[1])
    assert [r["message"] for r in payload["records"]] == ["wanted one", "wanted two"]
    assert payload["skipped"] == 0
    await gen.aclose()  # type: ignore[attr-defined]


async def test_tail_is_rate_limited_and_counts_skipped(
    applog: AppLog, logger: logging.Logger
) -> None:
    gen = applog.tail(interval=0.05, batch=10)
    task = asyncio.create_task(read_frames(gen, 2))
    await asyncio.sleep(0.01)
    for i in range(500):
        logger.info("burst %d", i)
    frames = await task
    payload = json.loads(frames[1].split("data: ", 1)[1])
    assert len(payload["records"]) == 10 and payload["skipped"] == 490
    assert payload["records"][-1]["message"] == "burst 499"  # newest are kept
    await gen.aclose()  # type: ignore[attr-defined]


async def test_tail_sends_keepalives_when_quiet(applog: AppLog) -> None:
    gen = applog.tail(interval=0.01, keepalive_s=0.05)
    frames = await read_frames(gen, 2)
    assert frames[1] == ": keepalive\n\n"
    await gen.aclose()  # type: ignore[attr-defined]


async def test_tail_can_resume_after_a_sequence_number(
    applog: AppLog, logger: logging.Logger
) -> None:
    for i in range(3):
        logger.info("m%d", i)
    gen = applog.tail(after=1, interval=0.01)
    frames = await read_frames(gen, 2)
    payload = json.loads(frames[1].split("data: ", 1)[1])
    assert [r["message"] for r in payload["records"]] == ["m1", "m2"]
    await gen.aclose()  # type: ignore[attr-defined]
