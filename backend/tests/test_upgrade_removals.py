"""Upgrading an installation that still has data of removed features: migration 0013 (the
*arr/qBittorrent integration, the model benchmarks, the api_call action) and migration 0014
(collectors, digest, reports, the Prometheus planner, the hand-written LLM client) clear it and
the app comes up clean, with no settings reset."""

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
    "llm": {
        "base_url": "http://llm.test/v1",
        "model": "test-model",
        "tool_mode": "none",
        "max_context_tokens": 8192,
        "stream_usage": True,
    },
    "worker": {"model": "", "tool_mode": "auto", "stream_usage": False},
    "report": {"interval": "5m", "language": "de", "digest_top_n": 5},
    "storage": {"retention": "30d", "sample_interval": "5m"},
    "collectors": {
        "system": {"enabled": True},
        "docker": {"url": "tcp://docker-proxy:2375", "log_scan": True},
        "prometheus": {"url": "http://prom:9090", "token": "prom-secret"},
        "arr": {"enabled": "auto", "interval": "2m", "max_queue_items": 50},
    },
    "outputs": {"telegram": {"enabled": False, "report_every": 3, "alerts": True}},
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
REMOVED_KV = (
    "llm.tool_mode",
    "prom_plan",
    "prom_dismissed",
    "telegram.report_count",
    "telegram.last_health",
    "telegram.report_requests",
)


def _old_database(path: str) -> None:
    """A database at schema v12 (before both removals) holding data of every removed part."""
    conn = sqlite3.connect(path)
    for version, _name, sql in load_migrations()[:12]:
        conn.executescript(f"BEGIN;\n{sql}\n;PRAGMA user_version = {version};\nCOMMIT;")
    now = time.time()
    for key in ("settings", "settings.backup.1"):
        conn.execute(
            "INSERT INTO kv (key, value, updated) VALUES (?, ?, ?)",
            (key, json.dumps(OLD_SETTINGS), now),
        )
    for key in REMOVED_KV:
        conn.execute("INSERT INTO kv (key, value, updated) VALUES (?, '1', ?)", (key, now))
    conn.execute(
        "INSERT INTO kv (key, value, updated) VALUES ('automation.pause', ?, ?)",
        (json.dumps({"paused": True, "since": now, "by": "bench #4"}), now),
    )
    for collector in ("arr", "system"):
        conn.execute(
            "INSERT INTO collector_samples (ts, collector, ok, data) VALUES (?, ?, 1, '{}')",
            (now, collector),
        )
    conn.execute("INSERT INTO reports (ts, markdown) VALUES (?, 'old report')", (now,))
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


def test_upgrade_drops_removed_features_and_starts_clean(bootstrap: Bootstrap) -> None:
    bootstrap.data_dir.mkdir(parents=True, exist_ok=True)
    _old_database(str(bootstrap.db_path))

    with TestClient(create_app(bootstrap)) as client:
        values = client.get("/api/settings").json()["values"]
        actions = client.get("/api/actions").json()
        status = client.get("/api/status")

    for section in ("arr", "bench", "report", "collectors"):
        assert section not in values
    assert values["approvals"]["approval_ttl"] == "2h"
    assert values["general"]["language"] == "de"
    assert values["general"]["docker_url"] == "tcp://docker-proxy:2375"
    assert values["llm"]["model"] == "test-model"
    assert values["outputs"]["telegram"]["send_summary"] == "changed"  # from report_every = 3
    assert "report_every" not in values["outputs"]["telegram"]
    assert "tool_mode" not in values["llm"] and "tool_mode" not in values["worker"]
    assert status.status_code == 200 and status.json()["pending_approvals"] == 0
    # the old actions stay readable in the audit log
    assert "arr_search" in json.dumps(actions)

    conn = sqlite3.connect(str(bootstrap.db_path))
    try:
        keys = [row[0] for row in conn.execute("SELECT key FROM kv")]
        stored = {
            key: json.loads(value)
            for key, value in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'settings%'")
        }
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
            for section in ("arr", "bench", "report", "collectors"):
                assert section not in value, (key, section)
            text = json.dumps(value)
            for secret in ("radarr-secret", "judge-secret", "prom-secret"):
                assert secret not in text, (key, secret)
            for gone in ("tool_mode", "max_context_tokens", "stream_usage", "sample_interval"):
                assert gone not in text, (key, gone)
            assert "report_every" not in text and '"alerts"' not in text, key
            assert value["outputs"]["telegram"]["send_summary"] == "changed", key
    assert not {t for t in tables if t.startswith("bench_")}
    assert not tables & {"collector_samples", "reports", "prom_queries"}
    assert "actions" in tables and "approval_batches" in tables
    assert "automation.pause" not in keys
    assert not set(REMOVED_KV) & set(keys)
    # the waiting one can never run now: closed; the executed one is history and stays
    assert statuses == {"expired": "arr_search", "executed": "arr_search"}
    assert expired[1] == "upgrade"
    assert "removed" in json.loads(expired[0])["detail"]


def _send_summary_after_migration(telegram: dict[str, object]) -> object:
    """Run migration 0016 on a stored settings row (and a backup) and read the new field."""
    sql = dict((name, text) for _v, name, text in load_migrations())["0016_telegram_summary.sql"]
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT, updated REAL)")
    for key in ("settings", "settings.backup.7"):
        value = json.dumps({"outputs": {"telegram": {"enabled": True, **telegram}}})
        conn.execute("INSERT INTO kv VALUES (?, ?, 1)", (key, value))
    conn.executescript(sql)
    rows = [json.loads(v)["outputs"]["telegram"] for (v,) in conn.execute("SELECT value FROM kv")]
    conn.close()
    assert rows[0] == rows[1]
    assert "report_every" not in rows[0] and "alerts" not in rows[0]
    return rows[0].get("send_summary")


def test_telegram_report_switches_map_to_send_summary() -> None:
    cases = [
        ({"report_every": 0, "alerts": True}, "changed"),
        ({"report_every": 0, "alerts": False}, "never"),
        ({"report_every": 1, "alerts": False}, "every"),
        ({"report_every": 1}, "every"),
        ({"report_every": 4, "alerts": False}, "changed"),
        ({"alerts": False}, "never"),
        ({"alerts": True}, "changed"),
        ({}, None),  # nothing stored: the default applies
    ]
    for old, expected in cases:
        assert _send_summary_after_migration(old) == expected, old
