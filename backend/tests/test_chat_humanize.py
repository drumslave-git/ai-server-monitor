from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ai_server_monitor.chat.humanize import humanize, size, span
from ai_server_monitor.config import Bootstrap

from .test_chat import FakeLLM, ask, call, make_app, prepare, say


def test_sizes_and_spans() -> None:
    assert size(0) == "0 B" and size(1023) == "1023 B" and size(1536) == "1.5 KiB"
    assert size(1_569_484_800) == "1.5 GiB" and size(3 * 1024**4) == "3.0 TiB"
    assert span(59) == "59s" and span(125) == "2m 5s" and span(3 * 3600 + 25 * 60) == "3h 25m"
    assert span(3 * 86400 + 4 * 3600 + 5) == "3d 4h"


def test_byte_fields_rates_durations_and_timestamps_are_spelled_out() -> None:
    data = {
        "processes": [{"pid": 6802, "name": "python3.13", "rss": 1_569_484_800, "threads": 50}],
        "memory": {"total": 133_000_000_000, "used": 11_600_000_000, "percent": 8.7},
        "uptime_s": 3 * 86400 + 4 * 3600,
        "age_s": 5,
        "net_rx_bps": 33_400,
        "containers": [{"name": "ollama", "mem_usage": 3_921_244_160, "mem_limit": 0}],
        "movie": {"title": "Heat", "sizeOnDisk": 12_000_000_000},
        "since": 1_794_700_000,
    }
    out = humanize(data)
    assert out["processes"][0] == {
        "pid": 6802,
        "name": "python3.13",
        "rss": "1.5 GiB",
        "threads": 50,
    }
    assert out["memory"] == {"total": "123.9 GiB", "used": "10.8 GiB", "percent": 8.7}
    assert out["uptime"] == "3d 4h" and "uptime_s" not in out
    assert out["age_s"] == 5  # a few seconds stay numbers
    assert out["net_rx_bps"] == "32.6 KiB/s"
    assert out["containers"][0] == {"name": "ollama", "mem_usage": "3.7 GiB", "mem_limit": "0 B"}
    assert out["movie"]["sizeOnDisk"] == "11.2 GiB"
    assert out["since"].endswith("UTC") and out["since"].startswith("2026-")
    assert data["processes"][0]["rss"] == 1_569_484_800  # the input is not changed


def test_ambiguous_numbers_are_left_alone() -> None:
    data = {
        "total": 10,  # an item count, no byte companions
        "size": 12,  # too small to be a size
        "items": [{"total": 2048}],  # no companion either: not guessed
        "page": {"total": 3, "used": 2},
        "id": 1_000_000,
        "flag": True,
        "count": 123456,
    }
    assert humanize(data) == data


def test_the_chat_model_sees_readable_sizes_while_sorting_still_uses_raw_numbers(
    bootstrap: Bootstrap,
) -> None:
    llm = FakeLLM(
        call("get_collector_data", {"collector": "system", "section": "processes.top_memory"}),
        say("ollama, 9 MB."),
    )
    app = make_app(bootstrap, llm)
    with TestClient(app) as client:
        prepare(client, app)
        events = ask(client, "what is using the most memory?")
        result = next(e for e in events if e["type"] == "tool_result")
        assert '"rss":"8.6 MiB"' in result["preview"]
        sent = llm.requests[1]["messages"][-1]["content"]
        assert "8.6 MiB" in sent and "9000000" not in sent
        order = [m["name"] for m in json.loads(sent.split("\n")[1])["items"]]
        assert order == ["ollama", "postgres"]  # still ranked by the raw numbers
