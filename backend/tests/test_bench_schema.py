"""The benchmark scenario format: models, loader, merge-patch, set hash."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_server_monitor.bench import schema
from ai_server_monitor.bench.schema import (
    CHECK_TYPES,
    Scenario,
    ScenarioError,
    Snapshot,
    build_snapshot,
    load_scenario_file,
    load_scenarios,
    merge_patch,
    scenario_set_hash,
)


def chat(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": "chat.x",
        "group": "chat",
        "title": "A chat scenario",
        "description": "Something long enough to describe it.",
        "input": {"kind": "chat", "messages": [{"role": "user", "content": "hi"}]},
        "checks": [{"type": "tool_called", "tool": "get_status"}],
    }
    doc.update(over)
    return doc


def test_minimal_chat_scenario_loads() -> None:
    s = Scenario.model_validate(chat())
    assert s.snapshot == "base"
    assert s.tool_mode == "auto"
    assert s.checks[0].weight == 1
    assert s.public()["checks"][0]["describe"] == "calls get_status"


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="extra"):
        Scenario.model_validate(chat(titel="typo"))
    with pytest.raises(ValueError):
        Scenario.model_validate(chat(checks=[{"type": "tool_called", "tool": "x", "wieght": 2}]))


def test_unknown_check_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        Scenario.model_validate(chat(checks=[{"type": "nope"}]))


def test_id_must_start_with_group() -> None:
    with pytest.raises(ValueError, match="start with its group"):
        Scenario.model_validate(chat(id="report.x"))
    with pytest.raises(ValueError, match="id must look like"):
        Scenario.model_validate(chat(id="Chat X"))


def test_group_and_input_kind_must_agree_except_robust() -> None:
    with pytest.raises(ValueError, match=r"needs input.kind"):
        Scenario.model_validate(chat(group="report", id="report.x"))
    ok = chat(group="robust", id="robust.x")
    assert Scenario.model_validate(ok).group == "robust"
    ok_report = chat(group="robust", id="robust.y", input={"kind": "report"})
    ok_report["checks"] = [{"type": "report_json_valid"}]
    assert Scenario.model_validate(ok_report).input.kind == "report"


def test_checks_must_fit_the_input_kind() -> None:
    with pytest.raises(ValueError, match="cannot score"):
        Scenario.model_validate(chat(checks=[{"type": "report_health", "equals": "ok"}]))
    with pytest.raises(ValueError, match="cannot score"):
        Scenario.model_validate(
            chat(
                group="report",
                id="report.x",
                input={"kind": "report"},
                checks=[{"type": "tool_called", "tool": "x"}],
            )
        )


def test_bad_regexes_are_rejected() -> None:
    with pytest.raises(ValueError, match="invalid regex"):
        Scenario.model_validate(chat(checks=[{"type": "answer_not_contains", "patterns": ["("]}]))
    with pytest.raises(ValueError, match="invalid regex"):
        Scenario.model_validate(chat(world={"commands": [{"match": "[", "output": ""}]}))


def test_health_check_needs_exactly_one_of_equals_in() -> None:
    from ai_server_monitor.bench.schema import ReportHealth

    assert ReportHealth.model_validate({"type": "report_health", "in": ["ok", "warn"]}).in_
    with pytest.raises(ValueError, match="exactly one"):
        ReportHealth.model_validate({"type": "report_health"})
    with pytest.raises(ValueError, match="exactly one"):
        ReportHealth.model_validate({"type": "report_health", "equals": "ok", "in": ["ok"]})


def test_findings_mention_min_cannot_exceed_patterns() -> None:
    with pytest.raises(ValueError, match="more than"):
        Scenario.model_validate(
            chat(
                group="report",
                id="report.x",
                input={"kind": "report"},
                checks=[{"type": "report_findings_mention", "patterns": ["a"], "min": 2}],
            )
        )


def test_world_and_tool_mode_only_for_chat() -> None:
    doc = chat(group="prom_plan", id="prom_plan.x")
    doc["input"] = {
        "kind": "prom_plan",
        "catalog": {
            "jobs": [
                {
                    "job": "node",
                    "targets": [{"instance": "h:9100"}],
                    "metrics": {"node_load5": {}},
                }
            ]
        },
    }
    doc["checks"] = [{"type": "prom_queries_valid", "min": 1}]
    assert Scenario.model_validate(doc).group == "prom_plan"
    with pytest.raises(ValueError, match="tool_mode"):
        Scenario.model_validate({**doc, "tool_mode": "json"})
    with pytest.raises(ValueError, match="world"):
        Scenario.model_validate({**doc, "world": {"commands": [{"match": "x"}]}})


def test_chat_must_end_with_the_user() -> None:
    with pytest.raises(ValueError, match="last chat message"):
        Scenario.model_validate(
            chat(
                input={
                    "kind": "chat",
                    "messages": [
                        {"role": "user", "content": "a"},
                        {"role": "assistant", "content": "b"},
                    ],
                }
            )
        )


def test_every_check_type_is_documented_and_describes_itself() -> None:
    text = (Path(__file__).parents[2] / "docs" / "benchmarks.md").read_text(encoding="utf-8")
    for name, cls in CHECK_TYPES.items():
        assert f"`{name}`" in text, f"{name} is not in docs/benchmarks.md"
        assert cls.model_fields["type"].default == name
    assert len(CHECK_TYPES) == 19


# --------------------------------------------------------------------------- merge patch


def test_merge_patch_follows_rfc7386() -> None:
    target = {"a": {"b": 1, "c": [1, 2]}, "d": 4}
    out = merge_patch(target, {"a": {"b": None, "c": [9], "e": {"f": 1}}, "d": 5})
    assert out == {"a": {"c": [9], "e": {"f": 1}}, "d": 5}
    assert target == {"a": {"b": 1, "c": [1, 2]}, "d": 4}  # inputs are left alone
    assert merge_patch({"a": 1}, [1]) == [1]
    assert merge_patch(None, {"a": {"b": None}}) == {"a": {}}


def test_build_snapshot_applies_the_patch_and_validates() -> None:
    base = {"collectors": {"system": {"data": {"memory": {"percent": 10, "total": 5}}}}}
    snap = build_snapshot(base, {"collectors": {"system": {"data": {"memory": {"percent": 96}}}}})
    assert snap.collectors["system"].data["memory"] == {"percent": 96, "total": 5}
    assert snap.collectors["system"].ok is True
    with pytest.raises(ScenarioError, match="patched snapshot is invalid"):
        build_snapshot(base, {"collectors": {"system": {"ok": "maybe"}}})


# --------------------------------------------------------------------------- loading


def write(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_shipped_smoke_scenarios_load() -> None:
    scenarios, problems = load_scenarios(include_smoke=True)
    assert not problems, problems
    smoke = [s for s in scenarios if s.smoke]
    assert {s.group for s in smoke} == {"report", "chat", "robust"}
    assert all(
        s.id in {"report.smoke_disk", "chat.smoke_failed_unit", "robust.smoke_json_tools"}
        for s in smoke
    )


def test_shipped_scenario_set_is_valid() -> None:
    """Everything committed under scenarios/ (the real set) loads, ids are unique, snapshots
    exist. A broken scenario file fails here with the file name and the reason."""
    scenarios, problems = load_scenarios()
    assert not problems, "\n".join(problems)
    assert len({s.id for s in scenarios}) == len(scenarios)


def test_loader_reports_problems_but_keeps_loading(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(schema, "SNAPSHOTS_DIR", tmp_path / "snapshots")
    write(tmp_path / "snapshots" / "base.json", {"collectors": {}})
    root = tmp_path / "scenarios"
    write(root / "chat" / "good.json", chat(id="chat.good"))
    write(root / "chat" / "wrong_name.json", chat(id="chat.other"))
    write(root / "report" / "wrong_group.json", chat(id="chat.wrong_group"))
    write(root / "chat" / "nosnap.json", chat(id="chat.nosnap", snapshot="missing"))
    (root / "chat" / "broken.json").write_text("{not json", encoding="utf-8")
    write(root / "chat" / "badcheck.json", chat(id="chat.badcheck", checks=[{"type": "zzz"}]))
    scenarios, problems = load_scenarios(root)
    assert [s.id for s in scenarios] == ["chat.good", "chat.nosnap"]
    text = "\n".join(problems)
    assert "does not match the path" in text
    assert "broken.json" in text
    assert "badcheck.json" in text
    assert "unknown snapshot 'missing'" in text


def test_load_scenario_file_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "chat" / "x.json"
    write(path, chat(checks=[]))
    with pytest.raises(ScenarioError, match=r"chat/x.json"):
        load_scenario_file(path)


def test_set_hash_changes_with_content_and_ignores_order(tmp_path: Path) -> None:
    a = Scenario.model_validate(chat(id="chat.a"))
    b = Scenario.model_validate(chat(id="chat.b"))
    h = scenario_set_hash([a, b])
    assert h == scenario_set_hash([b, a])
    changed = Scenario.model_validate(chat(id="chat.b", title="Another title"))
    assert scenario_set_hash([a, changed]) != h
    assert len(h) == 12


def test_snapshot_model_accepts_the_real_collector_shape() -> None:
    snap = Snapshot.model_validate(
        {
            "now": 1780315200,
            "collectors": {
                "system": {"data": {"hostname": "h"}},
                "docker": {"ok": False, "error": "no socket", "state": "error"},
            },
            "history": {"system": [{"hours_ago": 24, "data": {"filesystems": []}}]},
        }
    )
    assert snap.collectors["docker"].ok is False
    assert snap.history["system"][0].hours_ago == 24
