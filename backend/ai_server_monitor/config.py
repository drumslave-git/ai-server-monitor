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


ToolMode = Literal["auto", "native", "json", "none"]


class LLMSettings(_Model):
    base_url: OptionalHttpUrl = ""
    """OpenAI-compatible API base URL, e.g. http://ollama:11434/v1, http://llama:8080/v1,
    https://openrouter.ai/api/v1. Empty = LLM not configured."""
    api_key: Secret = SecretStr("")
    """API key, if the endpoint needs one."""
    model: str = ""
    """Model name as the endpoint knows it, e.g. qwen2.5:14b or gpt-4o-mini."""
    tool_mode: ToolMode = "auto"
    """How the model calls tools in chat. auto = detect; native = OpenAI tool calls;
    json = JSON-in-text fallback for models without tool calling; none = no tools."""
    max_context_tokens: int = Field(default=8192, ge=1024)
    """Context window to plan prompts for. For Ollama, also raise OLLAMA_CONTEXT_LENGTH."""
    max_output_tokens: int = Field(default=1500, ge=64)
    """Maximum tokens per answer/report."""
    temperature: float = Field(default=0.2, ge=0, le=2)
    timeout: Duration = timedelta(minutes=3)
    """Per-request timeout. Local models on CPU can be slow."""
    max_retries: int = Field(default=2, ge=0, le=10)
    stream_usage: bool = True
    """Ask the endpoint for token usage while streaming (turned off automatically if
    the endpoint rejects it)."""
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """Extra provider-specific request fields (JSON), passed through verbatim."""

    @property
    def configured(self) -> bool:
        return bool(self.base_url.strip() and self.model.strip())


class ReportSettings(_Model):
    interval: Duration = timedelta(minutes=5)
    """How often the LLM writes a status report."""
    skip_if_unchanged: bool = False
    """Skip the LLM call when nothing relevant changed since the last report."""
    language: str = "en"
    """Language of reports and chat answers."""


class StorageSettings(_Model):
    retention: Duration = timedelta(days=30)
    """How long metrics history, reports and chats are kept. The action log is permanent."""
    sample_interval: Duration = timedelta(minutes=5)
    """Resolution of stored history. Live data is always fresh."""
    prune_interval: Duration = timedelta(hours=1)
    """How often old data is deleted."""


class CollectorSettings(_Model):
    enabled: bool | Literal["auto"] = True
    """auto = use it if available on this host, silently skip otherwise."""
    interval: Duration = timedelta(seconds=30)


class SystemCollectorSettings(CollectorSettings):
    interval: Duration = timedelta(seconds=15)
    top_n: int = Field(default=10, ge=1, le=50)
    """How many top processes (by CPU and by memory) to report."""


class DockerCollectorSettings(CollectorSettings):
    url: str = ""
    """Docker API, e.g. tcp://docker-proxy:2375 or unix:///var/run/docker.sock.
    Empty = $DOCKER_HOST (set by the compose file), else the local socket."""
    log_scan: bool = True
    """Count error lines in container logs since the last run."""
    log_tail: int = Field(default=300, ge=10, le=5000)
    """Max log lines read per container per run."""
    df_interval: Duration = timedelta(minutes=30)
    """How often to check Docker disk usage (images, volumes, build cache)."""


class SystemdCollectorSettings(CollectorSettings):
    interval: Duration = timedelta(minutes=1)
    bus_address: str = ""
    """D-Bus address. Empty = the host's system bus under the host mount."""


class JournalCollectorSettings(CollectorSettings):
    interval: Duration = timedelta(minutes=1)
    priority: Literal["emerg", "alert", "crit", "err", "warning", "notice", "info"] = "warning"
    """Lowest severity to read."""
    directory: str = ""
    """Journal directory. Empty = the host's /var/log/journal (or /run/log/journal)."""
    max_entries: int = Field(default=2000, ge=100, le=50000)
    """Max entries read per run."""
    initial_lookback: Duration = timedelta(hours=1)
    """How far back to read after a (re)start."""


class PromQuery(_Model):
    name: Name
    """Short identifier shown in reports, e.g. targets_down."""
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    """PromQL expression, evaluated as an instant query."""
    description: str = ""
    """What the result means, e.g. "Scrape targets that are down". Helps the report writer."""


class PrometheusCollectorSettings(CollectorSettings):
    enabled: bool | Literal["auto"] = "auto"
    """auto = on as soon as a URL is set."""
    interval: Duration = timedelta(minutes=1)
    url: OptionalHttpUrl = ""
    """Prometheus base URL, e.g. http://prometheus:9090. Empty = not used."""
    token: Secret = SecretStr("")
    """Bearer token, if Prometheus sits behind an authenticating proxy."""
    username: str = ""
    """Basic-auth user (used when no bearer token is set)."""
    password: Secret = SecretStr("")
    timeout: Duration = timedelta(seconds=10)
    """Per-query timeout."""
    max_series: int = Field(default=50, ge=1, le=1000)
    """Max series kept per query result; the rest is only counted."""
    queries: list[PromQuery] = Field(
        default_factory=lambda: [
            PromQuery(
                name="targets_down",
                query="up == 0",
                description="Scrape targets that are down",
            ),
            PromQuery(
                name="firing_alerts",
                query='ALERTS{alertstate="firing"}',
                description="Alerts currently firing in Prometheus",
            ),
        ]
    )
    """PromQL queries run on every collection and included in reports."""

    @field_validator("queries", mode="before")
    @classmethod
    def _legacy_queries(cls, value: Any) -> Any:
        # Before the query list existed this was a {name: expression} map.
        if isinstance(value, dict):
            return [{"name": k, "query": v} for k, v in value.items()]
        return value

    @field_validator("queries")
    @classmethod
    def _unique_names(cls, value: list[PromQuery]) -> list[PromQuery]:
        names = [q.name for q in value]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate query names: {', '.join(dupes)}")
        return value


class ImageUpdatesCollectorSettings(CollectorSettings):
    enabled: bool | Literal["auto"] = "auto"
    interval: Duration = timedelta(hours=12)
    url: str = ""
    """Docker API, e.g. tcp://docker-proxy:2375. Empty = $DOCKER_HOST (set by the compose
    file), else the local socket."""
    cache_ttl: Duration = timedelta(hours=6)
    """How long a registry answer is reused. Registries rate-limit, so don't set this low."""


class CollectorsSettings(_Model):
    system: SystemCollectorSettings = SystemCollectorSettings()
    """CPU, memory, disks, network, temperatures, top processes."""
    docker: DockerCollectorSettings = DockerCollectorSettings()
    """Containers: state, health, restarts, resource use, log errors, disk usage."""
    gpu: CollectorSettings = CollectorSettings(enabled="auto", interval=timedelta(seconds=30))
    """NVIDIA GPUs (needs the NVIDIA container runtime)."""
    systemd: SystemdCollectorSettings = SystemdCollectorSettings()
    """Failed units and restart loops."""
    journal: JournalCollectorSettings = JournalCollectorSettings()
    """Warnings and errors from the system journal."""
    apt: CollectorSettings = CollectorSettings(interval=timedelta(hours=6))
    """Pending package updates, security updates, reboot required."""
    image_updates: ImageUpdatesCollectorSettings = ImageUpdatesCollectorSettings()
    """Running containers whose image tag has a newer build in its registry (same tag, new
    digest; does not look for newer version numbers)."""
    prometheus: PrometheusCollectorSettings = PrometheusCollectorSettings()
    """Run PromQL queries against an existing Prometheus."""


class TelegramSettings(_Model):
    enabled: bool = False
    token: Secret = SecretStr("")
    """Bot token from @BotFather."""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    """Only these chats may talk to the bot."""
    report_every: int = Field(default=1, ge=0)
    """Send every Nth report (0 = alerts only)."""


class MetricsSettings(_Model):
    enabled: bool = True
    """Expose Prometheus metrics at /metrics."""


class OutputsSettings(_Model):
    telegram: TelegramSettings = TelegramSettings()
    metrics: MetricsSettings = MetricsSettings()


class ArrInstance(_Model):
    name: Name
    """Display name, e.g. radarr-4k."""
    url: HttpUrl
    """Base URL, e.g. http://radarr:7878."""
    api_key: Secret
    """Settings → General → API Key in the app."""

    @field_validator("api_key")
    @classmethod
    def _api_key_required(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("API key is required")
        return value


class QbitSettings(_Model):
    url: HttpUrl
    """WebUI URL, e.g. http://qbittorrent:8080."""
    username: str = "admin"
    password: Secret = SecretStr("")


Policy = Literal["off", "suggest", "approve", "auto"]


class ActionSettings(_Model):
    policy: Policy = "suggest"
    """off = never; suggest = mention in reports; approve = ask you first; auto = just do it."""


class StalledCleanupSettings(ActionSettings):
    policy: Policy = "approve"
    stalled_after: Duration = timedelta(hours=6)
    """A download with no progress for this long counts as stalled."""
    max_per_hour: int = Field(default=10, ge=1)


class SearchActionSettings(ActionSettings):
    interval: Duration = timedelta(hours=6)
    batch: int = Field(default=10, ge=1)
    """Items searched per run (keeps indexers from being hammered)."""


class ActionsSettings(_Model):
    stalled_cleanup: StalledCleanupSettings = StalledCleanupSettings()
    """Remove stalled/failed downloads, blocklist the release and search again."""
    missing_search: SearchActionSettings = SearchActionSettings()
    """Search for missing episodes/movies."""
    cutoff_search: SearchActionSettings = SearchActionSettings(
        interval=timedelta(hours=24), batch=5
    )
    """Search for quality upgrades (cutoff unmet)."""
    add_media: ActionSettings = ActionSettings(policy="approve")
    """Add movies/series when asked in chat."""
    indexer_triage: ActionSettings = ActionSettings()
    """Test/disable failing indexers in Prowlarr."""
    approval_ttl: Duration = timedelta(hours=24)
    """Pending approvals expire after this."""


class ArrSettings(_Model):
    dry_run: bool = True
    """Log what would be done without changing anything."""
    radarr: list[ArrInstance] = Field(default_factory=list)
    sonarr: list[ArrInstance] = Field(default_factory=list)
    whisparr: list[ArrInstance] = Field(default_factory=list)
    prowlarr: ArrInstance | None = None
    qbittorrent: QbitSettings | None = None
    actions: ActionsSettings = ActionsSettings()


class Settings(_Model):
    """Everything configurable from the web UI."""

    general: GeneralSettings = GeneralSettings()
    llm: LLMSettings = LLMSettings()
    report: ReportSettings = ReportSettings()
    storage: StorageSettings = StorageSettings()
    collectors: CollectorsSettings = CollectorsSettings()
    outputs: OutputsSettings = OutputsSettings()
    arr: ArrSettings = ArrSettings()

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
