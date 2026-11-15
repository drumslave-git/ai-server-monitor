"""The shipped benchmark scenarios and snapshots: they load, patch, make sense and are synthetic.

This is a linter for the data in `bench/scenarios/` and `bench/snapshots/` (the format itself is
tested in test_bench_schema.py). The repository is public: nothing in these files may identify
a real network, host or person.
"""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any

import pytest

from ai_server_monitor.actions.registry import REGISTRY
from ai_server_monitor.bench import schema
from ai_server_monitor.bench.schema import (
    CHECK_TYPES,
    GROUPS,
    Scenario,
    build_snapshot,
    load_scenarios,
    load_snapshot_file,
)
from ai_server_monitor.chat.host_tools import host_tools
from ai_server_monitor.chat.tools import READ_TOOLS
from ai_server_monitor.config import Settings
from ai_server_monitor.db import Database
from ai_server_monitor.digest.digester import build_digest

SCENARIOS, PROBLEMS = load_scenarios()
SNAPSHOT_FILES = sorted(schema.SNAPSHOTS_DIR.glob("*.json"))
DATA_FILES = [*sorted(schema.SCENARIOS_DIR.glob("*/*.json")), *SNAPSHOT_FILES]
DATA_FILES = [p for p in DATA_FILES if "_smoke" not in p.parts]

BY_ID = {s.id: s for s in SCENARIOS}
GROUP_MINIMUM = {"report": 6, "chat": 10, "prom_plan": 3, "robust": 5}

KNOWN_TOOLS = (
    {t.name for t in READ_TOOLS}
    | {t.name for t in host_tools()}
    | set(REGISTRY)
    | {"api_call", "api_endpoints", "mcp_call"}
)
API_SERVICES = {
    "radarr",
    "sonarr",
    "whisparr",
    "prowlarr",
    "qbittorrent",
    "docker",
    "prometheus",
    "movies",
    "series",
    "all_arr",
}
WRITE_TOOLS = {name for name, d in REGISTRY.items() if d.chat_tool}


def test_everything_loads() -> None:
    assert PROBLEMS == []
    assert SCENARIOS, "no scenarios shipped"


def test_ids_are_unique_and_match_the_path() -> None:
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids))
    files = [p for p in schema.SCENARIOS_DIR.glob("*/*.json") if p.parent.name != "_smoke"]
    assert len(files) == len(SCENARIOS)
    for path in files:
        assert f"{path.parent.name}.{path.stem}" in BY_ID


def test_group_coverage() -> None:
    for group, minimum in GROUP_MINIMUM.items():
        assert group in GROUPS
        n = sum(1 for s in SCENARIOS if s.group == group)
        assert n >= minimum, f"{group}: {n} scenarios, expected at least {minimum}"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_scenario_is_well_formed(scenario: Scenario) -> None:
    assert len(scenario.description) >= 120, "say what it tests and why it matters"
    assert scenario.judge is not None, "every shipped scenario has a judge rubric"
    weights = [c.weight for c in scenario.checks]
    assert max(weights) >= 2, "give the decisive checks a weight of 2 or more"
    assert len(scenario.checks) >= 3
    for check in scenario.checks:
        assert check.type in CHECK_TYPES
    # the snapshot exists and the patch applies to it
    base = load_snapshot_file(scenario)
    snap = build_snapshot(base, scenario.patch)
    assert snap.collectors


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
def test_tool_names_exist(scenario: Scenario) -> None:
    mcp_names = {f"{r.server}__{r.tool}" for r in scenario.world.mcp}
    known = KNOWN_TOOLS | mcp_names
    for check in scenario.checks:
        names: list[str] = []
        if check.type in ("tool_called", "tool_not_called"):
            names = check.tool.split("|")  # type: ignore[attr-defined]
        elif check.type == "tool_order":
            names = list(check.tools)  # type: ignore[attr-defined]
        elif check.type == "approval_requested":
            names = [check.action]  # type: ignore[attr-defined]
        for name in names:
            assert name in known, f"{scenario.id}: unknown tool {name!r}"
    for rule in scenario.world.api:
        assert rule.service in API_SERVICES, f"{scenario.id}: unknown service {rule.service!r}"
    for check in scenario.checks:
        if check.type == "approval_requested":
            assert check.action in WRITE_TOOLS  # type: ignore[attr-defined]


def test_write_scenarios_check_the_honesty_rules() -> None:
    """Any scenario that offers a write path must also check that nothing was executed."""
    for s in SCENARIOS:
        if s.input.kind != "chat":
            continue
        tools_used = {t for c in s.checks if c.type == "tool_called" for t in c.tool.split("|")}  # type: ignore[attr-defined]
        if tools_used & WRITE_TOOLS:
            types = {c.type for c in s.checks}
            assert {"no_write_executed", "no_write_claimed"} <= types, s.id


def test_injection_scenarios_forbid_the_write_tools() -> None:
    for sid in ("robust.injection_journal_line", "robust.injection_torrent_name"):
        forbidden = {
            t
            for c in BY_ID[sid].checks
            if c.type == "tool_not_called"
            for t in c.tool.split("|")  # type: ignore[attr-defined]
        }
        assert {"qbit_delete_torrent", "arr_remove_queue_item"} <= forbidden


# ----------------------------------------------------------------------------- snapshots

_PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "127.0.0.0/8",
        "172.16.0.0/12",
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "0.0.0.0/8",
    )
]
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.]*\d)(?!\.\d)")
# Things that must never be committed: the owner's network and names, and secret formats.
_FORBIDDEN = [
    r"192\.168\.",
    r"tcloud",
    r"drumslave",
    r"\bgeorg",
    r"tislenko",
    r"sk-[A-Za-z0-9]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)\"?(password|passwd|api[_-]?key|secret|token)\"?\s*[:=]\s*\"?[A-Za-z0-9/+_-]{12,}",
]


@pytest.mark.parametrize("path", DATA_FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_real_data_in_committed_files(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for pattern in _FORBIDDEN:
        found = re.search(pattern, text)
        assert not found, f"{path.name}: forbidden content {found.group(0)!r}"
    for m in _IPV4.finditer(text):
        octets = [int(g) for g in m.groups()]
        if any(o > 255 for o in octets):
            continue  # a version number like 5.26.2.10099
        ip = ipaddress.ip_address(".".join(map(str, octets)))
        assert any(ip in net for net in _PRIVATE_NETS), f"{path.name}: public IP {ip}"


@pytest.mark.parametrize("path", SNAPSHOT_FILES, ids=lambda p: p.name)
def test_snapshot_file_is_valid(path: Path) -> None:
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    schema.Snapshot.model_validate(doc)
    assert doc["name"] == path.stem
    assert doc.get("now"), "snapshots pin the time"


def test_every_snapshot_is_used() -> None:
    used = {s.snapshot for s in SCENARIOS}
    assert {p.stem for p in SNAPSHOT_FILES} <= used | {"base"}


# What the real digest says about each snapshot: the scenarios' expected answers rest on it.
EXPECTED_FLAGS: dict[str, dict[str, list[str]]] = {
    "base": {"crit": [], "warn": []},
    "base-disk-filling": {
        "crit": [],
        "warn": ["Disk /mnt/disks/downloads 91% full", "full in ~4 days"],
    },
    "base-restart-loop": {
        "crit": ["esphome is restart-looping"],
        "warn": ["logrotate.service failed"],
    },
    "base-oom": {"crit": ["comfyui was OOM-killed"], "warn": ["High memory use", "Heavy swap use"]},
    "base-five-problems": {
        "crit": ["Disk /mnt/disks/models 96% full"],
        "warn": [
            "backup-sync.service failed",
            "open-webui is unhealthy",
            "is hot",
            "security updates",
        ],
    },
    "base-arr-stuck": {"crit": [], "warn": ["queue item(s) with problems", "FictionTracker"]},
}


def _raw(snap: schema.Snapshot) -> tuple[dict[str, dict[str, Any]], float]:
    now = float(snap.now or 0)
    raw: dict[str, dict[str, Any]] = {}
    for name, c in snap.collectors.items():
        ts = now - c.age_s
        raw[name] = {
            "state": "active",
            "reason": None,
            "last_ts": ts,
            "ok": c.ok,
            "error": c.error,
            "result": {"name": name, "ts": ts, "ok": c.ok, "data": c.data, "error": c.error},
        }
    return raw, now


@pytest.mark.parametrize("name", sorted(EXPECTED_FLAGS))
async def test_digest_flags_of_snapshots(name: str, db: Database) -> None:
    doc = json.loads((schema.SNAPSHOTS_DIR / f"{name}.json").read_text(encoding="utf-8"))
    snap = schema.Snapshot.model_validate(doc)
    raw, now = _raw(snap)
    for collector, samples in snap.history.items():
        for sample in samples:
            await db.execute(
                "INSERT INTO collector_samples (ts, collector, ok, data, error) VALUES (?,?,?,?,?)",
                (now - sample.hours_ago * 3600, collector, 1, json.dumps(sample.data), None),
            )
    digest = await build_digest(raw, db, Settings().report, now=now)
    flags = [(f.severity, f"{f.title} {f.detail}") for f in digest.flags]
    expected = EXPECTED_FLAGS[name]
    for severity in ("crit", "warn"):
        actual = [text for sev, text in flags if sev == severity]
        if not expected[severity]:
            assert not actual, f"{name}: unexpected {severity} flags {actual}"
        for needle in expected[severity]:
            assert any(needle in text for text in actual), f"{name}: no {severity} flag {needle!r}"
    if name == "base":
        assert all(sev == "info" for sev, _ in flags)
