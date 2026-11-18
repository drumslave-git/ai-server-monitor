"""The picture of all jobs: the overall status (newest summary run) and every job's last run.
Shared by GET /api/status and the Telegram /status command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import api_models

if TYPE_CHECKING:
    from ..context import AppContext


def headline(run: dict[str, Any]) -> str:
    """First line of a run's summary (or its error)."""
    text = (run.get("summary") or run.get("error") or "").strip()
    return text.splitlines()[0].lstrip("# ").strip() if text else ""


async def job_status(ctx: AppContext) -> dict[str, Any]:
    """`overall` (newest summary run) and the newest run of every job."""
    jobs = await ctx.jobs.store.list()
    latest = await ctx.jobs.store.latest_runs()
    running = await ctx.jobs.store.running_runs()
    items = []
    overall = None
    for job in jobs:
        run = latest.get(job["slug"])
        items.append(
            api_models.JobStatus(
                slug=job["slug"],
                name=job["name"],
                kind=job["kind"],
                enabled=job["enabled"],
                status=run["status"] if run else None,
                finished=run["finished"] if run else None,
                run_id=run["id"] if run else None,
                running_run_id=running.get(job["slug"]),
            )
        )
        if job["kind"] == "summary" and run and (overall is None or run["id"] > overall.run_id):
            overall = api_models.OverallStatus(
                status=run["status"],
                headline=headline(run),
                finished=run["finished"],
                run_id=run["id"],
            )
    return {"overall": overall, "jobs": items}
