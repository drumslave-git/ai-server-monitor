"""HTTP API of the scheduled jobs."""

from __future__ import annotations

from typing import Annotated, Any

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


async def _with_queue(ctx: AppContext, run: dict[str, Any] | None) -> dict[str, Any] | None:
    """A summary run with the runs of its queue."""
    if run is None:
        return None
    return {**run, "queue": await ctx.jobs.store.queue_runs(run["id"])}


async def _out(
    ctx: AppContext,
    job: dict[str, Any],
    latest: dict[str, dict[str, Any]],
    running: dict[str, int],
) -> Job:
    slug = job["slug"]
    last = latest.get(slug)
    if job["kind"] == "summary":
        last = await _with_queue(ctx, last)
    next_run = None if job["retired"] else ctx.job_next_run(slug)
    out = job_out(job, last, next_run, running.get(slug))
    out.notice = job.get("notice")
    return out


async def _job(ctx: AppContext, slug: str, notice: str | None = None) -> Job:
    job = await ctx.jobs.store.get(slug)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no job {slug!r}")
    latest = await ctx.jobs.store.latest_runs()
    running = await ctx.jobs.store.running_runs()
    return await _out(ctx, {**job, "notice": notice}, latest, running)


async def _exists(ctx: AppContext, slug: str) -> None:
    if await ctx.jobs.store.get(slug) is None:
        raise HTTPException(status_code=404, detail=f"no job {slug!r}")


@router.get("/api/jobs")
async def list_jobs(request: Request) -> JobList:
    """Every summary and job (summaries first, then builtins) with its newest finished run (a
    summary's with the runs of its queue) and next scheduled time; `retired`: the old builtins
    a release replaced, whose runs stay readable."""
    ctx = _ctx(request)
    store = ctx.jobs.store
    latest = await store.latest_runs()
    running = await store.running_runs()
    return JobList(
        items=[await _out(ctx, j, latest, running) for j in await store.list()],
        retired=[await _out(ctx, j, latest, running) for j in await store.retired()],
    )


@router.post("/api/jobs", status_code=201)
async def create_job(request: Request, body: JobCreate) -> Job:
    """A new job (`kind` topic: a check) or summary (`kind` summary: runs its `members` one
    after another, then sums up). `notice` says what else changed (a member's own schedule
    removed)."""
    ctx = _ctx(request)
    try:
        job = await ctx.jobs.store.create(body.model_dump(), created_by="user")
    except UnknownDomain as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await ctx.jobs_changed()
    return await _job(ctx, body.slug, job.get("notice"))


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
    """One run; a summary run with the runs of its queue, in the order they ran."""
    ctx = _ctx(request)
    run = await ctx.jobs.store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no run {run_id}")
    return JobRun.model_validate(await _with_queue(ctx, run))


@router.get("/api/jobs/{slug}")
async def get_job(request: Request, slug: str) -> Job:
    return await _job(_ctx(request), slug)


@router.put("/api/jobs/{slug}")
async def update_job(request: Request, slug: str, body: JobUpdate) -> Job:
    """Change the given fields. Editing a builtin's prompt, schedule, model, limits, domains or
    (a summary) members marks it edited; `POST /api/jobs/{slug}/reset` restores the release's
    version. A job in a summary cannot get a schedule; one that joins a summary loses its own,
    and `notice` says so."""
    ctx = _ctx(request)
    await _exists(ctx, slug)
    try:
        job = await ctx.jobs.store.update(slug, body.model_dump(exclude_unset=True))
    except UnknownDomain as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await ctx.jobs_changed()
    return await _job(ctx, slug, job.get("notice"))


@router.delete("/api/jobs/{slug}", status_code=204)
async def delete_job(request: Request, slug: str) -> Response:
    """Own jobs and summaries only (and their runs); builtins can be disabled instead.
    Deleting a summary keeps its jobs (in no other summary they are detached, without a
    schedule)."""
    ctx = _ctx(request)
    await _exists(ctx, slug)
    try:
        await ctx.jobs.store.delete(slug)
    except JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await ctx.jobs_changed()
    return Response(status_code=204)


@router.post("/api/jobs/{slug}/reset")
async def reset_job(request: Request, slug: str) -> Job:
    """A builtin back to the release's name, prompt, schedule, model and limits (a summary:
    its members too); domains cleared, notify and enabled kept."""
    ctx = _ctx(request)
    await _exists(ctx, slug)
    try:
        job = await ctx.jobs.store.reset(slug)
    except JobError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    await ctx.jobs_changed()
    return await _job(ctx, slug, job.get("notice"))


@router.post("/api/jobs/{slug}/run", status_code=202)
async def run_job(request: Request, slug: str) -> RunStarted:
    """Start a run now (also when the job is disabled or automation is paused): a summary runs
    its whole queue, a job in a summary runs alone (no summary). Returns the run id at once;
    `job.started` / `job.finished` events follow on /api/stream."""
    ctx = _ctx(request)
    await _exists(ctx, slug)
    return RunStarted(run_id=await ctx.jobs.start(slug, "manual"))


@router.get("/api/jobs/{slug}/runs")
async def list_runs(
    request: Request, slug: str, limit: Annotated[int, Query(ge=1, le=200)] = 20
) -> JobRunList:
    """The job's runs, newest first (a running one included); a retired job's too."""
    ctx = _ctx(request)
    if await ctx.jobs.store.get(slug, retired=True) is None:
        raise HTTPException(status_code=404, detail=f"no job {slug!r}")
    return JobRunList(
        items=[JobRun.model_validate(r) for r in await ctx.jobs.store.runs(slug, limit)]
    )
