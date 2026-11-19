"""The Settings "Test" button of the Main and Worker model sections: is the endpoint reachable,
does it have the model, and does the model call a tool the way a real run needs it?

The tool-call part runs like a real run of that profile (`agent.runtime.run_agent`: streamed,
the saved model settings, nothing forced): main = the chat's instructions and its own tools,
worker = a topic job's instructions and tools. The model is asked to run `echo check-<word>`
with `bash` (the read-only shell). Only a real `bash` tool call passes; a model that writes
the call as text, or answers without it, fails with that said plainly. Of the chat's other
tools only the definitions are offered: they do nothing here.

Then one small structured call the way the helper calls make it (domain classification, command
marks, tool sorting: `agent.models.helper_output`), with the tested settings' "Structured
output": the result's `structured_output` says whether it worked."""

from __future__ import annotations

import secrets
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

import httpx
import openai
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior

from ..agent.models import ModelHub, helper_output
from ..agent.runtime import AgentConfig, AgentTool, run_agent
from ..config import LLMSettings, Settings
from ..tracing import trace_scope
from .text import strip_reasoning

if TYPE_CHECKING:
    from ..context import AppContext

NO_TOOLS = (
    "The model answered without calling the tool: it cannot call tools (native OpenAI tool "
    "calling), so this app cannot use it. Pick a model with tool support."
)
TEXT_INSTEAD = "tool call failed: the model answered with text instead of calling the tool"
TEST_ID = "settings:test"
MODES = {"json_schema": "JSON schema", "tool_call": "tool call"}


class Echo(BaseModel):
    """The structured part of the connection test."""

    word: str


def _request(word: str) -> str:
    return (
        f"This is a connection test. Run `echo {word}` with the `bash` tool now, then reply "
        "with only the word it printed."
    )


async def _unused(args: dict[str, Any]) -> str:
    return "Not available in the connection test: only `bash` runs here."


def _test_tools(app: AppContext, profile: Literal["main", "worker"]) -> list[AgentTool]:
    """The tools a real run of `profile` offers. `bash` runs (read-only shell, not in the chat
    audit trail); every other tool is only offered."""
    from ..agent.prompts import host_line
    from ..agent.safety import Scrubber
    from ..agent.tools import app_bash, app_shell

    shell = app_shell(app)
    bash = app_bash(app, None, shell)
    if profile == "worker":
        return [bash]
    from ..chat.engine import chat_tools

    tools = chat_tools(
        app, TEST_ID, TEST_ID, TEST_ID, shell, Scrubber(app.settings), host_line(app.host_env.root)
    )
    return [
        bash if t.name == "bash" else replace(t, handler=_unused, requires_approval=False)
        for t in tools
    ]


def _test_config(
    app: AppContext, profile: Literal["main", "worker"], model: Any, word: str
) -> tuple[AgentConfig, str]:
    """The agent configuration and prompt of a real run of `profile`, asking for the echo."""
    from ..agent.prompts import chat_prompt, host_line
    from ..agent.safety import Scrubber
    from ..chat.explain import host_zone

    settings = app.settings
    host = host_line(app.host_env.root)
    language = settings.general.language
    tools = _test_tools(app, profile)
    common = {
        "model": model,
        "tools": tools,
        "tool_timeout_s": settings.chat.tool_timeout.total_seconds(),
        "result_chars": settings.chat.max_tool_result_chars,
        "scrub": Scrubber(settings),
        "tracing": app.tracing,
        "trace": {"purpose": "test"},
        "zone": host_zone(app),  # the date line every real run gets
    }
    if profile == "worker":
        from ..jobs.prompts import topic_instructions
        from ..jobs.runner import JobResult

        cfg = AgentConfig(
            instructions=topic_instructions(host, language, _request(word)),
            max_steps=settings.jobs.max_steps,
            output_type=JobResult,
            **common,
        )
        return cfg, "Run the check now, then call final_result."
    cfg = AgentConfig(
        instructions=chat_prompt(host, language), max_steps=settings.chat.max_steps, **common
    )
    return cfg, _request(word)


async def check_model(
    cfg: LLMSettings,
    app: AppContext,
    *,
    profile: Literal["main", "worker"] = "main",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """{ok, reply, latency_s, models, model_listed, tool_call} or {ok: False, error, ...}.
    `cfg` is the model to test (for the worker already merged with the main endpoint)."""
    hub = ModelHub(
        Settings(llm=cfg.model_copy(update={"max_retries": 0})),
        tracing=app.tracing,
        transport=transport,
    )
    model = hub.model("main")
    models: list[str] | None = None
    try:
        try:
            models = [m.id async for m in model.client.models.list()]
        except openai.APIConnectionError as exc:
            return {"ok": False, "error": f"Could not reach {cfg.base_url}: {exc}"}
        except openai.APIError:
            models = None  # not every server lists its models
        listed = (cfg.model in models) if models else None
        if listed is False:
            return {
                "ok": False,
                "error": f"The endpoint has no model named {cfg.model!r}.",
                "models": models[:100] if models else None,
                "model_listed": False,
            }

        word = f"check-{secrets.token_hex(3)}"
        agent_cfg, prompt = _test_config(app, profile, model, word)
        called = False
        printed: str | None = None
        output: Any = None
        started = time.monotonic()
        try:
            async for event in run_agent(agent_cfg, prompt):
                if event["type"] == "tool_call" and event["name"] == "bash":
                    called = True
                elif event["type"] == "tool_result" and event["name"] == "bash":
                    printed = event["text"]
                elif event["type"] == "end":
                    output = event["output"]
        except ModelHTTPError as exc:
            body = str(exc.body or "")[:300]
            if "tool" in body.lower():
                return {**_base(models, listed), "error": f"{NO_TOOLS} ({exc.status_code}: {body})"}
            return {**_base(models, listed), "error": f"HTTP {exc.status_code}: {body}"}
        except UnexpectedModelBehavior as exc:
            # e.g. a worker that keeps answering with text where a structured result is due
            error = str(exc)[:300] if called else f"{TEXT_INSTEAD} ({str(exc)[:200]})"
            return {**_base(models, listed), "error": error}
        except ModelAPIError as exc:
            return {**_base(models, listed), "error": str(exc)[:300]}
        answer = getattr(output, "summary", output)
        text = strip_reasoning(str(answer or ""))[:200]
        if not called:
            return {**_base(models, listed), "error": TEXT_INSTEAD, "reply": text}
        if printed is not None and word in printed:
            reply = f"{word} (printed by bash)"
        else:
            lines = (printed or "").strip().splitlines()
            detail = lines[0][:120] if lines else "no output"
            reply = f"bash was called; the shell could not run it here ({detail})"
        latency = round(time.monotonic() - started, 2)
        return {
            "ok": True,
            "reply": reply,
            "latency_s": latency,
            "models": models[:100] if models else None,
            "model_listed": listed,
            "tool_call": True,
            "structured_output": await _structured(model, cfg, word),
        }
    finally:
        await hub.aclose()


async def _structured(model: Any, cfg: LLMSettings, word: str) -> dict[str, Any]:
    """{ok, text}: one structured answer (`Echo`) asked for the way the helper calls ask."""
    mode = MODES[cfg.structured_output]
    agent: Agent[None, Echo] = Agent(
        model,
        instructions="This is a connection test. Answer with the word you are given.",
        output_type=helper_output(model, Echo),
    )
    try:
        with trace_scope("test"):
            await agent.run(f"The word is {word}.")
    except Exception as exc:
        text = f"structured output failed ({mode}): {type(exc).__name__}: {str(exc)[:300]}"
        if cfg.structured_output == "json_schema":
            text += '. Set "Structured output" to tool_call if this endpoint does not support it.'
        return {"ok": False, "text": text}
    return {"ok": True, "text": f"structured output OK ({mode})"}


def _base(models: list[str] | None, listed: bool | None) -> dict[str, Any]:
    return {"ok": False, "models": models[:100] if models else None, "model_listed": listed}
