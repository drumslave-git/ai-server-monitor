from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import httpx
import pytest

from ai_server_monitor.config import Bootstrap
from ai_server_monitor.events import EventBus
from ai_server_monitor.outputs.sse import event_stream, format_event, parse_types

from .live_server import live_server
from .test_chat import FakeLLM, make_app, say

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


def test_llm_calls_arrive_end_to_end_over_a_real_connection(bootstrap: Bootstrap) -> None:
    app = make_app(bootstrap, FakeLLM(say("All good.")))
    with live_server(app) as base, httpx.Client(base_url=base, timeout=15) as client:
        with client.stream("GET", "/api/stream", params={"types": "llm"}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"].startswith("no-cache")

            def trigger() -> None:
                time.sleep(0.5)  # the server subscribes once the stream is open
                assert httpx.post(f"{base}/api/chat", json={"message": "hi"}, timeout=15).is_success

            threading.Thread(target=trigger).start()
            frames = read_sse(response, until="llm.call")
        events = [f["event"] for f in frames if "event" in f]
        assert events == ["llm.call"] and frames[0].get("retry") == "3000"
        call = frames[-1]["data"]
        assert call["status"] == "ok" and call["prompt_tokens"] == 10
        ids = [int(f["id"]) for f in frames if "id" in f]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)

        status = client.get("/api/status").json()
        assert status["llm_configured"] is True and status["main_model"] == "fake"

        deadline = time.time() + 5  # the disconnect is noticed by the server
        while app.state.ctx.events.subscriber_count > 0 and time.time() < deadline:
            time.sleep(0.05)
        assert app.state.ctx.events.subscriber_count == 0
