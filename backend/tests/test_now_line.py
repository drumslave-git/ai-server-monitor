"""Every agent run knows the current moment: one line with weekday, date, time and time zone
(Settings -> General time zone, else the host's), computed per run."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.agent import prompts
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.context import AppContext
from ai_server_monitor.jobs.builtins import DAILY
from ai_server_monitor.jobs.prompts import summary_input

from .conftest import LLM_URL
from .jobfakes import ScriptedModels, finish, tool
from .mcp_fake import FakeMcp
from .test_chat import FakeLLM, say
from .test_jobs import make_app, run
from .test_mcp_chat import McpLLM, settings_for

# 22:30 UTC on Thursday 19 November is already Friday 20 November in Kyiv (UTC+2)
THU_2230_UTC = datetime(2026, 11, 19, 22, 30, tzinfo=UTC).timestamp()
KYIV_LINE = "Now: Friday 2026-11-20 00:30, time zone Europe/Kyiv (UTC+02:00)."
NEXT_DAY_LINE = "Now: Saturday 2026-11-21 00:30, time zone Europe/Kyiv (UTC+02:00)."


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The clock of the date line, frozen; the test moves it by changing `clock[0]`."""
    now = [THU_2230_UTC]
    monkeypatch.setattr(prompts, "time", SimpleNamespace(time=lambda: now[0]))
    return now


def system_text(request: dict[str, Any]) -> str:
    return json.dumps([m for m in request["messages"] if m["role"] == "system"])


def test_the_line_is_in_the_zone_with_its_offset_and_the_knowledge_note() -> None:
    line = prompts.now_line(ZoneInfo("Europe/Kyiv"), THU_2230_UTC)
    assert line.startswith(KYIV_LINE)
    assert "Your own knowledge ends earlier than this date" in line
    assert prompts.now_line(UTC, THU_2230_UTC).startswith(
        "Now: Thursday 2026-11-19 22:30, time zone UTC (UTC+00:00)."
    )


async def test_chat_turns_get_the_line_and_it_is_recomputed(
    tmp_path: Path, clock: list[float]
) -> None:
    llm = FakeLLM(say("Friday."), say("Saturday."))
    settings = Settings.model_validate(
        {
            "llm": {"base_url": LLM_URL, "model": "fake", "max_retries": 0},
            "general": {"timezone": "Europe/Kyiv"},
        }
    )
    app = AppContext(
        Bootstrap(data_dir=tmp_path / "data", host_root=tmp_path / "no-host"),
        llm_transport=httpx.MockTransport(llm.handler),
        seed_settings=settings,
    )
    await app.start()
    try:
        events = [e async for e in app.chat.run("which day is it?")]
        assert events[-1]["type"] == "done"
        first = system_text(llm.requests[0])
        assert KYIV_LINE in first and "Thursday" not in first
        assert "never answered from memory" in first  # the look-it-up sentence
        clock[0] += 86400
        session = events[-1]["session_id"]
        [e async for e in app.chat.run("and now?", session_id=session)]
        second = system_text(llm.requests[-1])
        assert NEXT_DAY_LINE in second and KYIV_LINE not in second
    finally:
        await app.stop()


def test_job_runs_get_the_line_in_the_configured_zone(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str], clock: list[float]
) -> None:
    with TestClient(make_app(bootstrap, general={"timezone": "Europe/Kyiv"})) as client:
        models.script = [tool("bash", command="df -h /"), finish("ok", "Fine.")]
        run(client, "storage:usage")
        assert KYIV_LINE in models.requests[-1]["instructions"]
        clock[0] += 86400
        for slug in DAILY:  # the summary alone: its jobs are off
            client.put(f"/api/jobs/{slug}", json={"enabled": False})
        models.script = [finish("ok", "Fine.")]
        run(client, "summary:daily")
        assert NEXT_DAY_LINE in models.requests[-1]["instructions"]


def test_the_summary_input_stamp_is_in_the_owner_zone_not_container_time() -> None:
    text = summary_input([], None, now=THU_2230_UTC, zone=ZoneInfo("Europe/Kyiv"))
    assert "(now 2026-11-20 00:30 Europe/Kyiv)" in text


async def test_the_classification_gets_the_line_and_the_tool_names_per_domain(
    tmp_path: Path, clock: list[float]
) -> None:
    from .test_mcp_chat import start_app

    server = FakeMcp("streamable_http")
    await server.start()
    try:
        llm = McpLLM(say("It is warm."))
        llm.picks = ["home"]
        s = settings_for(server.url)
        s.general.timezone = "Europe/Kyiv"
        async for app in start_app(tmp_path, llm, s):
            [e async for e in app.chat.run("how warm is the kitchen?")]
            assert len(llm.classify_requests) == 1
            request = llm.classify_requests[0]
            assert KYIV_LINE in system_text(request)
            text = request["messages"][-1]["content"]
            assert "(tools: home_get_temperature, home_set_heating)" in text
    finally:
        await server.stop()
