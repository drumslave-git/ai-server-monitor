"""The shared agent loop (`agent/runtime.py`) outside the chat: approval-required tools and a
structured final result (what scheduled jobs will use)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel

from ai_server_monitor.agent.models import ModelHub
from ai_server_monitor.agent.runtime import AgentConfig, AgentTool, run_agent
from ai_server_monitor.agent.tools import file_for_approval
from ai_server_monitor.config import Settings

from .conftest import LLM_URL
from .test_chat import FakeLLM, call, say


def hub(llm: FakeLLM) -> ModelHub:
    settings = Settings.model_validate({"llm": {"base_url": LLM_URL, "model": "fake"}})
    return ModelHub(settings, transport=httpx.MockTransport(llm.handler))


async def collect(cfg: AgentConfig, prompt: str) -> list[dict[str, Any]]:
    return [event async for event in run_agent(cfg, prompt)]


async def test_an_approval_required_call_is_filed_and_the_model_continues() -> None:
    filed: list[dict[str, Any]] = []

    async def file_request(args: dict[str, Any]) -> dict[str, Any]:
        filed.append(args)
        return {"status": "pending", "note": "NOT DONE: waiting for the owner's approval"}

    tool = AgentTool(
        name="set_heating",
        description="Set the heating (changes things).",
        parameters={
            "type": "object",
            "properties": {"celsius": {"type": "number"}},
            "required": ["celsius"],
        },
        handler=file_request,
        requires_approval=True,
    )
    llm = FakeLLM(call("set_heating", {"celsius": 21}, "h1"), say("It waits for your approval."))
    models = hub(llm)
    try:
        events = await collect(
            AgentConfig(
                model=models.model("main"),
                instructions="test",
                tools=[tool],
                on_approval=file_for_approval,
            ),
            "warmer please",
        )
    finally:
        await models.aclose()
    assert filed == [{"celsius": 21}]  # filed once, by the approval bridge
    [result] = [e for e in events if e["type"] == "tool_result"]
    assert result["name"] == "set_heating" and result["ok"] and "pending" in result["text"]
    # The model read what happened to the call (as the deferred call's result) and went on.
    second = llm.requests[1]
    assert "NOT DONE" in json.dumps(second["messages"][-1])
    assert events[-1]["type"] == "end" and events[-1]["output"] == "It waits for your approval."


class Finding(BaseModel):
    status: str
    summary: str


async def test_a_structured_result_ends_the_run() -> None:
    llm = FakeLLM(call("final_result", {"status": "warn", "summary": "Disk 91% full."}, "f1"))
    models = hub(llm)
    try:
        events = await collect(
            AgentConfig(model=models.model("main"), instructions="test", output_type=Finding),
            "check the disks",
        )
    finally:
        await models.aclose()
    end = events[-1]
    assert end["type"] == "end" and end["output"] == Finding(
        status="warn", summary="Disk 91% full."
    )
    assert len(llm.requests) == 1
