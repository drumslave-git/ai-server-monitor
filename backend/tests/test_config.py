from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_server_monitor import settings_store
from ai_server_monitor.config import (
    SECRET_MASK,
    Bootstrap,
    Settings,
    format_duration,
    parse_duration,
    settings_json_schema,
)
from ai_server_monitor.db import Database
from ai_server_monitor.settings_store import (
    KV_KEY,
    UPGRADES_KEY,
    SettingsStore,
    lenient_validate,
    merge_secrets,
)


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


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(300, "5m"), (5400, "1h30m"), (0.5, "500ms"), (86400 * 30, "30d"), (0, "0s"), (61, "1m1s")],
)
def test_format_duration_roundtrip(seconds: float, text: str) -> None:
    assert format_duration(timedelta(seconds=seconds)) == text
    assert parse_duration(text) == timedelta(seconds=seconds)


def test_bootstrap_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASM_DATA_DIR", "/srv/asm")
    monkeypatch.setenv("ASM_PORT", "9000")
    b = Bootstrap()
    assert b.db_path == Path("/srv/asm/monitor.db") and b.port == 9000
    assert b.host_root == Path("/host")


def test_defaults_and_json_shape() -> None:
    s = Settings()
    assert s.llm.configured is False  # nothing configured until the user does it in the UI
    assert s.arr.dry_run is True
    data = s.public_dict()
    assert data["report"]["interval"] == "5m"  # human durations, not ISO 8601
    assert data["collectors"]["gpu"]["enabled"] == "auto"


def test_secrets_masked_publicly_but_stored_in_clear() -> None:
    s = Settings(llm={"api_key": "sk-1"}, outputs={"telegram": {"token": ""}})
    assert s.public_dict()["llm"]["api_key"] == SECRET_MASK
    assert s.public_dict()["outputs"]["telegram"]["token"] == ""  # unset stays empty
    assert s.storage_dict()["llm"]["api_key"] == "sk-1"
    assert Settings.model_validate(s.storage_dict()) == s  # storage round-trips


def test_schema_has_help_text_and_widgets() -> None:
    schema = settings_json_schema()
    llm = schema["$defs"]["LLMSettings"]["properties"]
    assert "OpenAI-compatible" in llm["base_url"]["description"]  # from attribute docstrings
    assert llm["api_key"]["format"] == "password"
    assert llm["timeout"]["format"] == "duration" and llm["timeout"]["default"] == "3m"


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"llm": {"modle": "typo"}})


def test_merge_secrets_matches_list_items_by_name() -> None:
    current = {"radarr": [{"name": "a", "api_key": "KA"}, {"name": "b", "api_key": "KB"}]}
    incoming = {"radarr": [{"name": "b", "api_key": SECRET_MASK}, {"name": "c", "api_key": "KC"}]}
    assert merge_secrets(incoming, current) == {
        "radarr": [{"name": "b", "api_key": "KB"}, {"name": "c", "api_key": "KC"}]
    }
    assert merge_secrets({"k": SECRET_MASK}, {}) == {"k": ""}  # mask without stored value


def test_lenient_validate_keeps_valid_parts() -> None:
    raw = Settings(llm={"model": "m1", "base_url": "http://x/v1"}).storage_dict()
    raw["llm"]["removed_in_new_version"] = 1
    raw["report"]["interval"] = "garbage"
    settings, problems = lenient_validate(raw)
    assert settings.llm.model == "m1"  # untouched
    assert settings.report.interval == timedelta(minutes=5)  # reset to default
    assert len(problems) == 2


def test_lenient_validate_drops_incomplete_list_item_not_everything() -> None:
    raw = Settings(llm={"model": "keep-me"}).storage_dict()
    raw["arr"]["radarr"] = [
        {"name": "", "url": "", "api_key": ""},
        {"name": "ok", "url": "http://radarr:7878", "api_key": "k"},
    ]
    del raw["arr"]["radarr"][0]["name"]  # missing required field
    settings, _ = lenient_validate(raw)
    assert settings.llm.model == "keep-me"
    assert [r.name for r in settings.arr.radarr] == ["ok"]


def test_every_tool_may_run_for_an_hour_by_default() -> None:
    assert Settings().chat.tool_timeout == timedelta(hours=1)


async def test_stored_old_default_tool_timeout_moves_to_the_new_default(db: Database) -> None:
    """The database holds every setting, so the old 30 s default would stay forever."""
    raw = Settings().storage_dict()
    raw["chat"]["tool_timeout"] = "30s"
    raw["chat"]["disk_scan_budget"] = "20s"  # removed setting
    await db.kv_set("settings", raw)
    assert (await SettingsStore(db).load()).chat.tool_timeout == timedelta(hours=1)
    # a value the owner chose is kept
    raw["chat"]["tool_timeout"] = "5m"
    await db.kv_set("settings", raw)
    assert (await SettingsStore(db).load()).chat.tool_timeout == timedelta(minutes=5)


async def test_store_roundtrip_and_repair(db: Database) -> None:
    store = SettingsStore(db)
    assert await store.load() == Settings()  # first run
    await store.save(Settings(llm={"model": "m2", "api_key": "k"}))
    loaded = await store.load()
    assert loaded.llm.model == "m2" and loaded.llm.api_key.get_secret_value() == "k"

    raw = await db.kv_get(KV_KEY)
    raw["report"]["interval"] = "nope"  # e.g. written by an older/newer version
    await db.kv_set(KV_KEY, raw)
    repaired = await store.load()
    assert repaired.llm.model == "m2"
    backups = await db.fetchall("SELECT key FROM kv WHERE key LIKE 'settings.backup.%'")
    assert len(backups) == 1


async def test_old_default_upgrades_apply_once(db: Database) -> None:
    store = SettingsStore(db)
    # fresh install: nothing to upgrade, and a later deliberate old value is kept
    assert await store.load() == Settings()
    await store.save(Settings(llm={"max_output_tokens": 4096, "temperature": 0.2}))
    s = await SettingsStore(db).load()
    assert s.llm.max_output_tokens == 4096 and s.llm.temperature == 0.2

    # an installation from before the marker: dropped once, kept after the user sets it again
    await db.execute("DELETE FROM kv WHERE key = ?", (UPGRADES_KEY,))
    s = await SettingsStore(db).load()
    assert s.llm.max_output_tokens == Settings().llm.max_output_tokens
    await store.save(
        s.model_copy(update={"llm": s.llm.model_copy(update={"max_output_tokens": 4096})})
    )
    assert (await SettingsStore(db).load()).llm.max_output_tokens == 4096


async def test_a_new_old_default_entry_applies_once(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SettingsStore(db)
    await store.save(Settings())
    await store.load()  # marker written for the current entries
    monkeypatch.setitem(settings_store.OLD_DEFAULTS, ("chat", "tool_timeout"), "1h")
    monkeypatch.setitem(settings_store.OLD_DEFAULTS, ("llm", "model"), "old-model")
    await db.kv_set(KV_KEY, {**Settings().storage_dict(), "llm": {"model": "old-model"}})
    await db.kv_set(UPGRADES_KEY, ["chat.tool_timeout", "llm.max_output_tokens", "llm.temperature"])
    assert (await SettingsStore(db).load()).llm.model == Settings().llm.model
    await store.save(Settings(llm={"model": "old-model"}))
    assert (await SettingsStore(db).load()).llm.model == "old-model"
