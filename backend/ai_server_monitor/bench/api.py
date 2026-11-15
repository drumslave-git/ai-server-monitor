"""HTTP API of the benchmarks: catalog, runs (start, cancel, list, detail, export, delete) and
private snapshots. Live progress goes over the event bus (`bench.progress`, `bench.done`)."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..context import AppContext
from .service import BenchBusy, BenchNotReady

router = APIRouter(prefix="/api/bench", tags=["benchmarks"])

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: str | None = None
    """Run only this group (report, chat, prom_plan, robust); omit for everything."""
    ids: list[str] | None = None
    """Run exactly these scenarios."""
    repeats: int | None = Field(default=None, ge=1, le=20)
    """Attempts per scenario (default: Settings → Benchmarks → repeats)."""


class SnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = Field(default="", max_length=300)


@router.get("/catalog")
async def catalog(request: Request, smoke: bool = False) -> dict[str, Any]:
    return _ctx(request).bench.catalog(include_smoke=smoke)


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    ctx = _ctx(request)
    llm = ctx.settings.llm
    job = ctx.scheduler.jobs.get("report")
    return {
        "running": ctx.bench.running,
        "progress": ctx.bench.progress,
        "llm": {"configured": llm.configured, "model": llm.model, "base_url": llm.base_url},
        "judge": ctx.settings.bench.judge.configured,
        "repeats": ctx.settings.bench.repeats,
        "report_running": bool(job is not None and job.running),
    }


@router.post("/runs", status_code=202)
async def start(request: Request, body: StartRequest) -> dict[str, Any]:
    ctx = _ctx(request)
    try:
        run_id = await ctx.bench.start(group=body.group, ids=body.ids, repeats=body.repeats)
    except BenchBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except BenchNotReady as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"run_id": run_id}


@router.post("/cancel")
async def cancel(request: Request) -> dict[str, bool]:
    return {"cancelled": await _ctx(request).bench.cancel()}


@router.get("/runs")
async def runs(request: Request) -> dict[str, Any]:
    return {"items": await _ctx(request).bench.store.list_runs()}


@router.get("/runs/{run_id}")
async def run_detail(request: Request, run_id: int) -> dict[str, Any]:
    store = _ctx(request).bench.store
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return {**run, "attempts": await store.attempts(run_id)}


@router.get("/runs/{run_id}/attempts/{attempt_id}")
async def attempt_detail(request: Request, run_id: int, attempt_id: int) -> dict[str, Any]:
    attempt = await _ctx(request).bench.store.attempt(attempt_id)
    if attempt is None or attempt["run_id"] != run_id:
        raise HTTPException(404, "no such attempt")
    return attempt


@router.get("/runs/{run_id}/export")
async def export(request: Request, run_id: int) -> Response:
    store = _ctx(request).bench.store
    run = await store.get_run(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    doc = {"run": run, "attempts": await store.full_attempts(run_id)}
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"bench-{run_id}-{run.get('model') or 'model'}")
    return Response(
        json.dumps(doc, ensure_ascii=False, indent=1),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}.json"'},
    )


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(request: Request, run_id: int) -> Response:
    ctx = _ctx(request)
    progress = ctx.bench.progress
    if progress and progress.get("run_id") == run_id:
        raise HTTPException(409, "this run is still in progress; cancel it first")
    if not await ctx.bench.store.delete_run(run_id):
        raise HTTPException(404, "no such run")
    return Response(status_code=204)


@router.get("/snapshots")
async def snapshots(request: Request) -> dict[str, Any]:
    return {"items": await _ctx(request).bench.store.list_snapshots()}


@router.post("/snapshots", status_code=201)
async def capture(request: Request, body: SnapshotRequest) -> dict[str, Any]:
    if not _NAME.match(body.name):
        raise HTTPException(400, "name: letters, digits, - and _ (max 40)")
    try:
        done = await _ctx(request).bench.capture_snapshot(body.name, body.description)
    except BenchNotReady as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**done, "reference": f"private:{body.name}"}


@router.delete("/snapshots/{name}", status_code=204)
async def delete_snapshot(request: Request, name: str) -> Response:
    if not await _ctx(request).bench.store.delete_snapshot(name):
        raise HTTPException(404, "no such snapshot")
    return Response(status_code=204)
