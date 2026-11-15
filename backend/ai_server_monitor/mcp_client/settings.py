"""Settings of the MCP client: which servers, which of their tools, and what policy governs them.

These models are written to move into `config.py` as `Settings.mcp` unchanged (they only
use the helper types that live there); until then the package owns them.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator

from ..config import Duration, HttpUrl, Name, Policy, Secret, _Model

McpTransport = Literal["auto", "streamable_http", "sse"]
ToolPolicy = Literal["inherit", "off", "suggest", "approve", "auto"]
ToolKindSetting = Literal["auto", "read", "write"]

ServerName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,23}$")]
"""Used in tool names (`<server>__<tool>`), so: letters, digits and '-', no underscores."""


class McpHeader(_Model):
    name: Name
    """Header name, e.g. X-Api-Key."""
    value: Secret
    """Header value (kept secret, like the token)."""


class McpToolOverride(_Model):
    name: Name
    """Tool name as the server calls it."""
    policy: ToolPolicy = "inherit"
    """inherit = the server's default policy; off = the chat does not get this tool at all;
    suggest / approve / auto as for the server (only used for tools that change things)."""
    kind: ToolKindSetting = "auto"
    """auto = read-only only if the server says so; read / write = your own call. A tool
    treated as read runs immediately; a write goes through the action policy."""


class McpServerSettings(_Model):
    name: ServerName
    """Short name shown in chat and in tool names, e.g. home-assistant."""
    enabled: bool = True
    """Off = never connected, its tools are not offered."""
    url: HttpUrl
    """MCP endpoint, e.g. http://homeassistant.local:8123/mcp_server/sse or
    https://grafana.example/mcp. Remote servers only (Streamable HTTP or the older HTTP+SSE);
    servers that run as a local program (stdio) are not supported."""
    transport: McpTransport = "auto"
    """auto = try Streamable HTTP, fall back to the older HTTP+SSE."""
    token: Secret = SecretStr("")
    """Bearer token sent as `Authorization: Bearer ...`, if the server needs one."""
    headers: list[McpHeader] = Field(default_factory=list)
    """Extra HTTP headers (for servers that want an API key header instead of a token)."""
    timeout: Duration = timedelta(seconds=30)
    """Connect timeout and per-call timeout."""
    tool_filter: list[str] = Field(default_factory=list)
    """The tools the chat may use. Empty = none: pick them after pressing Test (small models
    cope badly with dozens of tools, so choose only what you need)."""
    default_policy: Policy = "approve"
    """For tools that change things: off = never; suggest = only record the request;
    approve = ask you first (UI / Telegram); auto = just do it. Read-only tools always run."""
    tool_overrides: list[McpToolOverride] = Field(default_factory=list)
    """Per-tool exceptions to the policy or to the read-only detection."""

    @field_validator("tool_filter")
    @classmethod
    def _dedupe(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(v.strip() for v in value if v.strip()))

    @model_validator(mode="after")
    def _unique_overrides(self) -> McpServerSettings:
        names = [o.name for o in self.tool_overrides]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate tool overrides: {', '.join(dupes)}")
        return self

    def override(self, tool: str) -> McpToolOverride | None:
        return next((o for o in self.tool_overrides if o.name == tool), None)

    def policy_of(self, tool: str) -> Policy:
        """The policy that governs calls of `tool` that change things."""
        override = self.override(tool)
        if override is not None and override.policy != "inherit":
            return override.policy
        return self.default_policy

    def request_headers(self) -> dict[str, str]:
        """Headers to send, secrets in clear. Never log or return the result."""
        out: dict[str, str] = {}
        token = self.token.get_secret_value().strip()
        if token:
            out["Authorization"] = f"Bearer {token}"
        for header in self.headers:
            out[header.name] = header.value.get_secret_value()
        return out

    def secret_values(self) -> list[str]:
        values = [
            self.token.get_secret_value(),
            *(h.value.get_secret_value() for h in self.headers),
        ]
        return [v.strip() for v in values if len(v.strip()) >= 6]


class McpSettings(_Model):
    servers: list[McpServerSettings] = Field(
        default_factory=list, json_schema_extra={"test": "mcp"}
    )
    """MCP servers whose tools the chat may use (tools only: prompts and resources of a
    server are not used)."""
    max_inline_tool_tokens: int = Field(default=1200, ge=0)
    """If the schemas of all chosen tools together are bigger than this (about 4 characters
    per token), the chat gets two generic tools (list tools / call tool) instead of one tool
    per MCP tool. Small models do better with fewer schemas. 0 = always use the generic pair."""
    dry_run: bool = False
    """Pretend: record write calls in the action log but send nothing to the servers."""

    @model_validator(mode="after")
    def _unique_names(self) -> McpSettings:
        names = [s.name.lower() for s in self.servers]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate server names: {', '.join(dupes)}")
        return self

    def server(self, name: str) -> McpServerSettings | None:
        return next((s for s in self.servers if s.name == name), None)
