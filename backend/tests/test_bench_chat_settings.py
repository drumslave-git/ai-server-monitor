"""A benchmark run uses the chat settings of the app (tool routing, ...) and records them."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app

from .conftest import LLM_URL
from .fake_llm import CANNED_REPORT
from .test_bench_run import StubSystem, probe_transport, wait_done
from .test_chat import FakeLLM, say


def start_run(bootstrap: Bootstrap, chat: dict[str, Any] | None) -> dict[str, Any]:
    settings = Settings.model_validate(
        {
            "llm": {"base_url": LLM_URL, "model": "fake-model", "max_retries": 0},
            "chat": chat or {},
        }
    )
    app = create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(FakeLLM(say(CANNED_REPORT)).handler),
        collector_registry={"system": StubSystem},
        seed_settings=settings,
    )
    with TestClient(app) as client:
        app.state.ctx.bench._probe_transport = probe_transport()
        response = client.post("/api/bench/runs", json={"ids": ["report.smoke_disk"], "repeats": 1})
        assert response.status_code == 202, response.text
        wait_done(client)
        return client.get(f"/api/bench/runs/{response.json()['run_id']}").json()


@pytest.mark.parametrize("mode", ["off", "always", "auto"])
def test_run_records_the_routing_mode_it_ran_with(bootstrap: Bootstrap, mode: str) -> None:
    done = start_run(bootstrap, {"tool_routing": mode, "tool_routing_tokens": 3000})
    assert done["status"] == "done", done
    chat = done["meta"]["chat"]
    assert chat["tool_routing"] == mode and chat["tool_routing_tokens"] == 3000
    assert chat["tool_timeout_s"] == 3600 and chat["max_steps"] == 6
    assert set(chat) == {
        "tool_routing",
        "tool_routing_tokens",
        "max_steps",
        "history_messages",
        "max_tool_result_chars",
        "tool_timeout_s",
    }


def test_default_chat_settings_are_recorded_too(bootstrap: Bootstrap) -> None:
    done = start_run(bootstrap, None)
    assert done["meta"]["chat"]["tool_routing"] == "auto"
