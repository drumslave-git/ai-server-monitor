from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from servermon.config import interpolate, load_settings, parse_duration, redacted

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("15s", timedelta(seconds=15)),
        ("5m", timedelta(minutes=5)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        ("30d", timedelta(days=30)),
        ("500ms", timedelta(milliseconds=500)),
        ("2w", timedelta(weeks=2)),
        ("90", timedelta(seconds=90)),
        (45, timedelta(seconds=45)),
        (1.5, timedelta(seconds=1.5)),
        (timedelta(minutes=2), timedelta(minutes=2)),
    ],
)
def test_parse_duration(value: object, expected: timedelta) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "5x", "m5", "5m garbage", True, None, [1]])
def test_parse_duration_invalid(value: object) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


def test_interpolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FOO", "bar")
    monkeypatch.delenv("MISSING", raising=False)
    secret = tmp_path / "secret"
    secret.write_text("s3cret\n")
    monkeypatch.delenv("FROMFILE", raising=False)
    monkeypatch.setenv("FROMFILE_FILE", str(secret))
    data = {
        "a": "${FOO}",
        "b": ["x-${FOO}-y", "${MISSING:-dflt}"],
        "c": {"d": "${MISSING}", "e": 5},
        "f": "${FROMFILE}",
    }
    assert interpolate(data) == {
        "a": "bar",
        "b": ["x-bar-y", "dflt"],
        "c": {"d": "", "e": 5},
        "f": "s3cret",
    }


def test_defaults_without_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # No ASM_CONFIG and default path missing: pure defaults, no error.
    monkeypatch.setattr("servermon.config.DEFAULT_CONFIG_PATH", str(tmp_path / "nope.yaml"))
    s = load_settings()
    assert s.report.interval == timedelta(minutes=5)
    assert s.arr.dry_run is True
    assert s.collectors.gpu.enabled == "auto"


def test_explicit_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.yaml")


def test_yaml_and_env_precedence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "llm:\n"
        "  model: from-yaml\n"
        "  api_key: ${MY_KEY}\n"
        "  temperature: 0.7\n"
        "report:\n"
        "  interval: 10m\n"
        "arr:\n"
        "  radarr:\n"
        "    - { name: radarr, url: 'http://radarr:7878', api_key: abc }\n"
    )
    monkeypatch.setenv("MY_KEY", "sk-123")
    monkeypatch.setenv("ASM__LLM__MODEL", "from-env")
    monkeypatch.setenv("ASM__REPORT__SKIP_IF_UNCHANGED", "true")
    s = load_settings(cfg)
    assert s.llm.model == "from-env"  # env beats yaml
    assert s.llm.temperature == 0.7  # yaml kept where env is silent (deep merge)
    assert s.llm.api_key.get_secret_value() == "sk-123"
    assert s.report.interval == timedelta(minutes=10)
    assert s.report.skip_if_unchanged is True
    assert s.arr.radarr[0].url == "http://radarr:7878"


def test_asm_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("server:\n  port: 9999\n")
    monkeypatch.setenv("ASM_CONFIG", str(cfg))
    assert load_settings().server.port == 9999


def test_unknown_keys_rejected(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("llm:\n  modle: typo\n")
    with pytest.raises(ValidationError):
        load_settings(cfg)


def test_example_config_is_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    s = load_settings(REPO_ROOT / "config.example.yaml")
    assert s.llm.tool_mode == "auto"
    assert s.arr.actions.stalled_cleanup.policy == "approve"
    assert s.arr.actions.cutoff_search.interval == timedelta(hours=24)


def test_redacted_hides_secrets(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("llm:\n  api_key: supersecret\n")
    dumped = redacted(load_settings(cfg))
    assert "supersecret" not in str(dumped)
