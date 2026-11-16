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
    model_validator,
)
from pydantic.json_schema import GenerateJsonSchema
from pydantic_settings import BaseSettings, SettingsConfigDict

from .thresholds import normalize_threshold

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
ReasoningEffort = Literal["auto", "none", "low", "medium", "high"]


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
    max_output_tokens: int = Field(default=16384, ge=64)
    """Maximum tokens per answer/report, counting a thinking model's reasoning too. Too low and
    a model that thinks long runs out before it writes the answer (shown as an error)."""
    reasoning_effort: ReasoningEffort = "auto"
    """How much a thinking model reasons before answering chat questions and reports, sent as
    the API's reasoning_effort. auto = not sent, the model decides; none = no thinking (fast,
    shallower); low / medium / high = as the model supports. Short helper calls (chat titles,
    tool routing, tests) always ask for none."""
    temperature: float = Field(default=0.2, ge=0, le=2)
    """Randomness of answers: 0 is the most predictable, higher is more varied."""
    timeout: Duration = timedelta(minutes=3)
    """Per-request timeout. Local models on CPU can be slow."""
    max_retries: int = Field(default=2, ge=0, le=10)
    """How many times a failed model request is retried before giving up."""
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
    digest_token_budget: int = Field(default=3000, ge=500, le=100_000)
    """Size limit (in tokens, ~4 characters each) of the pre-analyzed data digest the model
    reads. Over the limit, the least important sections are shortened or dropped first.
    Raise it for models with a big context, lower it for small ones."""
    digest_top_n: int = Field(default=5, ge=1, le=20)
    """How many top processes, containers and disk writers the digest lists."""
    trend_window: Duration = timedelta(days=7)
    """How much stored history the digest uses for trends (disk growth, restarts)."""
    disk_warn_percent: float = Field(default=85, ge=1, le=100)
    """A filesystem fuller than this is flagged as a warning."""
    disk_crit_percent: float = Field(default=95, ge=1, le=100)
    """A filesystem fuller than this is flagged as critical."""
    disk_full_warn_days: float = Field(default=14, gt=0)
    """Warn when a disk's growth trend predicts it will be full within this many days."""
    disk_full_crit_days: float = Field(default=3, gt=0)
    """Critical when a disk is predicted to be full within this many days."""
    memory_warn_percent: float = Field(default=90, ge=1, le=100)
    """Memory use (excluding cache) above this is a warning."""
    memory_crit_percent: float = Field(default=97, ge=1, le=100)
    """Memory use (excluding cache) above this is critical."""
    swap_warn_percent: float = Field(default=50, ge=1, le=100)
    """Swap use above this is a warning."""
    load_warn_per_core: float = Field(default=1.5, gt=0)
    """5-minute load average per CPU core above this is a warning."""
    load_crit_per_core: float = Field(default=3.0, gt=0)
    """5-minute load average per CPU core above this is critical."""
    restart_loop_count: int = Field(default=3, ge=1)
    """A container or service restarted this many times within a day counts as a restart loop."""
    log_error_burst: int = Field(default=20, ge=1)
    """This many error lines (journal in one report interval, or one container's log in one
    check) count as an error burst."""
    gpu_temp_warn_c: float = Field(default=85, ge=30)
    """A GPU hotter than this is a warning."""
    unused_images_note_gb: float = Field(default=20, ge=0)
    """Suggest pruning when this many GB of Docker images are unused while the disk holding
    Docker data is above the disk warning level."""


class ChatSettings(_Model):
    max_steps: int = Field(default=6, ge=1, le=20)
    """Most tool-use rounds per question before the model must answer with what it has."""
    history_messages: int = Field(default=12, ge=0, le=100)
    """How many earlier messages of a conversation the model sees."""
    max_tool_result_chars: int = Field(default=6000, ge=500, le=50000)
    """Longest tool result passed to the model; bigger results are trimmed."""
    tool_timeout: Duration = timedelta(hours=1)
    """Longest one tool call (a disk scan, a command, an API call) may run before the chat
    gives up on it. The answer waits for the tool; the chat shows what is running and for
    how long."""
    shell: bool = True
    """Let the chat run read-only commands on the host (journalctl, systemctl status, ss, df,
    ps, ...) to answer questions. They run in a sandbox as an unprivileged user and can change
    nothing. Off = the chat only sees the collected data."""
    disk_cache_ttl: Duration = timedelta(minutes=10)
    """How long a finished disk scan of a path is reused."""
    tool_routing: Literal["auto", "always", "off"] = "auto"
    """Send the model only the tool groups a question needs (host, media, metrics, containers,
    actions, one per MCP server). A short model call picks the groups before the answer; the
    model can load another group itself. auto = only when the full tool definitions are bigger
    than the token limit below (strong models cope with all tools); always = every question;
    off = every tool every time."""
    tool_routing_tokens: int = Field(default=2000, ge=200, le=100_000)
    """With tool routing auto: the size of the tool definitions (about 4 characters per token)
    above which routing is used."""


class StorageSettings(_Model):
    retention: Duration = timedelta(days=30)
    """How long metrics history, reports and chat messages are kept. The action log is permanent."""
    sample_interval: Duration = timedelta(minutes=5)
    """Resolution of stored history. Live data is always fresh."""
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


class BenchJudgeSettings(_Model):
    """A second model that grades the answers of a benchmark run."""

    enabled: bool = False
    """Grade every answer 1-5 against the scenario's rubric. The grade is shown next to the rule
    score and never mixed into it. Costs one extra model call per graded attempt."""
    base_url: OptionalHttpUrl = ""
    """OpenAI-compatible API base URL of the judge, e.g. https://api.openai.com/v1."""
    api_key: Secret = SecretStr("")
    """API key of the judge, if its endpoint needs one."""
    model: str = ""
    """Model name of the judge."""

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url.strip() and self.model.strip())


class BenchSettings(_Model):
    """Model benchmarks (the Benchmarks page): fixed scenarios run against the model chosen in
    LLM. To compare models, switch the model there and run again."""

    repeats: int = Field(default=3, ge=1, le=20)
    """How many times every scenario runs. More runs smooth out the model's randomness."""
    attempt_timeout: Duration = timedelta(minutes=5)
    """Time budget of one attempt; a slower one counts as failed."""
    record_hardware: bool = True
    """Record the GPU, VRAM, power, CPU and RAM load of this host while a run goes, and show it
    with the results. Read from the same sources as the dashboard."""
    hardware_interval: Duration = timedelta(seconds=1)
    """How often the load is sampled during a run (between 500ms and 10s)."""
    judge: BenchJudgeSettings = BenchJudgeSettings()


class CollectorSettings(_Model):
    enabled: bool | Literal["auto"] = True
    """auto = use it if available on this host, silently skip otherwise."""
    interval: Duration = timedelta(seconds=30)
    """How often this collector gathers fresh data."""


class SystemCollectorSettings(CollectorSettings):
    interval: Duration = timedelta(seconds=15)
    """How often this collector gathers fresh data."""
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
    """How often this collector gathers fresh data."""
    bus_address: str = ""
    """D-Bus address. Empty = the host's system bus under the host mount."""


class JournalCollectorSettings(CollectorSettings):
    interval: Duration = timedelta(minutes=1)
    """How often this collector gathers fresh data."""
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
    unit: str = Field(default="", max_length=12)
    """Unit of the value, e.g. %, bytes, seconds. Optional."""
    warn_if: str | None = None
    """Warning when a value crosses this: an operator and a number, e.g. > 80 or == 0."""
    crit_if: str | None = None
    """Critical when a value crosses this, same form as the warning."""

    @field_validator("warn_if", "crit_if")
    @classmethod
    def _threshold(cls, value: str | None) -> str | None:
        return normalize_threshold(value)


class PrometheusCollectorSettings(CollectorSettings):
    enabled: bool | Literal["auto"] = "auto"
    """auto = on as soon as a URL is set."""
    interval: Duration = timedelta(minutes=1)
    """How often this collector gathers fresh data."""
    url: OptionalHttpUrl = ""
    """Prometheus base URL, e.g. http://prometheus:9090. Empty = not used."""
    token: Secret = SecretStr("")
    """Bearer token, if Prometheus sits behind an authenticating proxy."""
    username: str = ""
    """Basic-auth user (used when no bearer token is set)."""
    password: Secret = SecretStr("")
    """Basic-auth password."""
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
    """PromQL queries you wrote, run on every collection and included in reports."""
    auto_queries: bool = True
    """Let the LLM choose more queries from what this Prometheus scrapes (other exporters,
    errors, saturation, other hosts). Also reports firing alerts and down targets. The
    chosen queries are listed below and can be disabled, pinned, edited or deleted."""
    max_auto_queries: int = Field(default=12, ge=1, le=40)
    """Most automatically chosen queries."""
    replan_interval: Duration = timedelta(days=7)
    """Choose queries again at least this often (also when the set of metrics changes)."""

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
    """auto = on when the Docker API is reachable."""
    interval: Duration = timedelta(hours=12)
    """How often this collector gathers fresh data."""
    url: str = ""
    """Docker API, e.g. tcp://docker-proxy:2375. Empty = $DOCKER_HOST (set by the compose
    file), else the local socket."""
    cache_ttl: Duration = timedelta(hours=6)
    """How long a registry answer is reused. Registries rate-limit, so don't set this low."""


class ArrCollectorSettings(CollectorSettings):
    enabled: bool | Literal["auto"] = "auto"
    """auto = on as soon as an *arr app, Prowlarr or qBittorrent is configured under
    *arr & downloads."""
    interval: Duration = timedelta(minutes=2)
    """How often this collector gathers fresh data."""
    max_queue_items: int = Field(default=50, ge=1, le=500)
    """Max queue entries listed per *arr instance (the rest is only counted)."""


class CollectorsSettings(_Model):
    system: SystemCollectorSettings = SystemCollectorSettings()
    """CPU, memory, disks, network, temperatures, top processes."""
    docker: DockerCollectorSettings = Field(
        default=DockerCollectorSettings(), json_schema_extra={"test": "docker"}
    )
    """Containers: state, health, restarts, resource use, log errors, disk usage."""
    gpu: CollectorSettings = CollectorSettings(enabled="auto", interval=timedelta(seconds=30))
    """NVIDIA GPUs (needs the NVIDIA container runtime)."""
    systemd: SystemdCollectorSettings = SystemdCollectorSettings()
    """Failed units and restart loops."""
    journal: JournalCollectorSettings = JournalCollectorSettings()
    """Warnings and errors from the system journal."""
    apt: CollectorSettings = CollectorSettings(interval=timedelta(hours=6))
    """Pending package updates, security updates, reboot required."""
    image_updates: ImageUpdatesCollectorSettings = Field(
        default=ImageUpdatesCollectorSettings(), json_schema_extra={"test": "docker"}
    )
    """Running containers whose image tag has a newer build in its registry (same tag, new
    digest; does not look for newer version numbers)."""
    prometheus: PrometheusCollectorSettings = Field(
        default=PrometheusCollectorSettings(), json_schema_extra={"test": "prometheus"}
    )
    """Run PromQL queries against an existing Prometheus."""
    arr: ArrCollectorSettings = ArrCollectorSettings()
    """Radarr/Sonarr/Whisparr queues and health, Prowlarr indexers, qBittorrent torrents
    (the apps themselves are configured under *arr & downloads)."""


class TelegramSettings(_Model):
    enabled: bool = False
    """Turn the Telegram bot on (needs a bot token and at least one allowed chat id)."""
    token: Secret = SecretStr("")
    """Bot token from @BotFather."""
    allowed_chat_ids: list[int] = Field(default_factory=list)
    """Only these chats may talk to the bot. Don't know a chat id? Send /id to the bot: it
    answers in any chat."""
    report_every: int = Field(default=1, ge=0)
    """Send every Nth report (0 = alerts only)."""
    new_session_after: Duration = timedelta(hours=6)
    """Start a fresh chat after this long without a message in a Telegram chat (the old one
    stays in the Chat list on the web page). 0 = never: one chat until you send /new."""
    alerts: bool = True
    """Send a short alert whenever the health status changes (also when reports are not
    sent every time)."""
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
    """WebUI user name."""
    password: Secret = SecretStr("")
    """WebUI password."""


Policy = Literal["off", "suggest", "approve", "auto"]


class ActionSettings(_Model):
    policy: Policy = "suggest"
    """off = never; suggest = mention in reports; approve = ask you first; auto = just do it."""
    max_per_hour: int = Field(default=0, ge=0)
    """Cap on how many of these actions run per hour (0 = no cap)."""


class StalledCleanupSettings(ActionSettings):
    policy: Policy = "approve"
    """off = never; suggest = mention in reports; approve = ask you first; auto = just do it."""
    interval: Duration = timedelta(minutes=30)
    """How often to look for stalled downloads."""
    stalled_after: Duration = timedelta(hours=6)
    """A download with no progress for this long counts as stalled."""
    max_per_hour: int = Field(default=10, ge=0)
    """Cap on how many downloads are cleaned up per hour (0 = no cap)."""


class IndexerTriageSettings(ActionSettings):
    interval: Duration = timedelta(hours=1)
    """How often to look for failing indexers."""


class SearchActionSettings(ActionSettings):
    max_per_hour: int = Field(default=30, ge=0)
    """Cap on how many searches run per hour (0 = no cap)."""
    interval: Duration = timedelta(hours=6)
    """How often a search run starts."""
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
    indexer_triage: IndexerTriageSettings = IndexerTriageSettings()
    """Test/disable failing indexers in Prowlarr."""
    api_call: ActionSettings = ActionSettings(policy="approve")
    """Any other write the chat asks for through the HTTP API of a service (POST/PUT/DELETE
    to Radarr, Sonarr, Whisparr, Prowlarr, qBittorrent) and searches that query every indexer.
    Dry-run (above) applies too."""
    approval_ttl: Duration = timedelta(hours=24)
    """Pending approvals expire after this."""


class ArrSettings(_Model):
    dry_run: bool = True
    """Log what would be done without changing anything."""
    timeout: Duration = timedelta(seconds=15)
    """Per-request timeout for *arr, Prowlarr and qBittorrent."""
    radarr: list[ArrInstance] = Field(default_factory=list, json_schema_extra={"test": "radarr"})
    sonarr: list[ArrInstance] = Field(default_factory=list, json_schema_extra={"test": "sonarr"})
    whisparr: list[ArrInstance] = Field(
        default_factory=list, json_schema_extra={"test": "whisparr"}
    )
    prowlarr: ArrInstance | None = Field(default=None, json_schema_extra={"test": "prowlarr"})
    qbittorrent: QbitSettings | None = Field(
        default=None, json_schema_extra={"test": "qbittorrent"}
    )
    actions: ActionsSettings = ActionsSettings()

    @model_validator(mode="after")
    def _unique_instance_names(self) -> ArrSettings:
        for kind in ("radarr", "sonarr", "whisparr"):
            names = [i.name for i in getattr(self, kind)]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"{kind}: duplicate instance names: {', '.join(dupes)}")
        return self


# --------------------------------------------------------------------------- mcp

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


class Settings(_Model):
    """Everything configurable from the web UI."""

    general: GeneralSettings = GeneralSettings()
    llm: LLMSettings = LLMSettings()
    report: ReportSettings = ReportSettings()
    chat: ChatSettings = ChatSettings()
    storage: StorageSettings = StorageSettings()
    logs: LogsSettings = LogsSettings()
    collectors: CollectorsSettings = CollectorsSettings()
    outputs: OutputsSettings = OutputsSettings()
    arr: ArrSettings = ArrSettings()
    mcp: McpSettings = McpSettings()
    bench: BenchSettings = BenchSettings()

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
