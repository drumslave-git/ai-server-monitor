"""Typed API responses (they become TypeScript types via the OpenAPI schema)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings


class SummaryState(BaseModel):
    """One enabled summary and its newest finished run."""

    slug: str
    name: str
    status: Literal["ok", "warn", "crit", "error"] | None
    """None: it has not run yet."""
    headline: str
    """The first line of its summary (or the error); empty when it has not run."""
    finished: float | None
    run_id: int | None


class OverallStatus(BaseModel):
    """The enabled summaries: the worst of their newest runs, and each one's own state."""

    status: Literal["ok", "warn", "crit", "error"]
    """The worst status of the enabled summaries' newest runs (ok < warn < error < crit)."""
    summaries: list[SummaryState]
    """Every enabled summary, in the order of the jobs list (those not run yet too)."""


class JobStatus(BaseModel):
    slug: str
    name: str
    kind: Literal["topic", "summary"]
    enabled: bool
    status: Literal["ok", "warn", "crit", "error"] | None
    """Of the newest finished run; None = never ran."""
    finished: float | None
    run_id: int | None
    running_run_id: int | None = None
    """The run in progress right now (None: not running)."""


class ChangeShellStatus(BaseModel):
    """Commands that change the host (the chat's `bash_write`)."""

    policy: Literal["off", "approve", "auto"]
    """Settings → Approvals → shell changes."""
    available: bool
    """The container can run them (it has SYS_ADMIN and SYS_PTRACE)."""
    offered: bool
    """The chat has the tool now: policy not off and available."""
    reason: str | None
    """Why it is not available, with the compose lines that enable it."""


class StatusResponse(BaseModel):
    version: str
    uptime_s: float
    now: float
    llm_configured: bool
    main_model: str
    worker_model: str
    """The worker model's name (the main model's when no own worker model is set)."""
    pending_approvals: int
    """Actions waiting for a human decision (GET /api/approvals lists them)."""
    automation: dict[str, Any]
    """The global pause switch (GET /api/automation)."""
    overall: OverallStatus | None = None
    """The enabled summaries; None until one of them has run."""
    jobs: list[JobStatus] = []
    change_shell: ChangeShellStatus | None = None
    """Whether the chat can run approved changing commands, and why not."""
    """The newest finished run of every job (GET /api/jobs has the details)."""


class GpuUse(BaseModel):
    """One NVIDIA GPU as `nvidia-smi` lists it now."""

    name: str
    util_percent: float
    vram_used: int
    """Bytes."""
    vram_total: int
    """Bytes."""


class HardwareResponse(BaseModel):
    """What the hardware is doing now (read anew on every request, nothing stored)."""

    cpu_percent: float
    """All CPUs together, since the previous request."""
    ram_used: int
    """Bytes: total minus available."""
    ram_total: int
    """Bytes."""
    gpus: list[GpuUse]
    """Empty when no NVIDIA GPU can be read."""


class SettingsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    values: Settings
    """Current settings; secrets are masked."""
    json_schema: dict[str, Any] = Field(alias="schema")
    """JSON schema the settings form is generated from."""
    sections: dict[str, str]


class SettingsValues(BaseModel):
    values: Settings


class ModelInfoResult(BaseModel):
    """What the selected LLM model says about itself (Ollama only), for Settings → Main model."""

    available: bool
    """False when the endpoint is not Ollama, is unreachable, or no model is set."""
    parameters: dict[str, float] = Field(default_factory=dict)
    """The model's own sampling defaults, e.g. temperature, top_k, top_p."""
    capabilities: list[str] = Field(default_factory=list)
    """e.g. completion, vision, tools, thinking."""
    context_length: int | None = None
    """The model's maximum context, in tokens. Information only."""


class ConnectionTestResult(BaseModel):
    """Outcome of a Settings → Test button."""

    ok: bool
    message: str
    """What to tell the user: what answered, or what is wrong, in plain words."""
    details: dict[str, Any] = Field(default_factory=dict)
    """Structured extras: app, version, latency_ms, per-chat results, ..."""
