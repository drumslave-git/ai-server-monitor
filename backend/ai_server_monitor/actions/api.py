"""HTTP API of the action layer: audit log and approvals."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..context import AppContext
from .registry import ActionError
from .service import Conflict, NotFound

router = APIRouter()


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    items: list[int] | None = None
    """Item (audit row) ids to decide; omitted = every item of the batch that is still pending."""


@router.get("/api/actions")
async def list_actions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: str | None = None,
    action: str | None = None,
    batch: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    """The audit log, newest first. Filter with ?status=, ?action= and ?batch= (approval id)."""
    return await _ctx(request).actions.list_actions(
        limit=limit, offset=offset, status=status, action=action, batch=batch
    )


@router.get("/api/actions/{action_id}")
async def get_action(action_id: int, request: Request) -> dict[str, Any]:
    """One audit row, in the same shape as an item of `/api/actions`."""
    try:
        return await _ctx(request).actions.get(action_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/approvals")
async def list_approvals(request: Request) -> dict[str, Any]:
    """Approvals waiting for a decision (expired ones are swept first): `batches` has one
    entry per approval with its items; `items` is the flat list of pending items."""
    service = _ctx(request).actions
    return {"batches": await service.pending_batches(), "items": await service.pending()}


@router.get("/api/approvals/{batch_id}")
async def get_approval(batch_id: int, request: Request) -> dict[str, Any]:
    """One approval batch with all its items and their outcomes."""
    try:
        return await _ctx(request).actions.get_batch(batch_id)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/approvals/{batch_id}")
async def decide(batch_id: int, body: Decision, request: Request) -> dict[str, Any]:
    """Approve (→ executes the items now) or reject an approval batch: all its pending items,
    or only `items`. The answer is the batch with the outcome of every item; changing shell
    commands answer "approved" at once and report their outcome by event when they finish."""
    try:
        return await _ctx(request).actions.decide_batch(
            batch_id, body.decision, body.items, decided_by="ui"
        )
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
