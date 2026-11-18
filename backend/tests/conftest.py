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
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(llm={"base_url": LLM_URL, "model": "test-model"})


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


SHELL_SECRET = "sk-live-0123456789abcdef"
"""The same value as the chat tests' configured API key: the fake shell prints it."""


@pytest.fixture
def shell(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replaces the host shell of the `bash` tool: records the commands and prints a df line
    (with a configured secret in it). A command containing `sleep` takes 0.4 s."""
    import asyncio

    from ai_server_monitor.agent.shell import HostShell, ShellResult

    commands: list[str] = []

    async def run(self: HostShell, command: str, *, timeout_s: float, max_chars: int) -> Any:
        commands.append(command)
        if "sleep" in command:
            await asyncio.sleep(0.4)
        output = f"/dev/sda1  100G  40G  60G  40% /\nTOKEN={SHELL_SECRET}\n"
        return ShellResult(exit_code=0, output=output, duration_s=0.01, output_bytes=len(output))

    monkeypatch.setattr(HostShell, "run", run)
    return commands
