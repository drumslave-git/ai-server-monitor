from __future__ import annotations

import asyncio

from ai_server_monitor.events import EventBus


async def test_fan_out_to_all_subscribers_in_order() -> None:
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish("x", {"n": 1})
    bus.publish("y", {"n": 2})
    assert [(e.type, e.data["n"]) for e in [await a.get(), await a.get()]] == [("x", 1), ("y", 2)]
    assert (await b.get()).type == "x"


async def test_type_filter_matches_prefix() -> None:
    bus = EventBus()
    sub = bus.subscribe("report")
    bus.publish("other.thing")
    bus.publish("report.token", {"text": "hi"})
    bus.publish("reporting.nope")  # not a 'report.*' event
    event = await asyncio.wait_for(sub.get(), 1)
    assert event.type == "report.token" and event.id == 2


async def test_full_queue_drops_oldest_and_never_blocks() -> None:
    bus = EventBus()
    slow = bus.subscribe(maxsize=3)
    for i in range(10):
        bus.publish("t", {"i": i})  # publisher is never blocked
    got = [(await slow.get()).data["i"] for _ in range(3)]
    assert got == [7, 8, 9] and slow.dropped == 7


async def test_close_ends_iteration_and_unsubscribes() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish("t")
    seen: list[str] = []

    async def consume() -> None:
        async for event in sub:
            seen.append(event.type)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    sub.close()
    await asyncio.wait_for(task, 1)
    assert seen == ["t"] and bus.subscriber_count == 0
    bus.publish("after")  # no subscribers: fine


async def test_bus_close_stops_everyone() -> None:
    bus = EventBus()
    subs = [bus.subscribe() for _ in range(3)]
    bus.close()
    for sub in subs:
        assert sub.closed
    assert bus.subscriber_count == 0
