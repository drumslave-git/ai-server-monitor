"""The agents' tools: `bash`, `delegate`, and the MCP servers' tools in agent form."""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..llm.text import strip_reasoning
from .output import scrub_output
from .runtime import AgentConfig, AgentTool, ToolError, run_agent
from .safety import Scrubber
from .shell import HostShell

if TYPE_CHECKING:
    from ..chat.tools import Tool, ToolContext
    from ..context import AppContext

Audit = Callable[..., Awaitable[None]]

BASH_DESCRIPTION = (
    "Run a bash script on the server, read-only (it cannot change anything). Returns stdout "
    "and stderr as printed plus the exit code; long output keeps its head and tail."
)
DELEGATE_DESCRIPTION = (
    "Hand one self-contained sub-task to a smaller, faster model that has only `bash` on the "
    "same server. It returns its findings as text. Write the whole task in `task`: it sees "
    "nothing of this conversation and cannot ask back."
)


def bash_tool(
    shell: HostShell,
    *,
    default_timeout_s: float,
    max_timeout_s: float,
    max_chars: int,
    audit: Audit | None = None,
) -> AgentTool:
    async def handler(args: dict[str, Any]) -> str:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ToolError("bash needs `command`: the script to run")
        timeout = args.get("timeout_s") or default_timeout_s
        try:
            timeout = min(max(float(timeout), 1.0), max_timeout_s)
        except (TypeError, ValueError):
            raise ToolError("timeout_s must be a number of seconds") from None
        result = await shell.run(command, timeout_s=timeout, max_chars=max_chars)
        if audit is not None:
            await audit(
                cmd=command[:500],
                exit=result.exit_code,
                seconds=result.duration_s,
                bytes=result.output_bytes,
                stopped=result.stopped,
            )
        if result.stopped == "error":
            raise ToolError(f"bash could not run: {result.output}")
        return scrub_output(result.text())

    return AgentTool(
        name="bash",
        description=BASH_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash script. Pipes, loops, subshells and 2>&1 work.",
                },
                "timeout_s": {
                    "type": "integer",
                    "description": (
                        f"Seconds it may run (default {default_timeout_s:.0f}, "
                        f"at most {max_timeout_s:.0f})."
                    ),
                },
            },
            "required": ["command"],
        },
        handler=handler,
    )


def delegate_tool(run_worker: Callable[[str], Awaitable[str]]) -> AgentTool:
    async def handler(args: dict[str, Any]) -> str:
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ToolError("delegate needs `task`: the whole sub-task in words")
        return await run_worker(task.strip())

    return AgentTool(
        name="delegate",
        description=DELEGATE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The sub-task, complete: what to find out and how to report it.",
                }
            },
            "required": ["task"],
        },
        handler=handler,
    )


def mcp_agent_tools(tools: list[Tool], ctx: ToolContext) -> list[AgentTool]:
    """The MCP servers' chat tools (`mcp_client`) as agent tools. A tool that always writes
    is approval-required: Pydantic AI defers it and the handler files the approval request
    (see `file_for_approval`). The generic `mcp_call` decides per call and files writes
    itself."""

    def wrap(tool: Tool) -> AgentTool:
        async def handler(args: dict[str, Any]) -> Any:
            return await tool.handler(ctx, tool.args_model.model_validate(args))

        definition = tool.definition()["function"]
        return AgentTool(
            name=tool.name,
            description=definition["description"],
            parameters=definition["parameters"],
            handler=handler,
            requires_approval=tool.kind == "write" and tool.kind_for is None,
        )

    return [wrap(t) for t in tools]


async def file_for_approval(tool: AgentTool, args: dict[str, Any]) -> str:
    """`on_approval` of the chat: the tool's own handler files the request with the approval
    service (Approvals page, Telegram buttons) and says what policy decided."""
    result = await tool.handler(args)
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- built for the app


def app_shell(app: AppContext) -> HostShell:
    """The host shell as the app is configured (Settings → General → Docker URL)."""
    docker = app.settings.general.docker_url.strip() or os.environ.get("DOCKER_HOST")
    return HostShell(app.host_env.root, docker_host=docker or None)


def app_bash(app: AppContext, audit_session: str, shell: HostShell | None = None) -> AgentTool:
    """`bash` with the chat's limits; every command goes to the audit trail under
    `audit_session` (a chat session id, or `job:<slug>`)."""
    from ..chat.audit import record_call  # (chat imports this module)

    chat = app.settings.chat

    async def audit(**fields: Any) -> None:
        await record_call(app.db, "shell", audit_session, **fields)

    return bash_tool(
        shell or app_shell(app),
        default_timeout_s=min(
            chat.command_timeout.total_seconds(), chat.tool_timeout.total_seconds()
        ),
        max_timeout_s=chat.tool_timeout.total_seconds(),
        max_chars=max(chat.max_tool_result_chars - 400, 400),
        audit=audit,
    )


def app_delegate(
    app: AppContext,
    audit_session: str,
    scrub: Scrubber,
    host: str,
    shell: HostShell | None = None,
) -> AgentTool:
    """`delegate`: a fresh worker-model agent with only `bash` (traced as `subagent`)."""
    from .prompts import worker_prompt

    settings = app.settings

    async def run_worker(task: str) -> str:
        cfg = AgentConfig(
            model=app.models.model("worker"),
            instructions=worker_prompt(host, settings.general.language),
            tools=[app_bash(app, audit_session, shell)],
            max_steps=settings.chat.worker_max_steps,
            tool_timeout_s=settings.chat.tool_timeout.total_seconds(),
            result_chars=settings.chat.max_tool_result_chars,
            scrub=scrub,
            tracing=app.tracing,
            trace={"purpose": "subagent"},
            trace_tool_prefix="worker:",
        )
        output, steps, stopped = "", 0, None
        async for event in run_agent(cfg, task):
            if event["type"] == "end":
                output, steps, stopped = event["output"], event["steps"], event["stopped"]
        text = strip_reasoning(str(output)) or "(the worker gave no answer)"
        limit = ", stopped at its step limit" if stopped else ""
        return f"{text}\n[worker model {app.models.name('worker')}: {steps} steps{limit}]"

    return delegate_tool(run_worker)
