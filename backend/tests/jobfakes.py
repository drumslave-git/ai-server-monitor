"""Scripted models for job tests: Pydantic AI's FunctionModel in place of the configured ones."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from ai_server_monitor.agent.models import ModelHub

Step = str | tuple[str, dict[str, Any]] | Callable[[list[ModelMessage], AgentInfo], Any]


def tool(name: str, **args: Any) -> tuple[str, dict[str, Any]]:
    return (name, args)


def finish(status: str = "ok", summary: str = "All good.", findings: Any = ()) -> tuple:
    return tool("final_result", status=status, summary=summary, findings=list(findings))


class ScriptedModels:
    """Each model request takes the next step of the script: text, or a tool call. Records
    what each request offered and was told."""

    def __init__(self) -> None:
        self.script: list[Step] = []
        self.requests: list[dict[str, Any]] = []

    async def stream(self, messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        self.requests.append(
            {
                "tools": [t.name for t in info.function_tools],
                "instructions": info.instructions or "",
                "messages": messages,
                "text": str(messages),
            }
        )
        step: Step = self.script.pop(0) if self.script else "(script ended)"
        if step == "<sleep>":
            await asyncio.sleep(30)
        if callable(step):
            step = step(messages, info)
        if isinstance(step, str):
            yield step
        else:
            name, args = step
            n = len(self.requests)
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"t{n}")}


@pytest.fixture
def models(monkeypatch: pytest.MonkeyPatch) -> ScriptedModels:
    scripted = ScriptedModels()
    model = FunctionModel(stream_function=scripted.stream, model_name="scripted")
    monkeypatch.setattr(ModelHub, "model", lambda self, profile: model)
    monkeypatch.setattr(ModelHub, "configured", lambda self, profile="main": True)
    return scripted
