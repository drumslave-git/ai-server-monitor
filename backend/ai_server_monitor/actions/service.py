"""The action layer: every write (today: MCP tool calls) goes through `ActionService.request`.

Flow for a request:  validate → policy → suggested | pending approval | execute (limits are
checked when an item is executed). Every outcome is a row in the permanent `actions` audit table.

Approvals are *batches*: the pending items of one action type from one run (an automation
run, or one chat turn) share an `approval_batches` row, so the user gets one approval — one
card, one Telegram message — for them, and can approve or reject all of it or a subset. Each
item stays its own audit row (`actions.batch_id`) with its own status. A single request is
just a batch of one.

Only structured arguments and the configured policy decide anything. Text that came from
logs, service data or an LLM can fill in `args`, but it cannot change the policy, skip the
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

from pydantic import ValidationError

from ..config import Settings
from ..db import Database
from .registry import REGISTRY, ActionDef, ActionError

log = logging.getLogger(__name__)

Notifier = Callable[[str, dict[str, Any]], Any]
"""Called as notifier(event, record) — event is one of the EVENT_* names. May be async."""

# Item events: the record is one audit row.
EVENT_SUGGESTED = "action.suggested"
EVENT_RESULT = "action.result"  # executed | failed | dry_run | rejected | deferred | expired
# Batch events: the record is a batch view (see `ActionService.get_batch`).
EVENT_PENDING = "approval.pending"  # a batch was opened
EVENT_UPDATED = "approval.updated"  # more items were added to an open batch
EVENT_DECIDED = "approval.decided"  # all or some items were approved / rejected
EVENT_EXPIRED = "approval.expired"  # the pending items of a batch timed out
EVENT_PROGRESS = "approval.progress"  # queued (approved) items of a batch were executed
BATCH_EVENTS = (EVENT_PENDING, EVENT_UPDATED, EVENT_DECIDED, EVENT_EXPIRED, EVENT_PROGRESS)

HOUR = 3600.0
_RAN = ("executed", "failed", "dry_run")
_OUTCOME_ORDER = (
    "executed",
    "dry_run",
    "failed",
    "queued",
    "deferred",
    "rejected",
    "expired",
    "approved",
    "pending",
    "suggested",
)


def outcome_text(counts: dict[str, int]) -> str:
    """ "37 executed, 2 failed, 1 deferred" (empty when there is nothing to count)."""
    names = [*_OUTCOME_ORDER, *sorted(set(counts) - set(_OUTCOME_ORDER))]
    return ", ".join(f"{counts[n]} {n.replace('_', ' ')}" for n in names if counts.get(n))


def queue_note(queued: int, next_in_s: float | None) -> str:
    """ ", next in ~12 min" for the batch summary while approved items wait for the hourly cap."""
    if not queued:
        return ""
    if not next_in_s or next_in_s < 60:
        return ", next in ~1 min"
    return f", next in ~{round(next_in_s / 60)} min"


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
        clock: Callable[[], float] = time.time,
        mcp: Any = None,
    ):
        self.db = db
        self.settings = settings
        self.notifiers = notifiers if notifiers is not None else []
        self.registry = registry if registry is not None else REGISTRY
        self._clock = clock
        self.mcp = mcp
        """The MCP manager (None when unused); `ActionDef.run` hooks reach it from here."""
        self._lock = asyncio.Lock()
        self._batch_lock = asyncio.Lock()

    # ------------------------------------------------------------------ requests

    async def request(
        self,
        action: str,
        args: dict[str, Any],
        source: str,
        *,
        reason: str | None = None,
        suggest_only: bool = False,
        run_key: str | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Ask for one action. Returns its audit record; its `status` says what happened:
        suggested | pending | rejected | executed | failed | dry_run | deferred.

        Needing approval, the item joins the open batch of the same action, source and
        `run_key` (a chat turn, ...); without a `run_key` it is a batch of one.
        `suggest_only` caps the outcome at "suggested" whatever the policy says (for things
        an automation only wants to point out, never carry out).

        Raises ActionError for an unknown action or malformed arguments (nothing to audit:
        no valid action was requested)."""
        records = await self.request_many(
            action,
            [(args, reason)],
            source,
            suggest_only=suggest_only,
            run_key=run_key,
            label=label,
        )
        return records[0]

    async def request_many(
        self,
        action: str,
        items: list[tuple[dict[str, Any], str | None]],
        source: str,
        *,
        suggest_only: bool = False,
        run_key: str | None = None,
        label: str | None = None,
    ) -> list[dict[str, Any]]:
        """Ask for several items of one action as one run: those that need approval go into
        one batch, announced once (the notifiers see one `approval.pending`, not one per
        item). `items` are (arguments, reason) pairs; the records come back in the same order.
        Nothing is audited when any item is malformed (ActionError)."""
        definition = self.registry.get(action)
        if definition is None:
            raise ActionError(f"unknown action {action!r}")
        if not source.strip():
            raise ActionError("source is required")
        parsed_items = [(self._parse(definition, action, args), reason) for args, reason in items]
        records: list[dict[str, Any]] = []
        touched: dict[int, list[Any]] = {}  # batch id -> [created by this call, items added]
        for parsed, reason in parsed_items:
            key = definition.policy_key(parsed)
            policy = definition.policy_of(self.settings, parsed)
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
                records.append(
                    await self._finish(
                        fields, "rejected", {"detail": "policy for this action is off"}, "policy"
                    )
                )
                continue
            problem = definition.check(self.settings, parsed)
            if problem:
                records.append(
                    await self._finish(fields, "rejected", {"detail": problem}, "policy")
                )
                continue
            if policy == "suggest" or suggest_only:
                record = await self._insert(fields, "suggested")
                await self._notify(EVENT_SUGGESTED, record)
                records.append(record)
            elif policy == "approve":
                batch_id, created = await self._batch_for(action, source, run_key, label)
                entry = touched.setdefault(batch_id, [False, 0])
                entry[0] = entry[0] or created
                entry[1] += 1
                expires = (await self._batch_row(batch_id))["expires_at"]
                records.append(
                    await self._insert(fields, "pending", expires_at=expires, batch_id=batch_id)
                )
            else:
                record = await self._insert(fields, "approved", decided_by="policy:auto")
                records.append(await self._run(record["id"]))
        for batch_id, (created, added) in touched.items():
            view = await self.get_batch(batch_id)
            view["added"] = added
            await self._notify(EVENT_PENDING if created else EVENT_UPDATED, view)
        return records

    def _parse(self, definition: ActionDef, action: str, args: dict[str, Any]) -> Any:
        try:
            return definition.args_model.model_validate(args)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                for e in exc.errors(include_url=False, include_input=False, include_context=False)
            )
            raise ActionError(f"invalid arguments for {action}: {problems}") from exc

    async def decide_batch(
        self,
        batch_id: int,
        decision: str,
        items: list[int] | None = None,
        decided_by: str = "ui",
    ) -> dict[str, Any]:
        """Approve (→ executes the items now) or reject the pending items of a batch: all of
        them, or only the item ids in `items`. Returns the batch view plus `decided` (how many
        items this call decided) and `skipped` (selected items that were no longer pending).
        Raises NotFound / Conflict / ActionError."""
        if decision not in ("approve", "reject"):
            raise ActionError("decision must be 'approve' or 'reject'")
        if items is not None and not items:
            raise ActionError("no items selected")
        await self._batch_row(batch_id)  # NotFound
        if await self._expire_batch(batch_id):
            raise Conflict("this approval has expired")
        rows = await self.db.fetchall(
            "SELECT id, status FROM actions WHERE batch_id = ?", (batch_id,)
        )
        status_of = {int(r["id"]): r["status"] for r in rows}
        # Rejecting also cancels approved items that are still queued for the hourly cap.
        open_states = ("pending",) if decision == "approve" else ("pending", "queued")
        if items is None:
            wanted = [i for i, st in status_of.items() if st in open_states]
            skipped: list[int] = []
        else:
            unknown = [i for i in items if i not in status_of]
            if unknown:
                raise NotFound(f"item {unknown[0]} does not belong to approval {batch_id}")
            wanted = [i for i in dict.fromkeys(items) if status_of[i] in open_states]
            skipped = [i for i in dict.fromkeys(items) if status_of[i] not in open_states]
        if not wanted:
            raise Conflict("nothing is pending here (it was decided meanwhile)")
        new_status = "approved" if decision == "approve" else "rejected"
        won: list[int] = []
        for item_id in sorted(wanted):
            # The status condition makes concurrent decisions race-safe: only one wins.
            changed = await self._update(
                "UPDATE actions SET status = ?, decided_by = ?, updated = ? "
                f"WHERE id = ? AND status IN ({','.join('?' * len(open_states))})",
                (new_status, decided_by, self._clock(), item_id, *open_states),
            )
            if changed:
                won.append(item_id)
        if not won:
            raise Conflict("this approval was decided meanwhile")
        for item_id in won:
            if decision == "reject":
                await self._notify(EVENT_RESULT, await self.get(item_id))
            else:
                await self._run(item_id)
        view = await self.get_batch(batch_id)
        view["decision"] = decision
        view["decided_by"] = decided_by
        view["decided"] = len(won)
        view["skipped"] = len(skipped) + len(wanted) - len(won)
        await self._notify(EVENT_DECIDED, view)
        return view

    async def decide(self, action_id: int, decision: str, decided_by: str = "ui") -> dict[str, Any]:
        """Decide one item of its batch (the others stay pending). Returns the item's record."""
        record = await self.get(action_id)
        if record["batch_id"] is None:
            raise Conflict(f"this action is not pending (it is {record['status']})")
        await self.decide_batch(record["batch_id"], decision, [action_id], decided_by)
        return await self.get(action_id)

    async def sweep_expired(self) -> int:
        """Mark pending approval items past their expiry as expired. Returns how many."""
        rows = await self.db.fetchall(
            "SELECT DISTINCT batch_id FROM actions WHERE status = 'pending' AND expires_at <= ?",
            (self._clock(),),
        )
        return sum([await self._expire_batch(r["batch_id"]) for r in rows])

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
        batch: int | None = None,
    ) -> dict[str, Any]:
        where, params = [], []
        for column, value in (("status", status), ("action", action), ("batch_id", batch)):
            if value is not None and value != "":
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
        """The items waiting for a decision (expired ones are swept first)."""
        await self.sweep_expired()
        rows = await self.db.fetchall("SELECT * FROM actions WHERE status = 'pending' ORDER BY id")
        return [record_from_row(r) for r in rows]

    async def pending_batches(self) -> list[dict[str, Any]]:
        """Batches with at least one pending or queued (approved, waiting for the hourly cap)
        item, oldest first (expired ones are swept first)."""
        await self.sweep_expired()
        rows = await self.db.fetchall(
            "SELECT DISTINCT batch_id FROM actions WHERE status IN ('pending', 'queued') "
            "AND batch_id IS NOT NULL ORDER BY batch_id"
        )
        return [await self.get_batch(int(r["batch_id"])) for r in rows]

    async def get_batch(self, batch_id: int) -> dict[str, Any]:
        """A batch with its items (the audit rows) and a summary of where they stand."""
        row = await self._batch_row(batch_id)
        rows = await self.db.fetchall(
            "SELECT * FROM actions WHERE batch_id = ? ORDER BY id", (batch_id,)
        )
        items = [record_from_row(r) for r in rows]
        counts: dict[str, int] = {}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        definition = self.registry.get(row["action"])
        summary = self._summary(definition, items)
        label = row["label"]
        queued = counts.get("queued", 0)
        next_in = await self._next_in(items) if queued else None
        note = queue_note(queued, next_in)
        return {
            "id": row["id"],
            "ts": row["ts"],
            "updated": row["updated"],
            "action": row["action"],
            "label": label,
            "source": row["source"],
            "run_key": row["run_key"],
            "expires_at": row["expires_at"],
            "title": f"{label}: {summary}" if label else summary,
            "summary": summary,
            "total": len(items),
            "pending": counts.get("pending", 0),
            "counts": counts,
            "queued": queued,
            "next_in_s": next_in,
            "queue_note": note,
            "outcome": outcome_text(counts) + note,
            "items": items,
        }

    @staticmethod
    def _summary(definition: ActionDef | None, items: list[dict[str, Any]]) -> str:
        """What the batch asks for, from the arguments of its items."""
        if definition is None or not items:
            return f"{len(items)} item(s)"
        try:
            parsed = [definition.args_model.model_validate(i["args"]) for i in items]
        except ValidationError:  # an argument model changed since the row was written
            return f"{definition.name}: {len(items)} item(s)"
        if definition.summary is not None:
            return definition.summary(parsed)
        if len(parsed) == 1:
            return definition.target(parsed[0])
        return f"{definition.name} × {len(parsed)}"

    # ------------------------------------------------------------------ batches

    async def _batch_row(self, batch_id: int) -> Any:
        row = await self.db.fetchone("SELECT * FROM approval_batches WHERE id = ?", (batch_id,))
        if row is None:
            raise NotFound(f"no approval with id {batch_id}")
        return row

    async def _batch_for(
        self, action: str, source: str, run_key: str | None, label: str | None
    ) -> tuple[int, bool]:
        """The open batch these items belong to (id, created now?)."""
        async with self._batch_lock:
            now = self._clock()
            if run_key is not None:
                row = await self.db.fetchone(
                    "SELECT b.id FROM approval_batches b WHERE b.action = ? AND b.source = ? "
                    "AND b.run_key = ? AND (b.expires_at IS NULL OR b.expires_at > ?) "
                    "AND EXISTS (SELECT 1 FROM actions a WHERE a.batch_id = b.id "
                    "AND a.status = 'pending') ORDER BY b.id DESC LIMIT 1",
                    (action, source, run_key, now),
                )
                if row is not None:
                    return int(row["id"]), False
            expires = now + self.settings.approvals.approval_ttl.total_seconds()
            batch_id = await self.db.execute(
                "INSERT INTO approval_batches (ts, updated, action, label, source, run_key, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now, now, action, label, source, run_key, expires),
            )
            return batch_id, True

    async def _expire_batch(self, batch_id: int) -> int:
        """Expire the pending items of a batch that are past their expiry. Returns how many."""
        rows = await self.db.fetchall(
            "SELECT id FROM actions WHERE batch_id = ? AND status = 'pending' AND expires_at <= ?",
            (batch_id, self._clock()),
        )
        done = 0
        for row in rows:
            changed = await self._update(
                "UPDATE actions SET status = 'expired', decided_by = 'timeout', updated = ? "
                "WHERE id = ? AND status = 'pending'",
                (self._clock(), row["id"]),
            )
            if changed:
                done += 1
                await self._notify(EVENT_RESULT, await self.get(int(row["id"])))
        if done:
            view = await self.get_batch(batch_id)
            view["decision"] = "expired"
            view["decided_by"] = "timeout"
            await self._notify(EVENT_EXPIRED, view)
        return done

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
        batch_id: int | None = None,
    ) -> dict[str, Any]:
        now = self._clock()
        action_id = await self.db.execute(
            "INSERT INTO actions (ts, updated, action, target, args, policy, policy_key, status, "
            "source, reason, result, decided_by, expires_at, batch_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                batch_id,
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

    async def _cap_state(
        self, record: dict[str, Any], definition: ActionDef, args: Any
    ) -> tuple[int, list[float]]:
        """(hourly cap, `updated` times of this key's executions in the last hour, oldest first)."""
        cap = definition.max_per_hour_of(self.settings, args)
        if not cap:
            return 0, []
        rows = await self.db.fetchall(
            "SELECT updated FROM actions WHERE policy_key = ? AND updated >= ? "
            f"AND status IN ({','.join('?' * len(_RAN))}) AND id != ? ORDER BY updated",
            [record["policy_key"], self._clock() - HOUR, *_RAN, record["id"]],
        )
        return int(cap), [float(r["updated"]) for r in rows]

    async def _next_in(self, items: list[dict[str, Any]]) -> float:
        """Seconds until the hourly cap has room for the first queued item of a batch."""
        record = next(i for i in items if i["status"] == "queued")
        definition = self.registry[record["action"]]
        args = definition.args_model.model_validate(record["args"])
        cap, ran = await self._cap_state(record, definition, args)
        if not cap or len(ran) < cap:
            return 0.0
        return max(ran[len(ran) - cap] + HOUR - self._clock(), 0.0)

    async def drain_queue(self) -> int:
        """Execute approved items that were held back by the hourly cap, oldest first, as far as
        the cap allows. Returns how many were run. Called by the scheduler every minute."""
        rows = await self.db.fetchall(
            "SELECT id, batch_id, policy_key FROM actions WHERE status = 'queued' ORDER BY id"
        )
        full: set[str] = set()
        touched: list[int] = []
        ran = 0
        for row in rows:
            if row["policy_key"] in full:
                continue
            record = await self.get(int(row["id"]))
            definition = self.registry[record["action"]]
            args = definition.args_model.model_validate(record["args"])
            cap, used = await self._cap_state(record, definition, args)
            if cap and len(used) >= cap:
                full.add(row["policy_key"])
                continue
            claimed = await self._update(
                "UPDATE actions SET status = 'approved', updated = ? "
                "WHERE id = ? AND status = 'queued'",
                (self._clock(), row["id"]),
            )
            if not claimed:
                continue
            done = await self._run(int(row["id"]))
            if done["status"] == "queued":
                full.add(row["policy_key"])
                continue
            ran += 1
            if row["batch_id"] is not None and row["batch_id"] not in touched:
                touched.append(int(row["batch_id"]))
        for batch_id in touched:
            view = await self.get_batch(batch_id)
            view["decision"] = "approve"
            view["decided_by"] = next(
                (i["decided_by"] for i in view["items"] if i.get("decided_by")), ""
            )
            await self._notify(EVENT_PROGRESS, view)
        return ran

    async def _run(self, action_id: int) -> dict[str, Any]:
        """Execute an approved action: limits → dry-run → the real call."""
        record = await self.get(action_id)
        definition = self.registry[record["action"]]
        args = definition.args_model.model_validate(record["args"])
        async with self._lock:
            cap, ran = await self._cap_state(record, definition, args)
            if cap and len(ran) >= cap:
                # Approved stays approved: the item waits and `drain_queue` runs it as soon as
                # the hourly cap has room again. Nobody is asked a second time.
                return await self._set(
                    action_id,
                    "queued",
                    {
                        "detail": f"limit reached: {cap} per hour for {record['policy_key']}; "
                        "waiting, it runs by itself when the limit frees up",
                    },
                )
            if definition.uses_dry_run(self.settings):
                return await self._set(
                    action_id,
                    "dry_run",
                    {"dry_run": True, "would": f"{record['action']} → {record['target']}"},
                )
            try:
                result = await definition.run(self, args)
            except ActionError as exc:
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
