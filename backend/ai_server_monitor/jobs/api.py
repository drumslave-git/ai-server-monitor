"""HTTP API of the scheduled jobs."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ..context import AppContext
from ..mcp_client.api import page
from ..mcp_client.turn import classify
from .schemas import Job, JobCreate, JobList, JobRun, JobRunList, JobUpdate, RunStarted, job_out
from .store import JobError, UnknownDomain

router = APIRouter()


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


async def _job(ctx: AppContext, slug: str) -> Job:
    job = await ctx.jobs.store.get(slug)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {slug!r}")
    latest = await ctx.jobs.store.latest_runs()
    running = await ctx.jobs.store.running_runs()
    return job_out(job, latest.get(slug), ctx.job_next_run(slug), running.get(slug))


@router.get("/api/jobs")
async def list_jobs(request: Request) -> JobList:
    """Every job (builtins first) with its newest finished run and next scheduled time."""
    ctx = _ctx(request)
    latest = await ctx.jobs.store.latest_runs()
    running = await ctx.jobs.store.running_runs()
    return JobList(
        items=[
            job_out(j, latest.get(j["slug"]), ctx.job_next_run(j["slug"]), running.get(j["slug"]))
            for j in await ctx.jobs.store.list()
        ]
    )


@router.post("/api/jobs", status_code=201)
async def create_job(request: Request, body: JobCreate) -> Job:
    """A custom topic job (runs on the worker model unless `model` is `main`)."""
    ctx = _ctx(request)
    try:
        await ctx.jobs.store.create(body.model_dump(), created_by="user")
    except UnknownDomain as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await ctx.jobs_changed()
    return await _job(ctx, body.slug)


class ClassifyRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)


class ClassifyResult(BaseModel):
    domains: list[str]
    """The domains the job's prompt needs (empty: none, or there are no domains with tools)."""


@router.post("/api/jobs/classify-domains")
async def classify_domains(request: Request, body: ClassifyRequest) -> ClassifyResult:
    """The job form's "Classify" button: the worker model picks, among the domains with at
    least one enabled tool, the MCP domains a job's prompt needs (nothing is saved)."""
    ctx = _ctx(request)
    tools = await page(ctx)
    on = {t.domain_id for t in tools.tools if t.enabled and t.domain_id is not None}
    domains = [d.model_dump() for d in tools.domains if d.id in on]
    if not domains:
        return ClassifyResult(domains=[])
    try:
        picked = await classify(ctx.models.model("worker"), domains, body.prompt)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"classification failed: {type(exc).__name__}: {exc}"
        ) from None
    return ClassifyResult(domains=picked)


@router.get("/api/jobs/runs/{run_id}")
async def get_run(request: Request, run_id: int) -> JobRun:
    run = await _ctx(request).jobs.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    return JobRun.model_validate(run)


@router.get("/api/jobs/{slug}")
async def get_job(request: Request, slug: str) -> Job:
    return await _job(_ctx(request), slug)


@router.put("/api/jobs/{slug}")
async def update_job(request: Request, slug: str, body: JobUpdate) -> Job:
    """Change the given fields. Editing a builtin's prompt, schedule, model or limits marks it
    edited; `POST /api/jobs/{slug}/reset` restores the release's version."""
    ctx = _ctx(request)
    try:
        await ctx.jobs.store.update(slug, body.model_dump(exclude_unset=True))
    except UnknownDomain as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except JobError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    await ctx.jobs_changed()
    return await _job(ctx, slug)


@router.delete("/api/jobs/{slug}", status_code=204)
async def delete_job(request: Request, slug: str) -> Response:
    """Custom jobs only (and their runs); builtins can be disabled instead."""
    ctx = _ctx(request)
    if await ctx.jobs.store.get(slug) is None:
        raise HTTPException(status_code=404, detail=f"no job {slug!r}")
    try:
        await ctx.jobs.store.delete(slug)
    except JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await ctx.jobs_changed()
    return Response(status_code=204)


@router.post("/api/jobs/{slug}/reset")
async def reset_job(request: Request, slug: str) -> Job:
    """A builtin back to the release's name, prompt, schedule, model and limits."""
    ctx = _ctx(request)
    if await ctx.jobs.store.get(slug) is None:
        raise HTTPException(status_code=404, detail=f"no job {slug!r}")
    try:
        await ctx.jobs.store.reset(slug)
    except JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await ctx.jobs_changed()
    return await _job(ctx, slug)


@router.post("/api/jobs/{slug}/run", status_code=202)
async def run_job(request: Request, slug: str) -> RunStarted:
    """Start a run now (also when the job is disabled or automation is paused). Returns the run
    id at once; `job.started` / `job.finished` events follow on /api/stream."""
    ctx = _ctx(request)
    if await ctx.jobs.store.get(slug) is None:
        raise HTTPException(status_code=404, detail=f"no job {slug!r}")
    return RunStarted(run_id=await ctx.jobs.start(slug, "manual"))


@router.get("/api/jobs/{slug}/runs")
async def list_runs(
    request: Request, slug: str, limit: Annotated[int, Query(ge=1, le=200)] = 20
) -> JobRunList:
    """The job's runs, newest first (a running one included)."""
    ctx = _ctx(request)
    if await ctx.jobs.store.get(slug) is None:
        raise HTTPException(status_code=404, detail=f"no job {slug!r}")
    return JobRunList(
        items=[JobRun.model_validate(r) for r in await ctx.jobs.store.runs(slug, limit)]
    )
