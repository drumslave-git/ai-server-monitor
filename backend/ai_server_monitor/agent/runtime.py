"""The one agent loop: chat, `delegate` and scheduled jobs all call `run_agent`.

The loop itself is Pydantic AI's. This module only adds what the monitor needs around it:
- plain event dicts for the caller (token, tool_call, tool_progress, tool_result, end), with
  `tool_progress` every few seconds while a tool runs (keeps the chat stream alive);
- every tool call recorded in `llm_tool_calls` (LLM calls are recorded by the tracing
  transport under the HTTP client, see `models.py`), scrubbed of secrets, size-capped and
  marked as untrusted data for the model;
- a step limit after which the model gets no tools and is told to answer with what it has;
- tools that need approval end the run as deferred calls (Pydantic AI's approval mechanism);
  `on_approval` files each one with the approval service and its message goes back to the
  model, which then continues in the same run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from pydantic_ai import (
    Agent,
    ApprovalRequired,
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    Tool,
    ToolApproved,
    ToolDenied,
    UsageLimits,
)
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RunUsage

from ..tracing import trace_scope
from ..tracing.scope import current_scope
from .prompts import STEP_LIMIT_NOTE
from .safety import Scrubber, cap_text, shrink, wrap_untrusted

log = logging.getLogger(__name__)

PROGRESS_EVERY_S = 5.0
PREVIEW = 400
MAX_APPROVAL_ROUNDS = 5
RETRIES = 3
"""Retries of a tool call or a final result before the model is given up on (Pydantic AI)."""
NOT_FINISHED = "The model did not finish within {steps} steps (the limit is {limit})."
Event = dict[str, Any]


class ToolError(Exception):
    """The tool could not do what was asked; the message goes back to the model."""


@dataclass
class AgentTool:
    """A tool in our terms: a JSON-schema signature and an async handler taking the argument
    dict. What it returns (text or JSON-able data) is what the model reads."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[Any]]
    requires_approval: bool = False
    """Pydantic AI defers the call instead of running it; `on_approval` takes over."""
    approval_for: Callable[[dict[str, Any]], bool] | None = None
    """Per call: True = defer this call like `requires_approval` (the generic `mcp_call`,
    which writes only for some of the tools it reaches)."""


@dataclass
class PendingApproval:
    """What `on_approval` returns for a call that waits for a person: the run stops there and
    is resumed (`resume_results`) when the approval is decided."""

    action_ids: list[int]
    message: str
    """What the user is told meanwhile (and the tool result shown for the call)."""


@dataclass
class Suspended:
    """The `output` of a run that stopped at calls waiting for approval."""

    messages: list[ModelMessage]
    resolved: dict[str, str]
    """Call id -> result text of the deferred calls that were settled at once."""
    pending: dict[str, PendingApproval]
    names: dict[str, str]
    """Call id -> tool name of every deferred call."""

    def to_json(self) -> dict[str, Any]:
        return {
            "messages": ModelMessagesTypeAdapter.dump_python(self.messages, mode="json"),
            "resolved": self.resolved,
            "pending": {
                cid: {"action_ids": p.action_ids, "message": p.message}
                for cid, p in self.pending.items()
            },
            "names": self.names,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Suspended:
        return cls(
            messages=ModelMessagesTypeAdapter.validate_python(data["messages"]),
            resolved=dict(data["resolved"]),
            pending={cid: PendingApproval(**p) for cid, p in data["pending"].items()},
            names=dict(data["names"]),
        )


def resume_results(
    suspended: Suspended, settled: dict[str, tuple[bool, str]]
) -> tuple[DeferredToolResults, dict[str, str]]:
    """The deferred results to resume `suspended` with: `settled` maps each pending call id to
    (ran, text): ran = the model gets `text` as the tool's result, else as the reason it was
    not run. Returns the results and the `AgentConfig.resolved` texts."""
    results = DeferredToolResults()
    resolved = dict(suspended.resolved)
    for call_id in suspended.resolved:
        results.approvals[call_id] = ToolApproved()
    for call_id, (ran, text) in settled.items():
        if ran:
            resolved[call_id] = text
            results.approvals[call_id] = ToolApproved()
        else:
            results.approvals[call_id] = ToolDenied(text)
    return results, resolved


@dataclass
class AgentConfig:
    model: Model
    instructions: str
    tools: Sequence[AgentTool] = ()
    model_settings: ModelSettings | None = None
    max_steps: int = 15
    """Model requests that may call tools; one more without tools is allowed for the answer."""
    tool_timeout_s: float = 3600.0
    result_chars: int = 6000
    scrub: Scrubber | None = None
    tracing: Any = None
    """`tracing.Tracing`: tool calls go to `llm_tool_calls`."""
    trace: dict[str, Any] = field(default_factory=dict)
    """`trace_scope` arguments for this run (purpose, link_kind, link_id, turn)."""
    trace_tool_prefix: str = ""
    """Prefix of the tool names in the trace (sub-agent calls: 'worker:')."""
    output_type: Any = str
    output_validator: Callable[[Any], Any] | None = None
    """Checks a structured result; raising `ModelRetry` sends it back to the model once more."""
    on_approval: Callable[[AgentTool, dict[str, Any]], Awaitable[str | PendingApproval]] | None = (
        None
    )
    """Files an approval-required call: returns the result text when it was settled at once
    (policy auto, rejected, ...), or PendingApproval when a person has to decide."""
    resolved: dict[str, str] = field(default_factory=dict)
    """Call id -> result of an approval-required call that already ran (settled at once, or
    approved and executed before a resume): the tool returns it instead of running."""


@dataclass
class _ToolOutcome:
    ok: bool
    truncated: bool
    text: str


def history_messages(history: Sequence[dict[str, str]]) -> list[ModelMessage]:
    """Earlier user/assistant texts of a conversation as Pydantic AI messages."""
    out: list[ModelMessage] = []
    for item in history:
        if item.get("role") == "user":
            out.append(ModelRequest(parts=[UserPromptPart(content=item["content"])]))
        elif item.get("role") == "assistant":
            out.append(ModelResponse(parts=[TextPart(content=item["content"])]))
    return out


def _args_dict(part: Any) -> dict[str, Any]:
    try:
        args = part.args_as_dict()
    except (ValueError, TypeError):
        return {"_raw": str(part.args)[:2000]}
    return args if isinstance(args, dict) else {"_raw": args}


class _Run:
    """State of one `run_agent` call."""

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.steps = 0
        self.stopped: str | None = None
        self.outcomes: dict[str, _ToolOutcome] = {}
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.by_name = {t.name: t for t in cfg.tools}
        self.pending: dict[str, PendingApproval] = {}
        self.round_resolved: dict[str, str] = {}

    # ------------------------------------------------------------------ tools

    def _shape(self, name: str, value: Any) -> tuple[str, bool]:
        scrub = self.cfg.scrub
        if isinstance(value, str):
            text = scrub.text(value) if scrub else value
            return cap_text(text, self.cfg.result_chars)
        cleaned = scrub.clean(value) if scrub else value
        return shrink(cleaned, self.cfg.result_chars)

    async def execute(self, tool: AgentTool, call_id: str, args: dict[str, Any]) -> str:
        """Run one tool call; never raises (every failure is text the model can react to)."""
        timer = None
        if self.cfg.tracing is not None:
            timer = self.cfg.tracing.start_tool(self.cfg.trace_tool_prefix + tool.name, args)
        ok, truncated = True, False
        if call_id in self.cfg.resolved:  # approved and run already (by the approval service)
            text = self.cfg.resolved[call_id]
            if timer is not None:
                timer.finish(ok=True, result=text, result_size=len(text))
            self.outcomes[call_id] = _ToolOutcome(True, False, text)
            return wrap_untrusted(tool.name, text)
        try:
            async with asyncio.timeout(self.cfg.tool_timeout_s):
                result = await tool.handler(args)
            text, truncated = self._shape(tool.name, result)
        except ToolError as exc:
            ok, text = False, self._shape(tool.name, str(exc))[0]
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}"
                for e in exc.errors(include_url=False, include_input=False, include_context=False)
            )
            ok, text = False, f"invalid arguments for {tool.name}: {details}"
        except TimeoutError:
            ok, text = False, f"{tool.name} timed out after {self.cfg.tool_timeout_s:.0f}s"
        except asyncio.CancelledError:
            if timer is not None:
                timer.finish(ok=False, error="cancelled")
            raise
        except Exception as exc:
            log.exception("agent tool %s crashed", tool.name)
            ok, text = False, f"{tool.name} failed: {type(exc).__name__}: {exc}"
        if timer is not None:
            timer.finish(ok=ok, result=text, error=None if ok else text, result_size=len(text))
        self.outcomes[call_id] = _ToolOutcome(ok, truncated, text)
        return wrap_untrusted(tool.name, text)

    def pydantic_tool(self, tool: AgentTool) -> Tool[Any]:
        async def call(ctx: RunContext[Any], **kwargs: Any) -> str:
            if (
                tool.approval_for is not None
                and not ctx.tool_call_approved
                and tool.approval_for(kwargs)
            ):
                raise ApprovalRequired()
            return await self.execute(tool, ctx.tool_call_id or "", kwargs)

        out = Tool.from_schema(
            call,
            name=tool.name,
            description=tool.description,
            json_schema=tool.parameters,
            takes_ctx=True,
        )
        out.requires_approval = tool.requires_approval
        return out

    # ------------------------------------------------------------------ step limit

    async def prepare(
        self, ctx: RunContext[Any], defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        """Called before every model request: counts steps, hides the tools past the limit."""
        self.steps = ctx.run_step
        scope = current_scope()
        if scope is not None:
            scope.step = ctx.run_step
        if ctx.run_step > self.cfg.max_steps and defs:
            self.stopped = "step_limit"
            return []
        return defs

    def not_finished(self) -> str:
        """The ending of a run out of steps: the steps it really took, and the limit."""
        return NOT_FINISHED.format(steps=self.steps or self.cfg.max_steps, limit=self.cfg.max_steps)

    def limit_note(self, ctx: RunContext[Any]) -> str:
        return STEP_LIMIT_NOTE if ctx.run_step > self.cfg.max_steps else ""

    # ------------------------------------------------------------------ the run

    async def pump(
        self,
        prompt: str | None,
        history: list[ModelMessage] | None,
        deferred: DeferredToolResults | None = None,
    ) -> None:
        cfg = self.cfg
        agent: Agent[None, Any] = Agent(
            cfg.model,
            instructions=[cfg.instructions, self.limit_note],
            tools=[self.pydantic_tool(t) for t in cfg.tools],
            output_type=[cfg.output_type, DeferredToolRequests]
            if any(t.requires_approval or t.approval_for for t in cfg.tools)
            else cfg.output_type,
            model_settings=cfg.model_settings,
            capabilities=[PrepareTools(self.prepare)],
            # A small model at the step limit may still call a tool that is gone or give a
            # malformed result a few times before it answers: retry instead of crashing.
            retries={"tools": RETRIES, "output": RETRIES},
            end_strategy="early",  # a final result ends the run; calls beside it are dropped
        )
        if cfg.output_validator is not None:
            check = cfg.output_validator

            def validate(output: Any) -> Any:
                return output if isinstance(output, DeferredToolRequests) else check(output)

            agent.output_validator(validate)
        limits = UsageLimits(request_limit=cfg.max_steps + RETRIES + 2)
        messages = history
        # Filled in place by Pydantic AI: a run that ends in an exception (the step limit, too
        # many invalid results) still reports what it used.
        usage = RunUsage()
        try:
            with trace_scope(**cfg.trace):
                for _ in range(MAX_APPROVAL_ROUNDS + 1):
                    result = None
                    async with agent.run_stream_events(
                        prompt if deferred is None else None,
                        message_history=messages,
                        deferred_tool_results=deferred,
                        usage_limits=limits,
                        usage=usage,
                    ) as stream:
                        async for event in stream:
                            if isinstance(event, AgentRunResultEvent):
                                result = event.result
                            else:
                                await self.queue.put(("event", event))
                    assert result is not None
                    usage = result.usage
                    if not isinstance(result.output, DeferredToolRequests):
                        await self.queue.put(("end", (result.output, usage)))
                        return
                    deferred = await self.approve(result.output)
                    messages = result.all_messages()
                    if self.pending:
                        suspended = Suspended(
                            messages=messages,
                            resolved=self.round_resolved,
                            pending=self.pending,
                            names={p.tool_call_id: p.tool_name for p in result.output.approvals},
                        )
                        await self.queue.put(("end", (suspended, usage)))
                        return
                await self.queue.put(("end", ("(stopped: too many approval rounds)", usage)))
        except UsageLimitExceeded:
            self.stopped = "step_limit"
            await self.queue.put(("end", (self.not_finished(), usage)))
        except UnexpectedModelBehavior as exc:
            if self.stopped != "step_limit":
                await self.queue.put(("error", exc))
                return
            # Out of steps and still no valid answer after the retries: a plain ending.
            log.info("agent did not finish at the step limit: %s", exc)
            await self.queue.put(("end", (self.not_finished(), usage)))
        except BaseException as exc:  # handed to the consumer, which re-raises it
            await self.queue.put(("error", exc))

    async def approve(self, requests: DeferredToolRequests) -> DeferredToolResults:
        """File each approval-required call. Settled at once: the model reads what happened.
        Waiting for a person: collected in `self.pending` (the run stops after this round)."""
        results = DeferredToolResults()
        self.pending = {}
        self.round_resolved = {}
        for part in requests.approvals:
            tool = self.by_name.get(part.tool_name)
            args = _args_dict(part)
            call_id = part.tool_call_id
            if tool is None or self.cfg.on_approval is None:
                message = f"{part.tool_name} cannot run here: nothing can approve it"
                self.outcomes[call_id] = _ToolOutcome(False, False, message)
            else:
                timer = None
                if self.cfg.tracing is not None:
                    timer = self.cfg.tracing.start_tool(
                        self.cfg.trace_tool_prefix + tool.name, args
                    )
                outcome: str | PendingApproval
                try:
                    outcome = await self.cfg.on_approval(tool, args)
                    ok = True
                except Exception as exc:
                    outcome, ok = f"{tool.name} could not be filed for approval: {exc}", False
                message = outcome.message if isinstance(outcome, PendingApproval) else outcome
                if timer is not None:
                    timer.finish(ok=ok, result=message, error=None if ok else message)
                self.outcomes[call_id] = _ToolOutcome(ok, False, message)
                if isinstance(outcome, PendingApproval):
                    self.pending[call_id] = outcome
                elif ok:
                    self.cfg.resolved[call_id] = message
                    self.round_resolved[call_id] = message
            await self.queue.put(("approval", (call_id, part.tool_name)))
            if call_id not in self.pending:
                approved = call_id in self.cfg.resolved
                results.approvals[call_id] = ToolApproved() if approved else ToolDenied(message)
        return results


async def run_agent(
    cfg: AgentConfig,
    prompt: str | None,
    *,
    history: list[ModelMessage] | None = None,
    deferred: DeferredToolResults | None = None,
) -> AsyncIterator[Event]:
    """Run one agent to its answer, yielding events. The last event is
    `{"type": "end", "output", "usage", "steps", "stopped"}`; errors of the model are raised.
    `output` is a `Suspended` when calls wait for approval: resume with its messages as
    `history`, `deferred` from `resume_results` and those texts as `cfg.resolved`.
    Closing the iterator early cancels the run (and the tool that is running)."""
    run = _Run(cfg)
    task = asyncio.create_task(run.pump(prompt, history, deferred))
    running: dict[str, tuple[str, float]] = {}
    reported: set[str] = set()
    scrub = cfg.scrub

    def result_event(call_id: str, name: str, fallback: str, ok: bool) -> Event:
        outcome = run.outcomes.get(call_id) or _ToolOutcome(ok, False, fallback)
        reported.add(call_id)
        running.pop(call_id, None)
        return {
            "type": "tool_result",
            "id": call_id,
            "name": name,
            "ok": outcome.ok,
            "truncated": outcome.truncated,
            "size": len(outcome.text),
            "preview": outcome.text[:PREVIEW],
            "text": outcome.text,
            "step": run.steps,
        }

    try:
        while True:
            try:
                kind, item = await asyncio.wait_for(
                    run.queue.get(), timeout=PROGRESS_EVERY_S if running else None
                )
            except TimeoutError:
                now = time.monotonic()
                for call_id, (name, started) in list(running.items()):
                    yield {
                        "type": "tool_progress",
                        "id": call_id,
                        "name": name,
                        "elapsed_s": int(now - started),
                    }
                continue
            if kind == "error":
                raise item
            if kind == "end":
                output, usage = item
                yield {
                    "type": "end",
                    "output": output,
                    "steps": run.steps,
                    "stopped": run.stopped,
                    "usage": {
                        "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
                        "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
                        "requests": getattr(usage, "requests", 0) or 0,
                    },
                }
                return
            if kind == "approval":
                call_id, name = item
                yield result_event(call_id, name, "", True)
                continue
            event = item
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                if event.part.content:
                    yield {"type": "token", "text": event.part.content, "step": run.steps}
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                if event.delta.content_delta:
                    yield {"type": "token", "text": event.delta.content_delta, "step": run.steps}
            elif isinstance(event, FunctionToolCallEvent):
                part = event.part
                args = _args_dict(part)
                if part.tool_call_id not in reported:
                    running[part.tool_call_id] = (part.tool_name, time.monotonic())
                yield {
                    "type": "tool_call",
                    "id": part.tool_call_id,
                    "name": part.tool_name,
                    "args": scrub.clean(args) if scrub else args,
                    "raw_args": args,
                    "step": run.steps,
                }
            elif isinstance(event, FunctionToolResultEvent):
                part = event.part
                if part.tool_call_id in reported:
                    continue  # an approval-required call: reported when it was filed
                if isinstance(part, RetryPromptPart):
                    text = part.model_response()
                    yield result_event(part.tool_call_id, part.tool_name or "?", text, False)
                elif isinstance(part, ToolReturnPart):
                    content = part.content
                    text = content if isinstance(content, str) else json.dumps(content, default=str)
                    yield result_event(part.tool_call_id, part.tool_name, text, True)
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(BaseException):
            await task
