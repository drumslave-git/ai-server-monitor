"""The Logs API mounted on a bare app (the real app mounts the same router)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import FastAPI

from ai_server_monitor.db import Database
from ai_server_monitor.tracing import Tracing
from ai_server_monitor.tracing.api import router
from ai_server_monitor.tracing.store import ToolRecord

from .tracing_helpers import API_KEY, call, make_tracing


@pytest.fixture
async def tracing(db: Database) -> Tracing:
    return await make_tracing(db)


@pytest.fixture
async def http(tracing: Tracing) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.state.tracing = tracing
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
def applog_logger(tracing: Tracing) -> Iterator[logging.Logger]:
    log = logging.getLogger("asm_test_api")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    tracing.applog.install(log)
    yield log
    log.handlers.clear()


async def test_list_and_filters(http: httpx.AsyncClient, tracing: Tracing) -> None:
    now = time.time()
    await tracing.store.insert_call(call(ts=now - 30, purpose="report"))
    await tracing.store.insert_call(call(ts=now - 20, purpose="chat", status="error", error="x"))
    await tracing.store.insert_call(
        call(ts=now - 10, purpose="chat", link_kind="chat", link_id="s")
    )

    body = (await http.get("/api/logs/llm")).json()
    assert body["total"] == 3 and body["limit"] == 50 and body["complete"] is True
    first = body["items"][0]
    assert first["purpose"] == "chat" and first["request_preview"] == "Disk is 88% full"
    assert "messages" not in first and first["status"] == "ok"

    assert (await http.get("/api/logs/llm", params={"purpose": "chat"})).json()["total"] == 2
    assert (await http.get("/api/logs/llm", params={"status": "error"})).json()["total"] == 1
    assert (await http.get("/api/logs/llm", params={"link_kind": "chat", "link_id": "s"})).json()[
        "total"
    ] == 1
    assert (await http.get("/api/logs/llm", params={"since": now - 25})).json()["total"] == 2
    assert (await http.get("/api/logs/llm", params={"q": "88%"})).json()["total"] == 3
    paged = (await http.get("/api/logs/llm", params={"limit": 1, "offset": 2})).json()
    assert len(paged["items"]) == 1 and paged["items"][0]["purpose"] == "report"
    assert (await http.get("/api/logs/llm", params={"status": "bogus"})).status_code == 422
    assert (await http.get("/api/logs/llm", params={"limit": 0})).status_code == 422


async def test_detail_roundtrip_and_404(http: httpx.AsyncClient, tracing: Tracing) -> None:
    tools = [{"type": "function", "function": {"name": "disk_usage"}}]
    call_id = await tracing.store.insert_call(
        call(
            tools=tools,
            reasoning="hm",
            tool_calls=[{"id": "1", "name": "disk_usage", "arguments": "{}"}],
        )
    )
    body = (await http.get(f"/api/logs/llm/{call_id}")).json()
    assert body["id"] == call_id and body["messages"][0]["role"] == "system"
    assert body["tools"] == tools and body["reasoning"] == "hm" and body["content"]
    assert body["tool_calls"][0]["name"] == "disk_usage" and body["tokens_per_s"] == 5.0
    assert (await http.get("/api/logs/llm/9999")).status_code == 404
    assert (await http.get("/api/logs/llm/abc")).status_code == 422


async def test_export_is_a_download(http: httpx.AsyncClient, tracing: Tracing) -> None:
    call_id = await tracing.store.insert_call(call())
    response = await http.get(f"/api/logs/llm/{call_id}/export")
    assert (
        response.headers["content-disposition"] == f'attachment; filename="llm-call-{call_id}.json"'
    )
    assert json.loads(response.text)["messages"][1]["content"] == "Disk is 88% full"
    assert (await http.get("/api/logs/llm/9999/export")).status_code == 404


async def test_timeline_and_tool_detail(http: httpx.AsyncClient, tracing: Tracing) -> None:
    t0 = time.time()
    link = {"link_kind": "chat", "link_id": "s1", "turn": "t1"}
    a = await tracing.store.insert_call(call(ts=t0, link_step=1, **link))
    tool = await tracing.store.insert_tool(
        ToolRecord(ts=t0 + 1, tool="disk_usage", ok=True, args={"query": "up"}, result="42", **link)
    )
    b = await tracing.store.insert_call(call(ts=t0 + 2, link_step=2, **link))
    body = (await http.get(f"/api/logs/llm/{b}/timeline")).json()
    assert body["selected"] == b
    assert [(e["type"], e["id"]) for e in body["entries"]] == [
        ("llm", a),
        ("tool", tool),
        ("llm", b),
    ]
    detail = (await http.get(f"/api/logs/tools/{tool}")).json()
    assert detail["args"] == {"query": "up"} and detail["result"] == 42 and detail["ok"] is True
    assert (await http.get("/api/logs/tools/999")).status_code == 404
    assert (await http.get("/api/logs/llm/999/timeline")).status_code == 404


async def test_stats(http: httpx.AsyncClient, tracing: Tracing) -> None:
    await tracing.store.insert_call(call(purpose="report"))
    body = (await http.get("/api/logs/llm/stats")).json()
    assert body["calls"] == 1 and body["enabled"] is True and body["store_bodies"] is True
    assert body["retention_days"] == 7 and body["max_total_mb"] == 200
    assert body["purposes"] == [{"purpose": "report", "count": 1}]


async def test_app_log_memory_and_stored(
    http: httpx.AsyncClient, tracing: Tracing, applog_logger: logging.Logger
) -> None:
    applog_logger.info("hello")
    applog_logger.warning("careful %s", API_KEY)
    applog_logger.error("broken")
    await tracing.applog.flush()

    memory = (await http.get("/api/logs/app")).json()
    assert memory["source"] == "memory" and memory["newest"] == 3
    assert [r["message"] for r in memory["items"]][::-1][0] == "hello"
    assert API_KEY not in json.dumps(memory) and "[redacted]" in json.dumps(memory)
    warn = (await http.get("/api/logs/app", params={"level": "WARNING"})).json()
    assert [r["level"] for r in warn["items"]] == ["ERROR", "WARNING"]
    assert (await http.get("/api/logs/app", params={"q": "broke"})).json()["items"][0]["seq"] == 3
    assert (await http.get("/api/logs/app", params={"logger": "asm_test_api"})).json()["items"]
    assert not (await http.get("/api/logs/app", params={"logger": "nope"})).json()["items"]
    older = (await http.get("/api/logs/app", params={"before": 3})).json()
    assert [r["seq"] for r in older["items"]] == [2, 1]

    stored = (await http.get("/api/logs/app", params={"source": "stored"})).json()
    assert stored["source"] == "stored" and [r["level"] for r in stored["items"]] == [
        "ERROR",
        "WARNING",
    ]
    assert stored["items"][0]["id"] is not None and API_KEY not in json.dumps(stored)
    assert (await http.get("/api/logs/app", params={"level": "LOUD"})).status_code == 422


async def test_logger_names(http: httpx.AsyncClient, applog_logger: logging.Logger) -> None:
    applog_logger.info("x")
    assert (await http.get("/api/logs/app/loggers")).json() == ["asm_test_api"]


async def test_stream_endpoint_returns_sse_frames(
    tracing: Tracing, applog_logger: logging.Logger
) -> None:
    """Called directly: an ASGI test client would wait for the endless stream to finish."""
    from fastapi import Request

    from ai_server_monitor.tracing import api

    app = FastAPI()
    app.state.tracing = tracing
    request = Request({"type": "http", "app": app, "headers": [], "query_string": b""})
    response = await api.app_log_stream(request, level="WARNING", logger=None, q=None, after=None)
    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"
    gen = response.body_iterator
    assert await gen.__anext__() == "retry: 3000\n\n"  # type: ignore[attr-defined]
    applog_logger.error("seen on the tail")
    frame = await gen.__anext__()  # type: ignore[attr-defined]
    assert frame.startswith("event: log") and "seen on the tail" in frame
    await gen.aclose()  # type: ignore[attr-defined]
