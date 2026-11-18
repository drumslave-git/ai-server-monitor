"""Request and response shapes of the jobs API (also used by the chat's job tools)."""

from __future__ import annotations

from typing import Any, Literal

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]{1,39}$"
MIN_INTERVAL_S = 60
MAX_INTERVAL_S = 31 * 86400


def check_cron(expr: str) -> str:
    """A standard 5-field crontab expression (minute hour day month weekday)."""
    expr = " ".join(expr.split())
    if len(expr.split(" ")) != 5:
        raise ValueError("cron needs 5 fields: minute hour day-of-month month day-of-week")
    try:
        CronTrigger.from_crontab(expr)
    except ValueError as exc:
        raise ValueError(f"invalid cron expression: {exc}") from None
    return expr


class _JobFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    prompt: str | None = Field(default=None, min_length=10, max_length=8000)
    """The check: scope, typical commands, what counts as ok / warn / crit."""
    interval_s: int | None = Field(default=None, ge=MIN_INTERVAL_S, le=MAX_INTERVAL_S)
    """Run every N seconds. Give this or `cron`."""
    cron: str | None = None
    """Run on a crontab schedule (minute hour day month weekday, server time)."""
    model: Literal["worker", "main"] | None = None
    enabled: bool | None = None
    timeout_s: int | None = Field(default=None, ge=30, le=6 * 3600)
    """Longest a run may take; empty = Settings → Jobs → timeout."""
    max_steps: int | None = Field(default=None, ge=1, le=60)
    """Most tool-use rounds of a run; empty = Settings → Jobs → max steps."""
    domains: list[str] | None = None
    """MCP domains whose enabled tools the job's runs get (exactly those; empty = the shell
    only). Names of existing domains (MCP tools page)."""

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        return check_cron(value) if value is not None else None

    @model_validator(mode="after")
    def _one_schedule(self) -> _JobFields:
        if self.interval_s is not None and self.cron is not None:
            raise ValueError("give either interval_s or cron, not both")
        return self


class JobCreate(_JobFields):
    slug: str = Field(pattern=SLUG_PATTERN)
    """Unique id: 2-40 characters, lowercase letters, digits, - and _."""
    name: str = Field(min_length=1, max_length=80)
    prompt: str = Field(min_length=10, max_length=8000)
    model: Literal["worker", "main"] = "worker"
    enabled: bool = True

    @model_validator(mode="after")
    def _has_schedule(self) -> JobCreate:
        if self.interval_s is None and self.cron is None:
            raise ValueError("a job needs a schedule: interval_s or cron")
        return self


class JobUpdate(_JobFields):
    """Only the fields given are changed. Setting interval_s clears cron and vice versa."""


class Finding(BaseModel):
    severity: Literal["info", "warn", "crit"]
    title: str
    detail: str = ""
    evidence: list[str] = []
    """The line(s) of command output the finding rests on, as the model quoted them."""
    verified: bool | None = None
    """True: every evidence line occurs in the run's command output (and warn/crit gave some);
    False: not confirmed by command output; None: a run from before evidence existed."""


class TopicLine(BaseModel):
    slug: str
    status: Literal["ok", "warn", "crit", "error", "none"]
    """The status of the topic's latest run when the summary ran (none: it had not run)."""
    clause: str
    """The summary's one clause about it, with its key number."""


class JobRunMcp(BaseModel):
    domains: list[str]
    """The job's domains when it ran."""
    unreachable: dict[str, str] = {}
    """MCP servers that could not be reached: name → why."""
    unsorted: list[str] = []
    """Enabled tools of those servers without a domain (not given to the run)."""


class JobRun(BaseModel):
    id: int
    job_slug: str
    trigger: Literal["schedule", "manual", "llm"]
    started: float
    finished: float | None = None
    duration_s: float | None = None
    status: Literal["running", "ok", "warn", "crit", "error"]
    summary: str | None = None
    """Markdown."""
    findings: list[Finding] = []
    topics: list[TopicLine] = []
    """Summary runs: one entry per enabled topic job."""
    error: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    steps: int | None = None
    trace_turn: str | None = None
    """The run's LLM and tool calls on the Logs page (turn id)."""
    mcp: JobRunMcp | None = None
    """What the run had of MCP (None: the job has no domains)."""


class Job(BaseModel):
    slug: str
    name: str
    kind: Literal["topic", "summary"]
    prompt: str
    interval_s: int | None = None
    cron: str | None = None
    model: Literal["worker", "main"]
    enabled: bool
    builtin: bool
    edited: bool
    """A builtin the user changed (it no longer follows prompt updates of new releases)."""
    timeout_s: int | None = None
    max_steps: int | None = None
    domains: list[str] = []
    """MCP domains: its runs get exactly their enabled tools."""
    created_by: Literal["builtin", "user", "llm"]
    created: float
    updated: float
    next_run: float | None = None
    """When the scheduler runs it next (None: disabled or automation paused)."""
    last_run: JobRun | None = None
    """The newest finished run."""
    running_run_id: int | None = None
    """The run in progress right now (None: not running)."""


class JobList(BaseModel):
    items: list[Job]


class JobRunList(BaseModel):
    items: list[JobRun]


class RunStarted(BaseModel):
    run_id: int


def job_out(
    job: dict[str, Any],
    last: dict[str, Any] | None,
    next_run: float | None,
    running_run_id: int | None = None,
) -> Job:
    return Job.model_validate(
        {**job, "last_run": last, "next_run": next_run, "running_run_id": running_run_id}
    )
