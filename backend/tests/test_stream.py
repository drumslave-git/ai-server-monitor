from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import pytest

from ai_server_monitor.collectors.base import CollectorResult
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.events import EventBus
from ai_server_monitor.main import create_app
from ai_server_monitor.outputs.live import LiveFeed, tick_payload
from ai_server_monitor.outputs.sse import event_stream, format_event, parse_types

from .fake_llm import CANNED_REPORT, stream_response
from .live_server import live_server
from .test_app import StubSystem

# --------------------------------------------------------------------------- framing


def test_frame_format_and_type_filter_parsing() -> None:
    bus = EventBus()
    event = bus.publish("report.token", {"text": 'say "hi"\n'})
    frame = format_event(event)
    assert frame.endswith("\n\n")
    lines = frame.rstrip("\n").split("\n")
    assert lines[0] == "id: 1" and lines[1] == "event: report.token"
    assert len(lines) == 3  # the payload newline is JSON-escaped: exactly one data line
    payload = json.loads(lines[2].removeprefix("data: "))
    assert payload["text"] == 'say "hi"\n' and "ts" in payload
    assert parse_types(" report , metrics ,") == ("report", "metrics") and parse_types(None) == ()


async def test_stream_sends_retry_then_events_then_keepalives() -> None:
    bus = EventBus()
    stream = event_stream(bus, ("report",), keepalive_s=0.05)
    assert await anext(stream) == "retry: 3000\n\n"
    assert await anext(stream) == ": keepalive\n\n"  # idle: comment frame
    bus.publish("metrics.tick", {"cpu": 1})  # filtered out
    bus.publish("report.done", {"id": 7})
    frame = await anext(stream)
    assert frame.startswith("id: 2\nevent: report.done\n")
    await stream.aclose()
    assert bus.subscriber_count == 0  # unsubscribed on close


async def test_stream_ends_when_the_bus_shuts_down() -> None:
    bus = EventBus()
    stream = event_stream(bus)
    await anext(stream)
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0.01)
    bus.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(task, 1)


async def test_slow_client_drops_oldest_and_never_blocks_publishers() -> None:
    bus = EventBus()
    stream = event_stream(bus)
    await anext(stream)
    for i in range(2000):  # far more than the per-client queue
        bus.publish("report.token", {"i": i})
    frame = await anext(stream)
    first = json.loads(frame.split("data: ")[1])["i"]
    assert first > 0  # the oldest were dropped
    await stream.aclose()


# --------------------------------------------------------------------------- live feed


def system_result(ok: bool = True, error: str | None = None) -> CollectorResult:
    data = {
        "cpu": {"percent": 12.34, "load": [0.5, 0.4, 0.3]},
        "memory": {"percent": 40.0, "swap_percent": 1.0},
        "network": [{"rx_bps": 100.4, "tx_bps": 50}, {"rx_bps": 10, "tx_bps": 5}],
    }
    return CollectorResult("system", 100.0, ok=ok, data=data if ok else {}, error=error)


def test_tick_payloads_are_compact() -> None:
    tick = tick_payload(system_result())
    assert tick == {
        "source": "system",
        "ts": 100.0,
        "cpu": 12.3,
        "mem": 40.0,
        "swap": 1.0,
        "load": 0.5,
        "net_rx_bps": 110,
        "net_tx_bps": 55,
    }
    gpu_data = {
        "gpus": [
            {
                "index": 0,
                "util_percent": 5,
                "mem_percent": 3.3,
                "temperature_c": 25,
                "power_w": 22.7,
                "name": "x",
                "processes": [1],
            }
        ]
    }
    gpu = tick_payload(CollectorResult("gpu", 1.0, ok=True, data=gpu_data))
    assert gpu is not None and gpu["gpus"] == [
        {"index": 0, "util": 5.0, "vram": 3.3, "temp": 25.0, "power_w": 22.7}
    ]
    assert tick_payload(CollectorResult("docker", 1.0, ok=True, data={})) is None
    assert tick_payload(system_result(ok=False, error="x")) is None


def test_collector_status_events_only_on_change() -> None:
    bus = EventBus()
    sub = bus.subscribe("collector")
    feed = LiveFeed(bus)
    results = [
        system_result(),
        system_result(),
        system_result(False, "boom"),
        system_result(False, "boom"),
        system_result(),
    ]
    for result in results:
        feed.on_result(result)
    states = []
    while not sub._queue.empty():
        event = sub._queue.get_nowait()
        assert event is not None
        states.append((event.data["ok"], event.data["error"]))
    assert states == [(True, None), (False, "boom"), (True, None)]


# --------------------------------------------------------------------------- end to end


def read_sse(response: httpx.Response, until: str, limit: int = 400) -> list[dict[str, Any]]:
    """Parse frames from a streaming response until an event named `until` arrives."""
    frames: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in response.iter_lines():
        if line == "":
            if current:
                frames.append(current)
                if current.get("event") == until or len(frames) >= limit:
                    break
            current = {}
        elif line.startswith(":"):
            current.setdefault("comment", line[1:].strip())
        else:
            key, _, value = line.partition(": ")
            current[key] = json.loads(value) if key == "data" else value
    return frames


def fake_llm_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return stream_response(CANNED_REPORT, pieces=12)

    return httpx.MockTransport(handler)


def test_report_tokens_arrive_end_to_end_over_a_real_connection(
    bootstrap: Bootstrap, settings: Settings
) -> None:
    app = create_app(
        bootstrap,
        collector_registry={"system": StubSystem},
        seed_settings=settings,
        llm_transport=fake_llm_transport(),
    )
    with live_server(app) as base, httpx.Client(base_url=base, timeout=15) as client:
        with client.stream("GET", "/api/stream", params={"types": "report"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"].startswith("no-cache")

            def trigger() -> None:
                time.sleep(0.5)  # the server subscribes once the stream is open
                assert httpx.post(f"{base}/api/reports/run").status_code == 202

            threading.Thread(target=trigger).start()
            frames = read_sse(response, until="report.done")
        events = [f["event"] for f in frames if "event" in f]
        assert events[0] == "report.started" and events[-1] == "report.done"
        tokens = [f["data"]["text"] for f in frames if f.get("event") == "report.token"]
        assert len(tokens) >= 10 and "".join(tokens) == CANNED_REPORT  # every token, in order
        ids = [int(f["id"]) for f in frames if "id" in f]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        done = frames[-1]["data"]
        assert done["health"] == "warn" and done["score"] == 74 and done["id"] == 1
        assert not any(e.startswith("metrics") for e in events)  # type filter respected

        # The report is stored and /api/status reflects it.
        status = client.get("/api/status").json()
        assert status["report"]["id"] == 1 and status["report"]["score"] == 74
        assert status["last_attempt"] is None and status["llm_configured"] is True

        deadline = time.time() + 5  # the disconnect is noticed by the server
        while app.state.ctx.events.subscriber_count > 1 and time.time() < deadline:
            time.sleep(0.05)
        assert app.state.ctx.events.subscriber_count == 1  # only the metrics consumer is left


def test_unfiltered_stream_carries_ticks_and_status_changes(
    bootstrap: Bootstrap, settings: Settings
) -> None:
    app = create_app(bootstrap, collector_registry={"system": StubSystem}, seed_settings=settings)
    with (
        live_server(app) as base,
        httpx.Client(base_url=base, timeout=15) as client,
        client.stream("GET", "/api/stream") as response,
    ):
        ctx = app.state.ctx

        def feed() -> None:
            time.sleep(0.5)
            ctx.live.on_result(system_result(False, "boom"))  # a state change
            ctx.live.on_result(system_result())

        threading.Thread(target=feed).start()
        frames = read_sse(response, until="metrics.tick")
    names = [f["event"] for f in frames if "event" in f]
    assert "collector.status" in names and names[-1] == "metrics.tick"
    tick = frames[-1]["data"]
    assert tick["source"] == "system" and tick["cpu"] == 12.3
    assert frames[0].get("retry") == "3000"
