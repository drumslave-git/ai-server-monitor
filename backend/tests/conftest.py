from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from ai_server_monitor.actions import REGISTRY
from ai_server_monitor.config import Bootstrap, LLMSettings, Settings
from ai_server_monitor.db import Database
from ai_server_monitor.llm import LLMClient

from .actionfakes import NAME, FakeAction

LLM_URL = "http://llm.test/v1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    for key in list(os.environ):
        if key.upper().startswith("ASM_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_action(monkeypatch: pytest.MonkeyPatch) -> FakeAction:
    """The fake write action, registered in the shared REGISTRY (so the chat tools, Telegram and
    the web API see it); its policy, cap and dry-run are attributes the test sets."""
    fake = FakeAction()
    monkeypatch.setitem(REGISTRY, NAME, fake.definition())
    return fake


@pytest.fixture
def llm_cfg() -> LLMSettings:
    return LLMSettings(base_url=LLM_URL, model="test-model", api_key="sk-test", max_retries=1)


@pytest.fixture
async def llm(llm_cfg: LLMSettings, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LLMClient]:
    client = LLMClient(llm_cfg)

    async def no_sleep(attempt: int) -> None:
        return None

    monkeypatch.setattr(client, "_backoff", no_sleep)
    yield client
    await client.aclose()


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(llm={"base_url": LLM_URL, "model": "test-model", "tool_mode": "none"})


@pytest.fixture
def bootstrap(tmp_path: Path) -> Bootstrap:
    return Bootstrap(data_dir=tmp_path / "data", host_root=tmp_path / "no-host")


def sse(*events: dict[str, Any] | str) -> bytes:
    """Build an SSE body from chunk dicts (or raw strings like '[DONE]')."""
    lines = []
    for event in events:
        payload = event if isinstance(event, str) else json.dumps(event)
        lines.append(f"data: {payload}\n\n")
    return "".join(lines).encode()


def chat_response(
    content: str | None = "",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
) -> httpx.Response:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)
