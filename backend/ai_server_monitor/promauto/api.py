"""HTTP API of the Prometheus auto-queries (Settings → Collectors → Prometheus)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..context import AppContext
from ..thresholds import normalize_threshold
from .store import MAX_QUERY, clean_name

router = APIRouter(prefix="/api/prometheus", tags=["prometheus"])


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


class QueryPatch(BaseModel):
    """Only the sent fields change. Changing the query text, thresholds, unit or name marks
    the query as edited, which keeps it when the planner chooses again."""

    enabled: bool | None = None
    pinned: bool | None = None
    name: str | None = Field(default=None, max_length=60)
    query: str | None = Field(default=None, min_length=1, max_length=MAX_QUERY)
    description: str | None = Field(default=None, max_length=160)
    unit: str | None = Field(default=None, max_length=12)
    warn_if: str | None = None
    crit_if: str | None = None

    @field_validator("warn_if", "crit_if")
    @classmethod
    def _threshold(cls, value: str | None) -> str | None:
        return normalize_threshold(value)


async def _overview(ctx: AppContext) -> dict[str, Any]:
    cfg = ctx.settings.collectors.prometheus
    return {
        "configured": bool(cfg.url),
        "auto_queries": cfg.auto_queries,
        "max_auto_queries": cfg.max_auto_queries,
        "llm_configured": ctx.settings.llm.configured,
        "queries": await ctx.promauto.store.fetch(),
        "plan": await ctx.promauto.status(),
    }


@router.get("/queries")
async def list_queries(request: Request) -> dict[str, Any]:
    """The planned queries (with their last result or error) and the state of the planner."""
    return await _overview(_ctx(request))


@router.patch("/queries/{query_id}")
async def patch_query(query_id: int, patch: QueryPatch, request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    changes = patch.model_dump(exclude_unset=True)
    if "name" in changes:
        name = clean_name(changes["name"])
        if not name:
            raise HTTPException(422, "The name needs letters, digits or underscores.")
        changes["name"] = name
        clash = next(
            (
                q
                for q in await ctx.promauto.store.fetch()
                if q["name"] == name and q["id"] != query_id
            ),
            None,
        )
        if clash is not None:
            raise HTTPException(409, f"There already is a query named {name}.")
    updated = await ctx.promauto.store.update(query_id, changes)
    if updated is None:
        raise HTTPException(404, "No such query.")
    return updated


@router.delete("/queries/{query_id}", status_code=204)
async def delete_query(query_id: int, request: Request) -> None:
    if not await _ctx(request).promauto.store.delete(query_id):
        raise HTTPException(404, "No such query.")


@router.post("/replan", status_code=202)
async def replan(request: Request) -> dict[str, Any]:
    """Choose the queries again now (runs in the background; poll GET /queries)."""
    ctx = _ctx(request)
    if not ctx.promauto.enabled():
        raise HTTPException(
            409, "Automatic queries are off, or Prometheus is not set up (Settings → Collectors)."
        )
    if not ctx.replan_prometheus_now():
        raise HTTPException(409, "A plan is already running.")
    return {"started": True}
