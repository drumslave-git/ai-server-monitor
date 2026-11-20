"""The jobs as tools of the main model in chat: few and plain. No approval step (they change
only the monitor's own schedule); every call goes to the chat audit trail, and what the model
creates is marked `created_by: llm`.

Jobs and summaries: a job is a check; a summary runs its member jobs in order and sums up
their results. A member has no schedule of its own (the summary runs it), so joining a summary
clears a job's schedule and the result's `notice` says so. `summary_member` puts one job into a
summary (after or before another member, by slug) or takes it out in one call."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from ..agent.runtime import AgentTool, ToolError
from .prompts import schedule_text
from .schemas import SLUG_PATTERN, JobCreate, JobUpdate
from .store import JobError

if TYPE_CHECKING:
    from ..context import AppContext

MAX_RESULTS = 10


class _SaveArgs(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN, description="Id, e.g. 'backups' or 'summary:weekly'.")
    kind: Literal["job", "summary"] | None = Field(
        default=None,
        description="New ones only: job (a check, the default) or summary (runs its members "
        "in order and sums up their results).",
    )
    name: str | None = Field(default=None, description="Display name (new ones: required).")
    prompt: str | None = Field(
        default=None,
        description="A job: the check for a small model: scope, typical commands, what counts "
        "as ok, warn and crit. A summary: how to sum up its jobs' results. New ones: required.",
    )
    interval_s: int | None = Field(
        default=None,
        description="Run every N seconds (min 60). Not for a job in a summary: a member has "
        "no schedule of its own.",
    )
    cron: str | None = Field(default=None, description="Or a crontab line, e.g. '0 6 * * *'.")
    model: str | None = Field(
        default=None, description="worker or main (default: worker for a job, main for a summary)."
    )
    enabled: bool | None = None
    notify: Literal["always", "changed", "warn_crit", "never"] | None = Field(
        default=None,
        description="A summary or a job in no summary: when its result goes to Telegram. "
        "always: every run; changed: when the status or the warnings change; warn_crit: on "
        "warn, crit or error; never. A job in a summary sends nothing itself.",
    )
    members: list[str] | None = Field(
        default=None,
        description="A summary: all its jobs (slugs) in the order they run; replaces the list. "
        "To add, move or remove one job use summary_member. Joining clears a job's schedule.",
    )
    summaries: list[str] | None = Field(
        default=None,
        description="A job: the summaries (slugs) it is in; joining one adds it at the end and "
        "clears its own schedule.",
    )
    domains: list[str] | None = Field(
        default=None,
        description="MCP domains whose tools the job's runs get (names from the deferred "
        "capabilities list without 'domain-'). Without domains a job has only the shell.",
    )


class _MemberArgs(BaseModel):
    summary: str = Field(description="The summary's slug, e.g. 'summary:health'.")
    job: str = Field(description="The job's slug, e.g. 'docker:status'.")
    action: Literal["add", "remove"] = Field(
        default="add",
        description="add (also moves a job that is in it already) or remove.",
    )
    after: str | None = Field(
        default=None, description="add: right after this member (slug). Default: at the end."
    )
    before: str | None = Field(
        default=None, description="add: right before this member (slug), instead of after."
    )


KIND_WORD = {"topic": "job", "summary": "summary"}
"""The store's kinds as the tools name them (a topic job is just "job" to the model)."""


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
    summary = job["kind"] == "summary"
    return {
        "slug": job["slug"],
        "name": job["name"],
        "kind": KIND_WORD[job["kind"]],
        "schedule": schedule_text(job),
        "enabled": job["enabled"],
        "builtin": job["builtin"],
        "model": job["model"],
        "domains": job.get("domains", []),
        "notify": "nothing itself (its summary reports)" if job["summaries"] else job["notify"],
        **({"members": job["members"]} if summary else {"summaries": job["summaries"]}),
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
        slug = args.get("slug")
        if not slug:  # the latest run of every enabled summary
            latest = await store.latest_runs()
            return {
                j["slug"]: _run_view(latest[j["slug"]]) if j["slug"] in latest else "not run yet"
                for j in await store.list()
                if j["kind"] == "summary" and j["enabled"]
            } or "no summary is enabled"
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
        kind = data.pop("kind", None)
        try:
            existing = await store.get(slug)
            if existing is None:
                stored_kind = "summary" if kind == "summary" else "topic"
                body = JobCreate.model_validate({"slug": slug, "kind": stored_kind, **data})
                job = await store.create(body.model_dump(), created_by="llm")
                action = "created"
            else:
                if kind is not None and kind != KIND_WORD[existing["kind"]]:
                    raise ToolError(
                        f"{slug} is a {KIND_WORD[existing['kind']]}; the kind cannot change "
                        "(create a new one with another slug)"
                    )
                changes = JobUpdate.model_validate(data).model_dump(exclude_unset=True)
                job = await store.update(slug, changes)
                action = "updated"
        except ValidationError as exc:
            raise _invalid(exc) from None
        except JobError as exc:
            raise ToolError(str(exc)) from None
        await app.jobs_changed()
        await audit("job_save", slug=slug, action=action)
        out = {"saved": action, **_job_line(job, None), "next_run": app.job_next_run(slug)}
        return out | ({"notice": job["notice"]} if job.get("notice") else {})

    async def job_delete(args: dict[str, Any]) -> Any:
        slug = str(args.get("slug") or "")
        try:
            await store.delete(slug)
        except JobError as exc:
            raise ToolError(str(exc)) from None
        await app.jobs_changed()
        await audit("job_delete", slug=slug)
        return {"deleted": slug}

    async def summary_member(args: dict[str, Any]) -> Any:
        try:
            parsed = _MemberArgs.model_validate(args)
        except ValidationError as exc:
            raise _invalid(exc) from None
        summary = await store.get(parsed.summary)
        if summary is None or summary["kind"] != "summary":
            what = "a job, not a summary" if summary else "not known"
            raise ToolError(f"{parsed.summary!r} is {what} (jobs_list shows the summaries)")
        if await store.get(parsed.job) is None:
            raise ToolError(f"no job {parsed.job!r} (jobs_list shows them)")
        current = list(summary["members"])
        others = [m for m in current if m != parsed.job]
        if parsed.action == "remove":
            if parsed.job not in current:
                raise ToolError(f"{parsed.job} is not in {parsed.summary}")
            wanted, done = others, "removed"
        else:
            if parsed.after and parsed.before:
                raise ToolError("give after or before, not both")
            anchor = parsed.after or parsed.before
            if anchor is not None and anchor not in others:
                raise ToolError(
                    f"{anchor!r} is not another member of {parsed.summary} "
                    f"(its members: {', '.join(current) or 'none'})"
                )
            if anchor is None:
                at = len(others)
            else:
                at = others.index(anchor) + (1 if parsed.after else 0)
            wanted = [*others[:at], parsed.job, *others[at:]]
            done = "moved" if parsed.job in current else "added"
            if wanted == current:
                done = "unchanged (already there)"
        try:
            saved = await store.update(parsed.summary, {"members": wanted})
        except JobError as exc:
            raise ToolError(str(exc)) from None
        await app.jobs_changed()
        await audit("summary_member", slug=parsed.summary, job=parsed.job, action=done)
        out: dict[str, Any] = {"summary": parsed.summary, parsed.job: done}
        out["members"] = saved["members"]
        notice = saved.get("notice")
        if done == "removed":
            job = await store.require(parsed.job)
            if not job["summaries"] and not (job["interval_s"] or job["cron"]):
                notice = (
                    f"{parsed.job} is in no summary now and has no schedule: it runs only when "
                    "asked (job_save interval_s or cron gives it one)."
                )
        return out | ({"notice": notice} if notice else {})

    async def job_run(args: dict[str, Any]) -> Any:
        slug = str(args.get("slug") or "")
        if await store.get(slug) is None:
            raise ToolError(f"no job {slug!r} (jobs_list shows them)")
        await audit("job_run", slug=slug)
        return _run_view(await app.jobs.run(slug, "llm"))

    return [
        AgentTool(
            "jobs_list",
            "The monitor's jobs (checks such as storage:usage or docker:status, and the "
            "summaries that run them in order) with their schedule, summaries and latest status.",
            {"type": "object", "properties": {}},
            jobs_list,
        ),
        AgentTool(
            "job_results",
            "The latest results of one job: status, summary and findings. Without a slug: the "
            "latest run of every enabled summary.",
            {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "Job id (default: every summary)."},
                    "limit": {"type": "integer", "description": "How many runs (1-10)."},
                },
            },
            job_results,
        ),
        AgentTool(
            "job_save",
            "Create a job (a check: slug, name, prompt and interval_s or cron, or summaries "
            "to join) or a summary (kind summary, slug, name, prompt, members and a schedule), "
            "or change one (slug and the fields to change, e.g. notify or enabled). A member "
            "of a summary has no schedule of its own: joining clears the job's schedule, and "
            "the result's notice says so. Only when the owner asks for it.",
            _schema(_SaveArgs),
            job_save,
        ),
        AgentTool(
            "job_delete",
            "Delete a custom job or summary the owner no longer wants (deleting a summary "
            "keeps its jobs). Builtins cannot be deleted: disable them with job_save "
            "enabled=false.",
            _slug_schema("Job id."),
            job_delete,
        ),
        AgentTool(
            "summary_member",
            "Put one job into a summary or take it out, in one call. add puts it at the end, "
            "or right after (or before) the member you name by slug; a job that is in it "
            "already moves there. A member has no schedule of its own: joining clears the "
            "job's schedule (the result's notice says so). Only when the owner asks for it.",
            _schema(_MemberArgs),
            summary_member,
        ),
        AgentTool(
            "job_run",
            "Run one job now and return its finished result (takes as long as the check).",
            _slug_schema("Job id."),
            job_run,
        ),
    ]
