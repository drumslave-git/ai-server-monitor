"""The write actions: name, argument schema, which policy governs them, executor.

Each subsystem that can change something (today: MCP tool calls) registers its action here;
`ActionService.request` applies the policy, the per-hour cap, dry-run and the audit log.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..config import Settings
from ..mcp_client import actions as mcp_actions
from . import host_command


class ActionError(Exception):
    """An action cannot be requested or executed (bad arguments, unknown target, ...)."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=200)
    """Human-readable name of what this touches; only used in the audit log."""


@dataclass(frozen=True)
class ActionDef:
    """One kind of write:

        ActionDef(
            name="mcp_call", description=..., args_model=McpCallArgs,
            policy_key=lambda a: f"mcp:{a.server}",          # label stored in the audit row
            target=..., check=lambda settings, args: reason or None,
            policy_of=lambda settings, args: "approve",      # off | suggest | approve | auto
            max_per_hour_of=lambda settings, args: 0,        # 0 = no cap
            uses_dry_run=lambda settings: settings.mcp.dry_run,
            run=lambda service, args: <awaitable dict>,
            chat_tool=False,                                 # the subsystem has its own chat tools
        )

    `run` gets the ActionService (so it can reach `service.mcp` and the like). A raised
    `ActionError` is recorded as `failed`."""

    name: str
    description: str
    args_model: type[_Args]
    policy_key: Callable[[Any], str]
    """The label stored in the audit row; the per-hour cap counts executions per key."""
    target: Callable[[Any], str]
    policy_of: Callable[[Settings, Any], str]
    """The policy for this request: off | suggest | approve | auto."""
    run: Callable[[Any, Any], Awaitable[dict[str, Any]]]
    """`run(service, args)` carries out an allowed request."""
    check: Callable[[Settings, Any], str | None] = lambda settings, args: None
    """Returns a reason when the request cannot be carried out (unknown target, ...)."""
    kind: str = "write"
    max_per_hour_of: Callable[[Settings, Any], int] = lambda settings, args: 0
    """The per-hour cap (0 = no cap)."""
    uses_dry_run: Callable[[Settings], bool] = lambda settings: False
    """Whether the global dry-run switch of this action's subsystem is on."""
    chat_tool: bool = True
    """False = no generic chat tool is made for this action (its subsystem has its own)."""
    summary: Callable[[list[Any]], str] | None = None
    """What an approval batch of these items asks for, from their parsed arguments. Default:
    the target of a single item, else "<name> × N"."""
    background: bool = False
    """True = an approved item runs after the approve call has returned (long commands); its
    outcome arrives as `action.result` and `approval.progress` events."""

    def schema(self) -> dict[str, Any]:
        return self.args_model.model_json_schema()


# --------------------------------------------------------------------------- registry

_ACTIONS = (
    ActionDef(
        name=mcp_actions.ACTION_NAME,
        description=mcp_actions.DESCRIPTION,
        args_model=mcp_actions.McpCallArgs,  # type: ignore[arg-type]
        policy_key=mcp_actions.policy_key,
        target=mcp_actions.target,
        check=lambda settings, args: mcp_actions.check(settings.mcp, args),
        policy_of=lambda settings, args: mcp_actions.policy_of(settings.mcp, args),
        max_per_hour_of=lambda settings, args: 0,
        uses_dry_run=lambda settings: settings.mcp.dry_run,
        run=lambda service, args: mcp_actions.execute(service.mcp, args),
        chat_tool=False,
    ),
    ActionDef(
        name=host_command.ACTION_NAME,
        description=host_command.DESCRIPTION,
        args_model=host_command.HostCommandArgs,  # type: ignore[arg-type]
        policy_key=lambda args: "shell",
        target=host_command.target,
        policy_of=host_command.policy_of,
        run=host_command.execute,
        chat_tool=False,
        background=True,
        summary=host_command.summary,
    ),
)

REGISTRY: dict[str, ActionDef] = {d.name: d for d in _ACTIONS}
