"""Typed API responses (they become TypeScript types via the OpenAPI schema)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings


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
