"""The action layer: every *arr / qBittorrent write goes through `ActionService.request`.

Flow for a request:  validate → policy → (limits) → suggested | pending approval | execute.
Every outcome is a row in the permanent `actions` audit table.

Only structured arguments and the configured policy decide anything. Text that came from
logs, *arr data or an LLM can fill in `args`, but it cannot change the policy, skip the
approval or turn off dry-run — those are read from settings here, never from the request.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from ..arr import ArrError, Clients
from ..config import Settings
from ..db import Database
from .registry import REGISTRY, ActionDef, ActionError

log = logging.getLogger(__name__)

Notifier = Callable[[str, dict[str, Any]], Any]
"""Called as notifier(event, record) — event is one of the EVENT_* names. May be async."""

EVENT_SUGGESTED = "action.suggested"
EVENT_PENDING = "approval.pending"
EVENT_EXPIRED = "approval.expired"
EVENT_RESULT = "action.result"  # executed | failed | dry_run | rejected

HOUR = 3600.0
_RAN = ("executed", "failed", "dry_run")


class NotFound(LookupError):
    pass


class Conflict(Exception):
    """The approval is not pending (anymore)."""


def _loads(text: str | None) -> Any:
    return json.loads(text) if text else None


def record_from_row(row: Any) -> dict[str, Any]:
    out = dict(row)
    out["args"] = _loads(out.get("args"))
    out["result"] = _loads(out.get("result"))
    return out


class ActionService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        notifiers: list[Notifier] | None = None,
        registry: dict[str, ActionDef] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        mcp: Any = None,
        api: Any = None,
    ):
        self.db = db
        self.settings = settings
        self.notifiers = notifiers if notifiers is not None else []
        self.registry = registry if registry is not None else REGISTRY
        self._transport = transport
        self._clock = clock
        self.mcp = mcp
        """The MCP manager (None when unused); `ActionDef.run` hooks reach it from here."""
        self.api = api
        """The apiaccess.ApiGateway (for the `api_call` action)."""
        self._clients: Clients | None = None
        self._lock = asyncio.Lock()

    @property
    def clients(self) -> Clients:
        if self._clients is None:
            self._clients = Clients(self.settings.arr, transport=self._transport)
        return self._clients

    async def close(self) -> None:
        if self._clients is not None:
            await self._clients.close()

    # ------------------------------------------------------------------ requests

    async def request(
        self,
        action: str,
        args: dict[str, Any],
        source: str,
        *,
        reason: str | None = None,
        suggest_only: bool = False,
    ) -> dict[str, Any]:
        """Ask for an action. Returns the audit record; its `status` says what happened:
        suggested | pending | rejected | executed | failed | dry_run.

        `suggest_only` caps the outcome at "suggested" whatever the policy says (for things
        an automation only wants to point out, never carry out).

        Raises ActionError for an unknown action or malformed arguments (nothing to audit:
        no valid action was requested)."""
        definition = self.registry.get(action)
        if definition is None:
            raise ActionError(f"unknown action {action!r}")
        try:
            parsed = definition.args_model.model_validate(args)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                for e in exc.errors(include_url=False, include_input=False, include_context=False)
            )
            raise ActionError(f"invalid arguments for {action}: {problems}") from exc
        if not source.strip():
            raise ActionError("source is required")

        key = definition.policy_key(parsed)
        policy = (
            definition.policy_of(self.settings, parsed)
            if definition.policy_of
            else getattr(self.settings.arr.actions, key).policy
        )
        fields: dict[str, Any] = {
            "action": action,
            "target": definition.target(parsed),
            "args": parsed.model_dump(),
            "policy": policy,
            "policy_key": key,
            "source": source,
            "reason": reason,
        }
        if policy == "off":
            return await self._finish(
                fields, "rejected", {"detail": "policy for this action is off"}, "policy"
            )
        problem = definition.check(self.settings, parsed)
        if problem:
            return await self._finish(fields, "rejected", {"detail": problem}, "policy")
        if policy == "suggest" or suggest_only:
            record = await self._insert(fields, "suggested")
            await self._notify(EVENT_SUGGESTED, record)
            return record
        if policy == "approve":
            expires = self._clock() + self.settings.arr.actions.approval_ttl.total_seconds()
            record = await self._insert(fields, "pending", expires_at=expires)
            await self._notify(EVENT_PENDING, record)
            return record
        record = await self._insert(fields, "approved", decided_by="policy:auto")
        return await self._run(record["id"])

    async def decide(self, action_id: int, decision: str, decided_by: str = "ui") -> dict[str, Any]:
        """Approve or reject a pending action. Raises NotFound / Conflict."""
        if decision not in ("approve", "reject"):
            raise ActionError("decision must be 'approve' or 'reject'")
        row = await self.db.fetchone("SELECT * FROM actions WHERE id = ?", (action_id,))
        if row is None:
            raise NotFound(f"no action with id {action_id}")
        now = self._clock()
        if (
            row["status"] == "pending"
            and row["expires_at"] is not None
            and row["expires_at"] <= now
        ):
            await self._sweep_one(action_id)
            raise Conflict("this approval has expired")
        if row["status"] != "pending":
            raise Conflict(f"this action is not pending (it is {row['status']})")
        new_status = "approved" if decision == "approve" else "rejected"
        # The status condition makes concurrent decisions race-safe: only one wins.
        won = await self._update(
            "UPDATE actions SET status = ?, decided_by = ?, updated = ? "
            "WHERE id = ? AND status = 'pending'",
            (new_status, decided_by, now, action_id),
        )
        if not won:
            raise Conflict("this action was decided meanwhile")
        if decision == "reject":
            record = await self.get(action_id)
            await self._notify(EVENT_RESULT, record)
            return record
        return await self._run(action_id)

    async def sweep_expired(self) -> int:
        """Mark pending approvals past their expiry as expired. Returns how many."""
        rows = await self.db.fetchall(
            "SELECT id FROM actions WHERE status = 'pending' AND expires_at <= ?",
            (self._clock(),),
        )
        return sum([await self._sweep_one(row["id"]) for row in rows])

    # ------------------------------------------------------------------ queries

    async def get(self, action_id: int) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM actions WHERE id = ?", (action_id,))
        if row is None:
            raise NotFound(f"no action with id {action_id}")
        return record_from_row(row)

    async def list_actions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        where, params = [], []
        for column, value in (("status", status), ("action", action)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = await self.db.fetchone(f"SELECT COUNT(*) AS n FROM actions {clause}", params)
        rows = await self.db.fetchall(
            f"SELECT * FROM actions {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        return {
            "items": [record_from_row(r) for r in rows],
            "total": total["n"] if total else 0,
            "limit": limit,
            "offset": offset,
        }

    async def pending(self) -> list[dict[str, Any]]:
        await self.sweep_expired()
        rows = await self.db.fetchall("SELECT * FROM actions WHERE status = 'pending' ORDER BY id")
        return [record_from_row(r) for r in rows]

    # ------------------------------------------------------------------ internals

    async def _update(self, sql: str, params: tuple[Any, ...]) -> int:
        """Run an UPDATE and return the number of rows it changed.

        (`Database.execute` returns the connection's last inserted id when that is
        non-zero, which is useless as a row count for UPDATEs.)"""
        cursor = await self.db.conn.execute(sql, params)
        await self.db.conn.commit()
        return cursor.rowcount

    async def _insert(
        self,
        fields: dict[str, Any],
        status: str,
        *,
        result: dict[str, Any] | None = None,
        decided_by: str | None = None,
        expires_at: float | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        action_id = await self.db.execute(
            "INSERT INTO actions (ts, updated, action, target, args, policy, policy_key, status, "
            "source, reason, result, decided_by, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                now,
                fields["action"],
                fields["target"],
                json.dumps(fields["args"]),
                fields["policy"],
                fields["policy_key"],
                status,
                fields["source"],
                fields["reason"],
                json.dumps(result) if result is not None else None,
                decided_by,
                expires_at,
            ),
        )
        return await self.get(action_id)

    async def _finish(
        self, fields: dict[str, Any], status: str, result: dict[str, Any], decided_by: str
    ) -> dict[str, Any]:
        record = await self._insert(fields, status, result=result, decided_by=decided_by)
        await self._notify(EVENT_RESULT, record)
        return record

    async def _set(
        self, action_id: int, status: str, result: dict[str, Any], decided_by: str | None = None
    ) -> dict[str, Any]:
        await self.db.execute(
            "UPDATE actions SET status = ?, result = ?, updated = ?, "
            "decided_by = COALESCE(?, decided_by) WHERE id = ?",
            (status, json.dumps(result), self._clock(), decided_by, action_id),
        )
        record = await self.get(action_id)
        await self._notify(EVENT_RESULT, record)
        return record

    async def _sweep_one(self, action_id: int) -> int:
        changed = await self._update(
            "UPDATE actions SET status = 'expired', decided_by = 'timeout', updated = ? "
            "WHERE id = ? AND status = 'pending'",
            (self._clock(), action_id),
        )
        if changed:
            await self._notify(EVENT_EXPIRED, await self.get(action_id))
        return 1 if changed else 0

    async def _run(self, action_id: int) -> dict[str, Any]:
        """Execute an approved action: limits → dry-run → the real call."""
        record = await self.get(action_id)
        definition = self.registry[record["action"]]
        args = definition.args_model.model_validate(record["args"])
        async with self._lock:
            cap = (
                definition.max_per_hour_of(self.settings, args)
                if definition.max_per_hour_of
                else getattr(self.settings.arr.actions, record["policy_key"]).max_per_hour
            )
            if cap:
                since = self._clock() - HOUR
                row = await self.db.fetchone(
                    "SELECT COUNT(*) AS n FROM actions WHERE policy_key = ? AND updated >= ? "
                    f"AND status IN ({','.join('?' * len(_RAN))}) AND id != ?",
                    [record["policy_key"], since, *_RAN, action_id],
                )
                if row and row["n"] >= cap:
                    return await self._set(
                        action_id,
                        "rejected",
                        {"detail": f"limit reached: {cap} per hour for {record['policy_key']}"},
                    )
            if definition.uses_dry_run(self.settings):
                return await self._set(
                    action_id,
                    "dry_run",
                    {"dry_run": True, "would": f"{record['action']} → {record['target']}"},
                )
            try:
                if definition.run is not None:
                    result = await definition.run(self, args)
                else:
                    assert definition.execute is not None
                    result = await definition.execute(self.clients, args)
            except (ActionError, ArrError) as exc:
                return await self._set(action_id, "failed", {"error": str(exc)})
            except Exception as exc:
                log.exception("action %s #%s crashed", record["action"], action_id)
                return await self._set(action_id, "failed", {"error": f"{type(exc).__name__}"})
            return await self._set(action_id, "executed", result)

    async def _notify(self, event: str, record: dict[str, Any]) -> None:
        for notifier in list(self.notifiers):
            try:
                outcome = notifier(event, record)
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                log.exception("action notifier failed for %s", event)
