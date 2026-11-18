"""The Settings "Test" button of the Main and Worker model sections: is the endpoint reachable,
does it have the model, and can the model call a tool (one native tool-call round trip)?

A model without native tool calling cannot run this product, so that is said plainly."""

from __future__ import annotations

import secrets
import time
from typing import Any

import httpx
import openai
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.usage import UsageLimits

from ..agent.models import ModelHub
from ..config import LLMSettings, Settings
from ..tracing import trace_scope
from .text import strip_reasoning

CHECK_TOOL = "get_check_word"
NO_TOOLS = (
    "The model answered without calling the tool: it cannot call tools (native OpenAI tool "
    "calling), so this app cannot use it. Pick a model with tool support."
)


def _instructions(word_tool: str) -> str:
    return (
        f"This is a connection test. Call the tool `{word_tool}` exactly once, then reply with "
        "only the word it returned."
    )


async def check_model(
    cfg: LLMSettings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    tracing: Any = None,
) -> dict[str, Any]:
    """{ok, reply, latency_s, models, model_listed, tool_call} or {ok: False, error, ...}."""
    hub = ModelHub(
        Settings(llm=cfg.model_copy(update={"max_retries": 0})),
        tracing=tracing,
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
        agent: Agent[None, str] = Agent(model, instructions=_instructions(CHECK_TOOL))

        @agent.tool_plain(name=CHECK_TOOL)
        def get_check_word() -> str:
            """Returns the word to reply with."""
            return word

        started = time.monotonic()
        try:
            with trace_scope("test"):
                result = await agent.run(
                    "Run the test.",
                    model_settings=OpenAIChatModelSettings(
                        temperature=0, max_tokens=1024, openai_reasoning_effort="none"
                    ),
                    usage_limits=UsageLimits(request_limit=3),
                )
        except ModelHTTPError as exc:
            body = str(exc.body or "")[:300]
            if "tool" in body.lower():
                return {**_base(models, listed), "error": f"{NO_TOOLS} ({exc.status_code}: {body})"}
            return {**_base(models, listed), "error": f"HTTP {exc.status_code}: {body}"}
        except (ModelAPIError, UnexpectedModelBehavior) as exc:
            return {**_base(models, listed), "error": str(exc)[:300]}
        called = any(
            isinstance(part, ToolCallPart) and part.tool_name == CHECK_TOOL
            for message in result.all_messages()
            for part in getattr(message, "parts", ())
        )
        reply = strip_reasoning(str(result.output))[:200]
        if not called:
            return {**_base(models, listed), "error": NO_TOOLS, "reply": reply}
        return {
            "ok": True,
            "reply": reply,
            "latency_s": round(time.monotonic() - started, 2),
            "models": models[:100] if models else None,
            "model_listed": listed,
            "tool_call": True,
        }
    finally:
        await hub.aclose()


def _base(models: list[str] | None, listed: bool | None) -> dict[str, Any]:
    return {"ok": False, "models": models[:100] if models else None, "model_listed": listed}
