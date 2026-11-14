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


@router.get("/api/actions")
async def list_actions(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: str | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    """The audit log, newest first. Filter with ?status= and ?action=."""
    return await _ctx(request).actions.list_actions(
        limit=limit, offset=offset, status=status, action=action
    )


@router.get("/api/approvals")
async def list_approvals(request: Request) -> dict[str, Any]:
    """Actions waiting for a decision (expired ones are swept first)."""
    return {"items": await _ctx(request).actions.pending()}


@router.post("/api/approvals/{action_id}")
async def decide(action_id: int, body: Decision, request: Request) -> dict[str, Any]:
    """Approve (→ executes it now) or reject a pending action."""
    try:
        return await _ctx(request).actions.decide(action_id, body.decision, decided_by="ui")
    except NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
