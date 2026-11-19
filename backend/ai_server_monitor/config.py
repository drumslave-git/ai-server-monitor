"""Configuration.

Two layers:

* `Bootstrap` — the few things the container needs before it can even start (data
  directory, port, where the host root is mounted). Read from `ASM_*` environment
  variables; the image defaults are right for the standard compose file, so normally
  nothing needs setting.
* `Settings` — everything else. Stored in the database and edited in the web UI
  (Settings page / `PUT /api/settings`), applied live. Field docstrings become the help
  text in the UI.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    SerializationInfo,
    StringConstraints,
    WithJsonSchema,
    field_validator,
    model_validator,
)
from pydantic.json_schema import GenerateJsonSchema
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_MASK = "********"
"""What the API returns for a secret that is set. Sending it back means "unchanged"."""

# --------------------------------------------------------------------------- durations

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DURATION_PATTERN = r"^\s*(\d+(\.\d+)?\s*(ms|s|m|h|d|w)\s*)+$|^\s*\d+(\.\d+)?\s*$"


def parse_duration(value: Any) -> timedelta:
    """Parse '15s', '5m', '1h30m', '30d', plain seconds (int/float/'90'), or a timedelta."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError("invalid duration: boolean")
    if isinstance(value, int | float):
        return timedelta(seconds=value)
    if not isinstance(value, str):
        raise ValueError(f"invalid duration: {value!r}")
    text = value.strip().replace(" ", "")
    if not text:
        raise ValueError("invalid duration: empty")
    try:
        return timedelta(seconds=float(text))
    except ValueError:
        pass
    pos, total = 0, 0.0
    for match in _DURATION_PART.finditer(text):
        if match.start() != pos:
            raise ValueError(f"invalid duration {value!r} (use e.g. 30s, 5m, 6h, 30d, 1h30m)")
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
        pos = match.end()
    if pos != len(text):
        raise ValueError(f"invalid duration {value!r} (use e.g. 30s, 5m, 6h, 30d, 1h30m)")
    return timedelta(seconds=total)


def format_duration(value: timedelta) -> str:
    """Inverse of parse_duration: 300s -> '5m', 5400s -> '1h30m', 0.5s -> '500ms'."""
    ms = round(value.total_seconds() * 1000)
    if ms == 0:
        return "0s"
    parts: list[str] = []
    for unit, size in (("d", 86_400_000), ("h", 3_600_000), ("m", 60_000), ("s", 1000)):
        if ms >= size:
            count, ms = divmod(ms, size)
            parts.append(f"{count}{unit}")
    if ms:
        parts.append(f"{ms}ms")
    return "".join(parts)


Duration = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, return_type=str, when_used="json"),
    WithJsonSchema(
        {
            "type": "string",
            "format": "duration",
            "pattern": DURATION_PATTERN,
            "examples": ["30s", "5m", "6h", "30d"],
        }
    ),
]


def _serialize_secret(value: SecretStr, info: SerializationInfo) -> str:
    raw = value.get_secret_value()
    if (info.context or {}).get("reveal_secrets"):
        return raw
    return SECRET_MASK if raw else ""


Secret = Annotated[
    SecretStr,
    PlainSerializer(_serialize_secret, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "password", "writeOnly": True}),
]

HttpUrl = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^https?://\S+$")]
OptionalHttpUrl = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^(https?://\S+)?$")
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# --------------------------------------------------------------------------- bootstrap


class Bootstrap(BaseSettings):
    """Startup parameters from the environment (ASM_DATA_DIR, ASM_PORT, ...)."""

    model_config = SettingsConfigDict(env_prefix="ASM_", extra="ignore")

    data_dir: Path = Path("/data")
    host: str = "0.0.0.0"
    port: int = 8080
    host_root: Path = Path("/host")
    """Where the host's / is mounted read-only. Falls back to / when it doesn't exist."""
    static_dir: Path | None = None
    """Built web UI to serve at /. Set in the image."""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"


# --------------------------------------------------------------------------- settings


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_attribute_docstrings=True)


class GeneralSettings(_Model):
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    """Log verbosity of the service."""
    language: str = "en"
    """Language of chat answers and Telegram messages."""
    docker_url: str = ""
    """Docker API the shell's `docker` commands use, e.g. tcp://docker-proxy:2375 or
    unix:///var/run/docker.sock. Empty = $DOCKER_HOST (set by the compose file), else the local
    socket."""
    timezone: str = ""
    """Time zone of cron schedules, e.g. Europe/Berlin. Empty = the host's own time zone (its
    /etc/timezone or /etc/localtime), UTC when that cannot be read."""

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        value = value.strip()
        if value:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown time zone {value!r}") from exc
        return value


class ApprovalsSettings(_Model):
    """Actions waiting for your decision (the Approvals page, Telegram buttons)."""

    approval_ttl: Duration = timedelta(hours=24)
    """Pending approvals expire after this."""
    shell_changes: Literal["off", "approve", "auto"] = "approve"
    """Commands that change the host, which the chat proposes when you ask for a change
    (restart a container, delete files): approve = each one waits for your approval (here or
    in Telegram) and the chat continues with the result; auto = they run without asking;
    off = the chat cannot change anything. They run as root on the host, which needs
    `cap_add: [SYS_ADMIN, SYS_PTRACE]` in compose.yaml (commented out in the example);
    without it the chat only reads, and the status says so."""


ReasoningEffort = Literal["auto", "none", "low", "medium", "high"]


class LLMSettings(_Model):
    """The big model: it answers chat questions and decides when to hand a sub-task to the
    worker model. It must support native tool calling (OpenAI `tools`)."""

    base_url: OptionalHttpUrl = ""
    """OpenAI-compatible API base URL, e.g. http://ollama:11434/v1, http://llama:8080/v1,
    https://openrouter.ai/api/v1. Empty = LLM not configured."""
    api_key: Secret = SecretStr("")
    """API key, if the endpoint needs one."""
    model: str = ""
    """Model name as the endpoint knows it, e.g. qwen2.5:14b or gpt-4o-mini."""
    max_output_tokens: int = Field(default=16384, ge=64)
    """Maximum tokens per answer, counting a thinking model's reasoning too. Too low and
    a model that thinks long runs out before it writes the answer (shown as an error)."""
    reasoning_effort: ReasoningEffort = "auto"
    """How much a thinking model reasons before answering chat questions, sent as the API's
    reasoning_effort (left out from then on if the endpoint rejects it). auto = not sent, the
    model decides; none = no thinking (fast, shallower); low / medium / high = as the model
    supports. Short helper calls (chat titles, tests) always ask for none."""
    temperature: Annotated[float, Field(ge=0, le=2)] | Literal["auto"] = "auto"
    """Randomness of answers: 0 is the most predictable, higher is more varied. auto = not sent,
    so the model's own default applies (for Ollama models it is shown above)."""
    top_p: Annotated[float, Field(gt=0, le=1)] | Literal["auto"] = "auto"
    """Nucleus sampling: only the most likely words that together cover this share of the
    probability are considered. auto = not sent, the model's own default applies."""
    top_k: Annotated[int, Field(ge=0)] | Literal["auto"] = "auto"
    """Only the this-many most likely words are considered at each step (0 = no limit). auto =
    not sent, the model's own default applies. Not every endpoint honours it."""
    timeout: Duration = timedelta(minutes=3)
    """Per-request timeout. Local models on CPU can be slow."""
    max_retries: int = Field(default=2, ge=0, le=10)
    """How many times a failed model request is retried before giving up."""
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """Extra provider-specific request fields (JSON), passed through verbatim."""
    structured_output: Literal["json_schema", "tool_call"] = "json_schema"
    """How the short helper calls get their structured answer: picking the MCP domains a chat
    question needs (worker model), marking the commands of an answer read-only or write and
    sorting MCP tools into domains (main model). json_schema = the request carries the JSON
    schema (response_format) and the endpoint holds the model to it; tool_call = the model is
    asked to call a final_result tool, which small models sometimes skip by answering in
    prose. Pick tool_call if the endpoint rejects response_format (the Test button says so).
    Chat answers and job runs are not affected."""

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())


class WorkerLLMSettings(LLMSettings):
    """A smaller, faster model for sub-tasks the main model hands off (looking through the
    host with the shell). Empty model = the main model does them too. Empty base URL = the
    main model's endpoint and API key with this model name (e.g. a second Ollama model)."""


class JobsSettings(_Model):
    """Scheduled checks by the language model: one topic each (storage, journal, docker, ...) on
    the worker model, and a summary of them all on the main model. The jobs themselves are on
    the Jobs page."""

    max_concurrent_runs: int = Field(default=1, ge=1, le=8)
    """How many job runs may use the models at the same time. 1 suits one local GPU: runs that
    come due together wait for each other."""
    timeout: Duration = timedelta(minutes=15)
    """Longest one job run may take (a job can set its own). A run over it is stored as error."""
    max_steps: int = Field(default=12, ge=1, le=60)
    """Most tool-use rounds of one job run (a job can set its own)."""


class ChatSettings(_Model):
    max_steps: int = Field(default=15, ge=1, le=60)
    """Most tool-use rounds of the main model per question before it must answer with what it
    has. A host question usually takes 3 to 8 shell commands; the rest is room for a check."""
    worker_max_steps: int = Field(default=20, ge=1, le=60)
    """Most tool-use rounds of one sub-task on the worker model. Higher than the main model's:
    a small model exploring with the shell needs more tries, and its steps are cheap."""
    command_timeout: Duration = timedelta(minutes=5)
    """How long one shell command may run when the model does not ask for longer (it may ask
    for up to the tool time limit below, e.g. for a disk walk)."""
    history_messages: int = Field(default=12, ge=0, le=100)
    """How many earlier messages of a conversation the model sees."""
    max_tool_result_chars: int = Field(default=6000, ge=500, le=50000)
    """Longest tool result passed to the model; bigger results are trimmed."""
    tool_timeout: Duration = timedelta(hours=1)
    """Longest one tool call (a disk scan, a command, an API call) may run before the chat
    gives up on it. The answer waits for the tool; the chat shows what is running and for
    how long."""


class StorageSettings(_Model):
    retention: Duration = timedelta(days=30)
    """How long chat messages and job runs are kept. The action log is permanent."""
    prune_interval: Duration = timedelta(hours=1)
    """How often old data is deleted."""


class LogsSettings(_Model):
    """The Logs page: what is kept of every request to the language model, and of the app's own
    log."""

    llm_trace: bool = True
    """Record every request to the language model: what was sent, what came back, how long it
    took. Shown on the Logs page. Off = new calls are not recorded."""
    store_bodies: bool = True
    """Keep the full text of prompts, tool definitions, answers and reasoning. Off = only the
    numbers are kept (timings, token counts, status), and the page cannot show what was sent.
    Passwords, API keys and tokens are removed from stored text either way."""
    retention: Duration = timedelta(days=7)
    """How long recorded calls and saved warnings stay."""
    max_total_mb: int = Field(default=200, ge=10, le=10_000)
    """Upper limit for the recorded calls on disk; the oldest go first when it is exceeded."""
    keep_app_log: bool = True
    """Save warnings and errors of the app log in the database so they survive a restart.
    The Logs page always shows the newest few thousand lines since the last start."""


class TelegramSettings(_Model):
    enabled: bool = False
    """Turn the Telegram bot on (needs a bot token and at least one allowed chat id)."""
    token: Secret = SecretStr("")
    """Bot token from @BotFather."""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    """Only these chats may talk to the bot. Don't know a chat id? Send /id to the bot: it
    answers in any chat."""
    new_session_after: Duration = timedelta(hours=6)
    """Start a fresh chat after this long without a message in a Telegram chat (the old one
    stays in the Chat list on the web page). 0 = never: one chat until you send /new."""
    send_summary: Literal["changed", "every", "never"] = "changed"
    """When to send the result of the Summary job to the allowed chats (status, headline and
    the warnings). changed = when the overall status or the list of warnings differs from the
    last summary; every = after every summary run; never = only on request (/summary)."""
    api_url: OptionalHttpUrl = ""
    """Bot API server. Empty = https://api.telegram.org."""
    web_url: OptionalHttpUrl = ""
    """Address of this web UI as you open it, e.g. https://monitor.example.com. Approval
    messages get a "Review" button that opens the approval there. Empty = no button."""


class MetricsSettings(_Model):
    enabled: bool = True
    """Expose Prometheus metrics at /metrics."""
    bearer_token: Secret = SecretStr("")
    """If set, scrapers must send "Authorization: Bearer <token>" (Prometheus: authorization
    in the scrape config). Empty = open to anyone who can reach the port."""


class OutputsSettings(_Model):
    telegram: TelegramSettings = Field(
        default=TelegramSettings(),
        json_schema_extra={"test": "telegram", "test_modes": {"send": "Send test message"}},
    )
    metrics: MetricsSettings = MetricsSettings()


# --------------------------------------------------------------------------- mcp

McpTransport = Literal["auto", "streamable_http", "sse"]
Policy = Literal["suggest", "approve", "auto"]
ToolPolicy = Literal["inherit", "suggest", "approve", "auto"]
ToolKindSetting = Literal["auto", "read", "write"]

ServerName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,23}$")]
"""Used in tool names (`<server>__<tool>`), so: letters, digits and '-', no underscores."""


def pattern_problem(err: Any) -> str:
    """Plain words for a `string_pattern_mismatch` of a pydantic error (needs its input):
    a server name and a URL have different rules."""
    field = next((p for p in reversed(err["loc"]) if isinstance(p, str)), "")
    if field == "name":
        if not str(err.get("input") or "").strip():
            return "is required"
        return "may only use letters, digits and '-' (up to 24, starting with a letter or digit)"
    return "must start with http:// or https://"


class McpHeader(_Model):
    name: Name
    """Header name, e.g. X-Api-Key."""
    value: Secret
    """Header value (kept secret, like the token)."""


class McpToolOverride(_Model):
    name: Name
    """Tool name as the server calls it."""
    policy: ToolPolicy = "inherit"
    """inherit = the server's default policy; suggest / approve / auto as for the server (only
    used for tools that change things)."""
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
    """auto = chosen by the URL: a path ending in /sse is the older HTTP+SSE, anything else
    Streamable HTTP (nothing is tried first)."""
    token: Secret = SecretStr("")
    """Bearer token sent as `Authorization: Bearer ...`, if the server needs one."""
    headers: list[McpHeader] = Field(default_factory=list)
    """Extra HTTP headers (for servers that want an API key header instead of a token)."""
    timeout: Duration = timedelta(seconds=30)
    """Connect timeout and per-call timeout."""
    tool_filter: list[str] = Field(default_factory=list)
    """The tools the agents may use. Empty = none: pick them after pressing Test."""
    default_policy: Policy = "approve"
    """For tools that change things: suggest = only record the request; approve = ask you
    first (UI / Telegram); auto = just do it. Read-only tools always run."""
    tool_overrides: list[McpToolOverride] = Field(default_factory=list)
    """Per-tool exceptions to the policy or to the read-only detection."""

    @model_validator(mode="before")
    @classmethod
    def _policy_off_is_gone(cls, data: Any) -> Any:
        """Settings saved before 0.8 may say `off`: a server's `off` (writes never run) is now
        `suggest` (writes only recorded); a tool's `off` (not offered) is the tool unchosen."""
        if not isinstance(data, dict):
            return data
        if data.get("default_policy") == "off":
            data = {**data, "default_policy": "suggest"}
        overrides = data.get("tool_overrides")
        if isinstance(overrides, list):
            off = {
                o.get("name") for o in overrides if isinstance(o, dict) and o.get("policy") == "off"
            }
            if off:
                data = {
                    **data,
                    "tool_filter": [t for t in data.get("tool_filter") or [] if t not in off],
                    "tool_overrides": [
                        {**o, "policy": "inherit"}
                        if isinstance(o, dict) and o.get("name") in off
                        else o
                        for o in overrides
                    ],
                }
        return data

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


class Settings(_Model):
    """Everything configurable from the web UI."""

    general: GeneralSettings = GeneralSettings()
    llm: LLMSettings = LLMSettings()
    worker: WorkerLLMSettings = WorkerLLMSettings()
    chat: ChatSettings = ChatSettings()
    jobs: JobsSettings = JobsSettings()
    storage: StorageSettings = StorageSettings()
    logs: LogsSettings = LogsSettings()
    outputs: OutputsSettings = OutputsSettings()
    mcp: McpSettings = McpSettings()
    approvals: ApprovalsSettings = ApprovalsSettings()

    def public_dict(self) -> dict[str, Any]:
        """JSON for the API: secrets masked."""
        return self.model_dump(mode="json")

    def storage_dict(self) -> dict[str, Any]:
        """JSON for the database: secrets in clear text."""
        return self.model_dump(mode="json", context={"reveal_secrets": True})


class _SettingsSchema(GenerateJsonSchema):
    """Encode schema defaults the way the API encodes values ('5m', not 'PT5M')."""

    def encode_default(self, dft: Any) -> Any:
        if isinstance(dft, timedelta):
            return format_duration(dft)
        if isinstance(dft, BaseModel):
            return dft.model_dump(mode="json")
        return super().encode_default(dft)


def settings_json_schema() -> dict[str, Any]:
    return Settings.model_json_schema(schema_generator=_SettingsSchema)
