"""Configuration: YAML file + `${VAR}` interpolation + `ASM__SECTION__KEY` env overrides.

Precedence (highest first): explicit kwargs > environment (`ASM__...`) > YAML file > defaults.

Inside the YAML file, `${VAR}` / `${VAR:-default}` are replaced with environment values.
If `VAR` is unset but `VAR_FILE` is set, the file's contents are used (Docker secrets style).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/config/config.yaml"
CONFIG_PATH_ENV = "ASM_CONFIG"

# --------------------------------------------------------------------------- durations

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)", re.IGNORECASE)
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


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
            raise ValueError(f"invalid duration: {value!r}")
        total += float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
        pos = match.end()
    if pos != len(text):
        raise ValueError(f"invalid duration: {value!r}")
    return timedelta(seconds=total)


Duration = Annotated[timedelta, BeforeValidator(parse_duration)]

# --------------------------------------------------------------------------- interpolation

_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _lookup_env(name: str, default: str | None) -> str:
    if name in os.environ:
        return os.environ[name]
    file_path = os.environ.get(f"{name}_FILE")
    if file_path:
        try:
            return Path(file_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("could not read %s_FILE=%s: %s", name, file_path, exc)
    if default is not None:
        return default
    log.debug("config references unset variable ${%s}; using empty string", name)
    return ""


def interpolate(value: Any) -> Any:
    """Recursively replace ${VAR} / ${VAR:-default} in all strings of a YAML structure."""
    if isinstance(value, str):
        return _VAR_PATTERN.sub(lambda m: _lookup_env(m.group(1), m.group(2)), value)
    if isinstance(value, list):
        return [interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- models


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Model):
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    static_dir: Path | None = None
    """Directory with the built SPA. Served at / when it exists."""


ToolMode = Literal["auto", "native", "json", "none"]


class LLMConfig(_Model):
    base_url: str = "http://localhost:11434/v1"
    api_key: SecretStr = SecretStr("")
    model: str = "qwen2.5:14b"
    tool_mode: ToolMode = "auto"
    max_context_tokens: int = 8192
    max_output_tokens: int = 1500
    temperature: float = 0.2
    timeout: Duration = timedelta(seconds=180)
    max_retries: int = 2
    stream_usage: bool = True
    """Request token usage in streamed responses (stream_options.include_usage)."""
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """Extra provider-specific request fields, passed through verbatim."""


class ReportConfig(_Model):
    interval: Duration = timedelta(minutes=5)
    skip_if_unchanged: bool = False
    language: str = "en"


class StorageConfig(_Model):
    path: Path = Path("/data/monitor.db")
    retention: Duration = timedelta(days=30)
    prune_interval: Duration = timedelta(hours=1)


class CollectorConfig(_Model):
    enabled: bool | Literal["auto"] = True
    interval: Duration = timedelta(seconds=30)


class JournalCollectorConfig(CollectorConfig):
    interval: Duration = timedelta(minutes=1)
    priority: Literal["emerg", "alert", "crit", "err", "warning", "notice", "info"] = "warning"


class PrometheusCollectorConfig(CollectorConfig):
    enabled: bool | Literal["auto"] = False
    interval: Duration = timedelta(minutes=1)
    url: str = "http://prometheus:9090"
    queries: dict[str, str] = Field(default_factory=dict)


class CollectorsConfig(_Model):
    host_root: Path = Path("/host")
    """Where the host filesystem is mounted read-only inside the container."""
    system: CollectorConfig = CollectorConfig(interval=timedelta(seconds=15))
    docker: CollectorConfig = CollectorConfig(interval=timedelta(seconds=30))
    gpu: CollectorConfig = CollectorConfig(enabled="auto", interval=timedelta(seconds=30))
    systemd: CollectorConfig = CollectorConfig(interval=timedelta(minutes=1))
    journal: JournalCollectorConfig = JournalCollectorConfig()
    apt: CollectorConfig = CollectorConfig(interval=timedelta(hours=6))
    image_updates: CollectorConfig = CollectorConfig(interval=timedelta(hours=12))
    prometheus: PrometheusCollectorConfig = PrometheusCollectorConfig()


class TelegramConfig(_Model):
    enabled: bool = False
    token: SecretStr = SecretStr("")
    allowed_chat_ids: list[int] = Field(default_factory=list)
    report_every: int = Field(default=1, ge=0)
    """Send every Nth report (0 = never send scheduled reports, alerts only)."""


class MetricsConfig(_Model):
    enabled: bool = True


class OutputsConfig(_Model):
    telegram: TelegramConfig = TelegramConfig()
    metrics: MetricsConfig = MetricsConfig()


class ArrInstance(_Model):
    name: str
    url: str
    api_key: SecretStr


class QbitConfig(_Model):
    url: str
    username: str = "admin"
    password: SecretStr = SecretStr("")


Policy = Literal["off", "suggest", "approve", "auto"]


class ActionConfig(_Model):
    policy: Policy = "suggest"


class StalledCleanupConfig(ActionConfig):
    policy: Policy = "approve"
    stalled_after: Duration = timedelta(hours=6)
    max_per_hour: int = 10


class SearchActionConfig(ActionConfig):
    interval: Duration = timedelta(hours=6)
    batch: int = 10


class ActionsConfig(_Model):
    stalled_cleanup: StalledCleanupConfig = StalledCleanupConfig()
    missing_search: SearchActionConfig = SearchActionConfig()
    cutoff_search: SearchActionConfig = SearchActionConfig(interval=timedelta(hours=24), batch=5)
    add_media: ActionConfig = ActionConfig(policy="approve")
    indexer_triage: ActionConfig = ActionConfig()
    approval_ttl: Duration = timedelta(hours=24)


class ArrConfig(_Model):
    dry_run: bool = True
    radarr: list[ArrInstance] = Field(default_factory=list)
    sonarr: list[ArrInstance] = Field(default_factory=list)
    whisparr: list[ArrInstance] = Field(default_factory=list)
    prowlarr: ArrInstance | None = None
    qbittorrent: QbitConfig | None = None
    actions: ActionsConfig = ActionsConfig()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ASM__",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )

    server: ServerConfig = ServerConfig()
    llm: LLMConfig = LLMConfig()
    report: ReportConfig = ReportConfig()
    storage: StorageConfig = StorageConfig()
    collectors: CollectorsConfig = CollectorsConfig()
    outputs: OutputsConfig = OutputsConfig()
    arr: ArrConfig = ArrConfig()


class _DictSource(PydanticBaseSettingsSource):
    """Settings source returning a pre-loaded dict (the interpolated YAML)."""

    def __init__(self, settings_cls: type[BaseSettings], data: dict[str, Any]):
        super().__init__(settings_cls)
        self._data = data

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._data


def load_yaml(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return interpolate(raw)


def load_settings(path: str | Path | None = None, **overrides: Any) -> Settings:
    """Load settings from YAML (if present) with env overrides.

    `path` defaults to $ASM_CONFIG, then /config/config.yaml. A missing file is not an
    error when no path was given explicitly (pure env/default configuration).
    """
    explicit = path is not None or CONFIG_PATH_ENV in os.environ
    config_path = Path(path or os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))
    data: dict[str, Any] = {}
    if config_path.is_file():
        data = load_yaml(config_path)
        log.info("loaded config from %s", config_path)
    elif explicit:
        raise FileNotFoundError(f"config file not found: {config_path}")

    class _FileSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (init_settings, env_settings, _DictSource(settings_cls, data))

    return _FileSettings(**overrides)


def redacted(settings: Settings) -> dict[str, Any]:
    """Settings as a JSON-safe dict with secrets masked (SecretStr dumps as '**********')."""
    return settings.model_dump(mode="json")
