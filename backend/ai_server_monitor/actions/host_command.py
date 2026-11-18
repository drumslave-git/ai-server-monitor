"""The `host_command` action: one changing shell command on the host, proposed by the chat's
`bash_write` tool. The audit row keeps the command (args), the model's reason (reason) and
the exit code with a capped output (result)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ACTION_NAME = "host_command"
DESCRIPTION = "Run a shell command on the host that changes something (as root)."
OUTPUT_CHARS = 4000
"""Output kept in the permanent audit log (head and tail)."""


class HostCommandArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    command: str = Field(min_length=1, max_length=4000)
    reason: str = Field(default="", max_length=500)


def policy_of(settings: Any, args: HostCommandArgs) -> str:
    """Settings → Approvals → changing shell commands: off | approve | auto."""
    return str(settings.approvals.shell_changes)


def target(args: HostCommandArgs) -> str:
    command = " ".join(args.command.split())
    return command if len(command) <= 120 else command[:119] + "…"


def summary(items: list[HostCommandArgs]) -> str:
    """The approval's title: short and fixed (the command itself is shown with each item)."""
    return "command on the host" if len(items) == 1 else f"{len(items)} commands on the host"


def check(service: Any) -> str | None:
    shell = getattr(service, "change_shell", None)
    if shell is None or not shell.status.available:
        return (shell.status.reason if shell is not None else None) or "no change shell"
    return None


async def execute(service: Any, args: HostCommandArgs) -> dict[str, Any]:
    from .registry import ActionError

    if reason := check(service):
        raise ActionError(f"cannot run commands on the host: {reason}")
    timeout = service.settings.chat.command_timeout.total_seconds()
    result = await service.change_shell.run(args.command, timeout_s=timeout, max_chars=OUTPUT_CHARS)
    if result.stopped == "error":
        raise ActionError(result.output)
    return {
        "exit_code": result.exit_code,
        "output": result.output,
        "duration_s": result.duration_s,
        "stopped": result.stopped,
        "truncated": result.truncated,
    }
