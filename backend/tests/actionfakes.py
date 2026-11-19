"""A fake write action: the vehicle for testing the generic approval mechanism.

`FakeAction` builds an `ActionDef` whose policy, hourly cap and dry-run switch are plain
attributes the test sets, and whose `run` records its calls instead of changing anything. Nothing
here touches the network or the host.

    fake = FakeAction()
    svc = ActionService(db, settings, registry=fake.registry())     # direct service tests
    # app-level tests (Telegram, web API, chat) use the `fake_action` fixture (conftest.py), which
    # registers the definition in the shared `REGISTRY`.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ai_server_monitor.actions.registry import ActionDef, ActionError, _Args
from ai_server_monitor.config import Settings

NAME = "fake_write"


class FakeArgs(_Args):
    target: str = Field(min_length=1, max_length=100)
    """What the write touches."""
    note: str = Field(default="", max_length=200)


def settings_with(ttl: str = "1h") -> Settings:
    """Settings whose approval timeout is `ttl` (the policy comes from the FakeAction)."""
    return Settings.model_validate({"approvals": {"approval_ttl": ttl}})


class FakeAction:
    def __init__(
        self,
        *,
        policy: str = "approve",
        cap: int = 0,
        dry_run: bool = False,
        chat_tool: bool = True,
    ) -> None:
        self.policy = policy
        """off | suggest | approve | auto: what `policy_of` answers."""
        self.cap = cap
        """The hourly cap (0 = none)."""
        self.dry_run = dry_run
        self.chat_tool = chat_tool
        self.kinds = False
        """True = the action has kinds (`ActionDef.covers`): the `note` of an item."""
        self.calls: list[str] = []
        """The `target` of every real execution, in order."""
        self.fail_targets: set[str] = set()
        """Targets whose run raises an ActionError (recorded as `failed`)."""
        self.crash_targets: set[str] = set()
        """Targets whose run raises an unexpected exception (recorded as `failed`)."""
        self.problem_targets: set[str] = set()
        """Targets `check` refuses (recorded as `rejected`)."""

    async def _run(self, service: Any, args: FakeArgs) -> dict[str, Any]:
        if args.target in self.fail_targets:
            raise ActionError(f"fake failure for {args.target}")
        if args.target in self.crash_targets:
            raise RuntimeError("boom")
        self.calls.append(args.target)
        return {"target": args.target, "done": True}

    def definition(self) -> ActionDef:
        return ActionDef(
            name=NAME,
            description="Fake write used by the tests. Changes nothing.",
            args_model=FakeArgs,  # type: ignore[arg-type]
            policy_key=lambda args: NAME,
            target=lambda args: f"fake: {args.target}",
            check=lambda settings, args: (
                f"{args.target} is not allowed" if args.target in self.problem_targets else None
            ),
            policy_of=lambda settings, args: self.policy,
            max_per_hour_of=lambda settings, args: self.cap,
            uses_dry_run=lambda settings: self.dry_run,
            run=self._run,
            chat_tool=self.chat_tool,
            covers=(lambda args: f"fake {args.note}") if self.kinds else None,
        )

    def registry(self) -> dict[str, ActionDef]:
        return {NAME: self.definition()}
