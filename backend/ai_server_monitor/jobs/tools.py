"""The jobs as tools of the main model in chat: few and plain. No approval step (they change
only the monitor's own schedule); every call goes to the chat audit trail, and what the model
creates is marked `created_by: llm`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, ValidationError

from ..agent.runtime import AgentTool, ToolError
from .prompts import schedule_text
from .schemas import SLUG_PATTERN, JobCreate, JobUpdate
from .store import JobError

if TYPE_CHECKING:
    from ..context import AppContext

MAX_RESULTS = 10


class _SaveArgs(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN, description="Job id, e.g. 'backups'.")
    name: str | None = Field(default=None, description="Display name (new jobs: required).")
    prompt: str | None = Field(
        default=None,
        description="The check for a small model: scope, typical commands, what counts as ok, "
        "warn and crit (new jobs: required).",
    )
    interval_s: int | None = Field(default=None, description="Run every N seconds (min 60).")
    cron: str | None = Field(default=None, description="Or a crontab line, e.g. '0 6 * * *'.")
    model: str | None = Field(default=None, description="worker (default) or main.")
    enabled: bool | None = None
    domains: list[str] | None = Field(
        default=None,
        description="MCP domains whose tools the job's runs get (names from the deferred "
        "capabilities list without 'domain-'). Without domains a job has only the shell.",
    )


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    from ..chat.tools import _clean_schema

    return _clean_schema(model.model_json_schema())


def _slug_schema(description: str, required: bool = True) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"slug": {"type": "string", "description": description}},
        "required": ["slug"] if required else [],
    }


def _job_line(job: dict[str, Any], last: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "slug": job["slug"],
        "name": job["name"],
        "kind": job["kind"],
        "schedule": schedule_text(job),
        "enabled": job["enabled"],
        "builtin": job["builtin"],
        "model": job["model"],
        "domains": job.get("domains", []),
        "last_status": last["status"] if last else None,
        "last_finished": last["finished"] if last else None,
        "last_summary": (last.get("summary") or last.get("error")) if last else None,
    }


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "job_slug", "trigger", "status", "summary", "findings", "error", "finished")
    return {k: run.get(k) for k in keep} | {"duration_s": run.get("duration_s")}


def _invalid(exc: ValidationError) -> ToolError:
    details = "; ".join(
        f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}"
        for e in exc.errors(include_url=False, include_input=False, include_context=False)
    )
    return ToolError(f"invalid job: {details}")


def job_tools(app: AppContext, session: str) -> list[AgentTool]:
    store = app.jobs.store

    async def audit(tool: str, **fields: Any) -> None:
        from ..chat.audit import record_call

        await record_call(app.db, "jobs", session, tool=tool, **fields)

    async def jobs_list(args: dict[str, Any]) -> Any:
        latest = await store.latest_runs()
        return [_job_line(j, latest.get(j["slug"])) for j in await store.list()]

    async def job_results(args: dict[str, Any]) -> Any:
        slug = args.get("slug") or "summary"
        if await store.get(slug) is None:
            raise ToolError(f"no job {slug!r} (jobs_list shows them)")
        limit = max(1, min(int(args.get("limit") or 1), MAX_RESULTS))
        runs = await store.runs(slug, limit)
        return [_run_view(r) for r in runs] or f"{slug} has not run yet"

    async def job_save(args: dict[str, Any]) -> Any:
        try:
            parsed = _SaveArgs.model_validate(args)
        except ValidationError as exc:
            raise _invalid(exc) from None
        data = parsed.model_dump(exclude_none=True)
        slug = data.pop("slug")
        try:
            if await store.get(slug) is None:
                body = JobCreate.model_validate({"slug": slug, **data})
                job = await store.create(body.model_dump(), created_by="llm")
                action = "created"
            else:
                changes = JobUpdate.model_validate(data).model_dump(exclude_unset=True)
                job = await store.update(slug, changes)
                action = "updated"
        except ValidationError as exc:
            raise _invalid(exc) from None
        except JobError as exc:
            raise ToolError(str(exc)) from None
        await app.jobs_changed()
        await audit("job_save", slug=slug, action=action)
        return {"saved": action, **_job_line(job, None), "next_run": app.job_next_run(slug)}

    async def job_delete(args: dict[str, Any]) -> Any:
        slug = str(args.get("slug") or "")
        try:
            await store.delete(slug)
        except JobError as exc:
            raise ToolError(str(exc)) from None
        await app.jobs_changed()
        await audit("job_delete", slug=slug)
        return {"deleted": slug}

    async def job_run(args: dict[str, Any]) -> Any:
        slug = str(args.get("slug") or "")
        if await store.get(slug) is None:
            raise ToolError(f"no job {slug!r} (jobs_list shows them)")
        await audit("job_run", slug=slug)
        return _run_view(await app.jobs.run(slug, "llm"))

    return [
        AgentTool(
            "jobs_list",
            "The monitor's scheduled checks (storage, journal, docker, ... and the summary) "
            "with their schedule and latest status.",
            {"type": "object", "properties": {}},
            jobs_list,
        ),
        AgentTool(
            "job_results",
            "The latest results of one job: status, summary and findings. Without a slug: the "
            "latest overall summary.",
            {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Job id (default: summary)."},
                    "limit": {"type": "integer", "description": "How many runs (1-10)."},
                },
            },
            job_results,
        ),
        AgentTool(
            "job_save",
            "Create a scheduled check (slug, name, prompt and interval_s or cron) or change "
            "one (slug and the fields to change). Only when the owner asks for it.",
            _schema(_SaveArgs),
            job_save,
        ),
        AgentTool(
            "job_delete",
            "Delete a custom job the owner no longer wants (builtins can only be disabled "
            "with job_save enabled=false).",
            _slug_schema("Job id."),
            job_delete,
        ),
        AgentTool(
            "job_run",
            "Run one job now and return its finished result (takes as long as the check).",
            _slug_schema("Job id."),
            job_run,
        ),
    ]
