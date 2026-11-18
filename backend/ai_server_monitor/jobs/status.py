"""The picture of all jobs: the overall status (newest summary run) and every job's last run.
Shared by GET /api/status and the Telegram /status command."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import api_models

if TYPE_CHECKING:
    from ..context import AppContext


_FAILURES = (
    # (start of the stored error or exception name, plain words)
    ("ModelConnectionError", "the model could not be reached"),
    ("ConnectError", "the model could not be reached"),
    ("ModelTimeoutError", "the model did not answer in time"),
    ("ModelAuthenticationError", "the model endpoint refused the API key"),
    ("ModelNotFoundError", "the model name is not known to the endpoint"),
    ("ModelRateLimitError", "the model endpoint is rate-limiting"),
    ("ModelQuotaExceededError", "the model endpoint's quota is used up"),
    ("ModelContextWindowExceededError", "the conversation got too long for the model"),
    ("UsageLimitExceeded", "the run reached its step limit"),
    ("timed out", "it took longer than its time limit"),
    ("no result", "the model gave no result"),
    ("cancelled", "the run was cancelled"),
    ("interrupted", "the service stopped during the run"),
    ("the model is not configured", "the model is not configured (Settings)"),
)


def failure_reason(error: str | None) -> str:
    """A failed run's error in plain words; the raw text stays in the run's `error`."""
    text = (error or "").strip()
    for start, words in _FAILURES:
        if text.startswith(start):
            return words
    if text.startswith("Model"):
        return "the model endpoint returned an error"
    return "an internal error (see the run)"


def headline(run: dict[str, Any], name: str = "Summary") -> str:
    """First line of a run's summary; for a failed run "<name> failed: <plain reason>"."""
    if run.get("status") == "error":
        return f"{name} failed: {failure_reason(run.get('error'))}"
    text = (run.get("summary") or "").strip()
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
