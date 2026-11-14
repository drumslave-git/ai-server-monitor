"""Typed API responses (they become TypeScript types via the OpenAPI schema)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings

Severity = Literal["info", "warn", "crit"]
Health = Literal["ok", "warn", "crit"]
ReportStatus = Literal["ok", "failed", "skipped"]


class Finding(BaseModel):
    severity: Severity
    title: str
    detail: str = ""


class ReportSummary(BaseModel):
    """A report without its Markdown body (list view)."""

    id: int
    ts: float
    status: ReportStatus
    health: Health | None = None
    score: int | None = None
    summary: str | None = None
    findings: list[Finding] = []
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int | None = None
    error: str | None = None
    parsed: bool = True
    """False when the model's JSON header was unreadable and rule-based values were used."""


class Report(ReportSummary):
    markdown: str = ""
    digest: str | None = None
    """The data digest the model was given."""
    digest_hash: str | None = None


class ReportList(BaseModel):
    items: list[ReportSummary]
    total: int
    limit: int
    offset: int


class DigestSection(BaseModel):
    key: str
    title: str
    priority: int
    lines: int
    kept_lines: int


class Digest(BaseModel):
    text: str
    tokens: int
    hash: str
    generated: float
    flags: list[Finding]
    sections: list[DigestSection]


class RunStarted(BaseModel):
    status: Literal["started"] = "started"


class CollectorState(BaseModel):
    state: Literal["active", "disabled", "unavailable"]
    reason: str | None = None
    ok: bool | None = None
    error: str | None = None
    last_ts: float | None = None


class LatestReport(BaseModel):
    id: int
    ts: float
    health: Health | None
    score: int | None
    summary: str | None
    findings: list[Finding]


class LastAttempt(BaseModel):
    """The newest report attempt that did not produce a report (failed/skipped)."""

    ts: float
    status: ReportStatus
    error: str | None


class NextReport(BaseModel):
    running: bool
    next_run: float | None
    interval_s: float


class FullestDisk(BaseModel):
    mount: str | None = None
    percent: float | None = None


class GpuSummary(BaseModel):
    count: int
    max_util_percent: float
    max_vram_percent: float


class ContainersSummary(BaseModel):
    running: int | None = None
    total: int | None = None
    unhealthy: int = 0


class PendingUpdates(BaseModel):
    apt: int | None = None
    security: int | None = None
    reboot_required: bool = False
    images: int = 0
    """Container images with a newer build."""


class Resources(BaseModel):
    """Key numbers from the latest collector results; None where a collector has no data."""

    cpu_percent: float | None = None
    load_1m: float | None = None
    cores: int | None = None
    memory_percent: float | None = None
    swap_percent: float | None = None
    uptime_s: float | None = None
    fullest_disk: FullestDisk | None = None
    gpu: GpuSummary | None = None
    containers: ContainersSummary | None = None
    failed_units: int | None = None
    pending_updates: PendingUpdates | None = None


class StatusResponse(BaseModel):
    version: str
    uptime_s: float
    now: float
    report: LatestReport | None
    """The latest successful report; None before the first one."""
    last_attempt: LastAttempt | None
    """Set when the newest attempt failed/was skipped (e.g. LLM not configured)."""
    next_report: NextReport
    llm_configured: bool
    collectors: dict[str, CollectorState]
    resources: Resources


class SettingsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    values: Settings
    """Current settings; secrets are masked."""
    json_schema: dict[str, Any] = Field(alias="schema")
    """JSON schema the settings form is generated from."""
    sections: dict[str, str]


class SettingsValues(BaseModel):
    values: Settings
