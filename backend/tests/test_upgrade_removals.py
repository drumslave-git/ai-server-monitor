"""Upgrading an installation that still has data of the removed features (the *arr/qBittorrent
integration, the model benchmarks, the api_call action): migration 0013 clears it and the app
comes up clean, with no settings reset."""

from __future__ import annotations

import json
import sqlite3
import time

from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap
from ai_server_monitor.db import load_migrations
from ai_server_monitor.main import create_app

OLD_SETTINGS = {
    "general": {"log_level": "info"},
    "llm": {"base_url": "http://llm.test/v1", "model": "test-model", "tool_mode": "none"},
    "collectors": {
        "system": {"enabled": True},
        "arr": {"enabled": "auto", "interval": "2m", "max_queue_items": 50},
    },
    "arr": {
        "dry_run": False,
        "timeout": "15s",
        "radarr": [{"name": "radarr", "url": "http://radarr:7878", "api_key": "radarr-secret"}],
        "sonarr": [],
        "whisparr": [],
        "prowlarr": None,
        "qbittorrent": {"url": "http://qbit:8080", "username": "admin", "password": "qb-pass"},
        "actions": {"stalled_cleanup": {"policy": "auto"}, "approval_ttl": "2h"},
    },
    "bench": {"repeats": 3, "judge": {"enabled": True, "api_key": "judge-secret"}},
}


def _old_database(path: str) -> None:
    """A database at schema v12 (the last one with the integration) holding its data."""
    conn = sqlite3.connect(path)
    for version, _name, sql in load_migrations()[:12]:
        conn.executescript(f"BEGIN;\n{sql}\n;PRAGMA user_version = {version};\nCOMMIT;")
    now = time.time()
    conn.execute(
        "INSERT INTO kv (key, value, updated) VALUES ('settings', ?, ?)",
        (json.dumps(OLD_SETTINGS), now),
    )
    conn.execute(
        "INSERT INTO kv (key, value, updated) VALUES ('settings.backup.1', ?, ?)",
        (json.dumps(OLD_SETTINGS), now),
    )
    conn.execute(
        "INSERT INTO collector_samples (ts, collector, ok, data) VALUES (?, 'arr', 1, '{}')",
        (now,),
    )
    conn.execute(
        "INSERT INTO collector_samples (ts, collector, ok, data) VALUES (?, 'system', 1, '{}')",
        (now,),
    )
    conn.execute(
        "INSERT INTO kv (key, value, updated) VALUES ('automation.pause', ?, ?)",
        (json.dumps({"paused": True, "since": now, "by": "bench #4"}), now),
    )
    conn.execute(
        "INSERT INTO bench_runs (id, started, status, scope, repeats) VALUES (1, ?, 'done', "
        "'all', 3)",
        (now,),
    )
    for status in ("pending", "executed"):
        conn.execute(
            "INSERT INTO actions (ts, updated, action, target, args, policy, status, source, "
            "policy_key, expires_at) VALUES (?, ?, 'arr_search', 'radarr/radarr: 2 item(s)', "
            "'{\"ids\": [1, 2]}', 'approve', ?, 'scheduler', 'missing_search', ?)",
            (now, now, status, now + 3600),
        )
    conn.commit()
    conn.close()


def test_upgrade_drops_arr_data_and_starts_clean(bootstrap: Bootstrap) -> None:
    bootstrap.data_dir.mkdir(parents=True, exist_ok=True)
    _old_database(str(bootstrap.db_path))

    with TestClient(create_app(bootstrap, collector_registry={})) as client:
        values = client.get("/api/settings").json()["values"]
        actions = client.get("/api/actions").json()

    assert "arr" not in values
    assert "arr" not in values["collectors"]
    assert "bench" not in values
    assert values["approvals"]["approval_ttl"] == "2h"
    assert values["llm"]["model"] == "test-model"
    # the old actions stay readable in the audit log
    assert "arr_search" in json.dumps(actions)

    conn = sqlite3.connect(str(bootstrap.db_path))
    try:
        keys = [row[0] for row in conn.execute("SELECT key FROM kv WHERE key LIKE 'settings%'")]
        stored = {
            key: json.loads(value)
            for key, value in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'settings%'")
        }
        samples = [row[0] for row in conn.execute("SELECT collector FROM collector_samples")]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        statuses = dict(conn.execute("SELECT status, action FROM actions").fetchall())
        expired = conn.execute(
            "SELECT result, decided_by FROM actions WHERE status = 'expired'"
        ).fetchone()
    finally:
        conn.close()

    # no settings were reset on load (that would have written a new backup)
    assert sorted(k for k in keys if k.startswith("settings.backup.")) == ["settings.backup.1"]
    for key, value in stored.items():
        if isinstance(value, dict):
            assert "arr" not in value, key
            assert "arr" not in value.get("collectors", {}), key
            assert "bench" not in value, key
            assert "radarr-secret" not in json.dumps(value), key
            assert "judge-secret" not in json.dumps(value), key
    assert samples == ["system"]
    assert not {t for t in tables if t.startswith("bench_")}
    assert "actions" in tables and "approval_batches" in tables
    assert "automation.pause" not in keys
    # the waiting one can never run now: closed; the executed one is history and stays
    assert statuses == {"expired": "arr_search", "executed": "arr_search"}
    assert expired[1] == "upgrade"
    assert "removed" in json.loads(expired[0])["detail"]
