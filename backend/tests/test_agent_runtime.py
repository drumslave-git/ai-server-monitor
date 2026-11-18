"""The shared agent loop (`agent/runtime.py`) outside the chat: approval-required tools and a
structured final result (what scheduled jobs will use)."""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import BaseModel

from ai_server_monitor.agent.models import ModelHub
from ai_server_monitor.agent.runtime import (
    AgentConfig,
    AgentTool,
    Suspended,
    resume_results,
    run_agent,
)
from ai_server_monitor.agent.tools import file_for_approval
from ai_server_monitor.config import Settings

from .conftest import LLM_URL
from .test_chat import FakeLLM, call, say


def hub(llm: FakeLLM) -> ModelHub:
    settings = Settings.model_validate({"llm": {"base_url": LLM_URL, "model": "fake"}})
    return ModelHub(settings, transport=httpx.MockTransport(llm.handler))


async def collect(cfg: AgentConfig, prompt: str) -> list[dict[str, Any]]:
    return [event async for event in run_agent(cfg, prompt)]


def heating_tool(answer: dict[str, Any], filed: list[dict[str, Any]]) -> AgentTool:
    async def file_request(args: dict[str, Any]) -> dict[str, Any]:
        filed.append(args)
        return answer

    return AgentTool(
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


async def test_a_call_settled_at_once_goes_back_to_the_model_in_the_same_run() -> None:
    filed: list[dict[str, Any]] = []
    tool = heating_tool({"status": "rejected", "note": "policy off: NOT DONE"}, filed)
    llm = FakeLLM(call("set_heating", {"celsius": 21}, "h1"), say("That is not allowed."))
    models = hub(llm)
    try:
        cfg = AgentConfig(
            model=models.model("main"),
            instructions="test",
            tools=[tool],
            on_approval=file_for_approval,
        )
        events = await collect(cfg, "warmer please")
    finally:
        await models.aclose()
    assert filed == [{"celsius": 21}]  # filed once, by the approval bridge
    [result] = [e for e in events if e["type"] == "tool_result"]
    assert result["name"] == "set_heating" and "rejected" in result["text"]
    assert "policy off: NOT DONE" in json.dumps(llm.requests[1]["messages"][-1])
    assert events[-1]["output"] == "That is not allowed."


async def test_a_pending_call_stops_the_run_and_it_resumes_with_the_outcome() -> None:
    filed: list[dict[str, Any]] = []
    pending = {"status": "pending", "action_id": 7, "approval_id": 3, "target": "heating 21"}
    tool = heating_tool(pending, filed)
    llm = FakeLLM(call("set_heating", {"celsius": 21}, "h1"), say("Done: it is 21 now."))
    models = hub(llm)
    try:
        cfg = AgentConfig(
            model=models.model("main"),
            instructions="test",
            tools=[tool],
            on_approval=file_for_approval,
        )
        events = await collect(cfg, "warmer please")
        suspended = events[-1]["output"]
        assert isinstance(suspended, Suspended) and len(llm.requests) == 1
        assert suspended.pending["h1"].action_ids == [7]
        assert "Waiting for approval #3: heating 21" in suspended.pending["h1"].message
        # stored as JSON and read back (a restart in between)
        stored = Suspended.from_json(json.loads(json.dumps(suspended.to_json())))
        # approved and executed by the approval service: the model gets the output
        results, resolved = resume_results(stored, {"h1": (True, "heating is 21 now")})
        cfg.resolved = resolved
        resumed = [e async for e in run_agent(cfg, None, history=stored.messages, deferred=results)]
    finally:
        await models.aclose()
    assert filed == [{"celsius": 21}]  # not filed a second time
    assert "heating is 21 now" in json.dumps(llm.requests[1]["messages"])
    [result] = [e for e in resumed if e["type"] == "tool_result"]
    assert result["ok"] and result["text"] == "heating is 21 now"
    assert resumed[-1]["output"] == "Done: it is 21 now."


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
