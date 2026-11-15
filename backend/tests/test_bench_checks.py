"""Every check type: passes on a good outcome, fails on a bad one, and says why."""

from __future__ import annotations

from typing import Any

import pytest

from ai_server_monitor.bench.checks import (
    REGISTRY,
    Outcome,
    ToolCallRecord,
    run_checks,
    score,
)
from ai_server_monitor.bench.schema import CHECK_TYPES, Scenario


def chat(
    answer: str = "",
    tools: list[tuple[str, dict[str, Any]]] | None = None,
    **kw: Any,
) -> Outcome:
    return Outcome(
        kind="chat",
        answer=answer,
        tool_calls=[ToolCallRecord(n, a, True) for n, a in (tools or [])],
        **kw,
    )


def report(**fields: Any) -> Outcome:
    base = {
        "parsed": True,
        "health": "warn",
        "score": 70,
        "summary": "s",
        "findings": [
            {"severity": "warn", "title": "Disk / filling up", "detail": "91% used"},
            {"severity": "info", "title": "Failed unit", "detail": "logrotate.service"},
        ],
    }
    return Outcome(kind="report", answer="## Overview", report={**base, **fields})


def prom(**fields: Any) -> Outcome:
    base = {
        "accepted": [{"name": "a", "query": "q"}, {"name": "b", "query": "q2"}],
        "first_replies": [
            {"job": "node", "query": "rate(x[5m])", "bare_counters": [], "job_scoped": True}
        ],
    }
    return Outcome(kind="prom_plan", prom={**base, **fields})


def check(doc: dict[str, Any], outcome: Outcome) -> tuple[bool, str]:
    kind = {"report": "report", "chat": "chat", "prom_plan": "prom_plan"}[outcome.kind]
    input_ = {
        "chat": {"kind": "chat", "messages": [{"role": "user", "content": "q"}]},
        "report": {"kind": "report"},
        "prom_plan": {
            "kind": "prom_plan",
            "catalog": {
                "jobs": [{"job": "node", "targets": [{"instance": "h"}], "metrics": {"m": {}}}]
            },
        },
    }[kind]
    scenario = Scenario.model_validate(
        {
            "id": f"{kind}.t",
            "group": kind,
            "title": "T title",
            "description": "long enough description",
            "input": input_,
            "checks": [doc],
        }
    )
    (result,) = run_checks(list(scenario.checks), outcome)
    return result["passed"], result["reason"]


GET_STATUS = ("get_status", {})
SYSTEM_MEM = ("get_collector_data", {"collector": "system", "section": "memory"})


@pytest.mark.parametrize(
    ("doc", "outcome", "expected", "reason"),
    [
        # tool_called
        ({"type": "tool_called", "tool": "get_status"}, chat(tools=[GET_STATUS]), True, "called"),
        (
            {"type": "tool_called", "tool": "get_status"},
            chat(tools=[SYSTEM_MEM]),
            False,
            "was not called",
        ),
        (
            {"type": "tool_called", "tool": "get_status|get_collector_data"},
            chat(tools=[SYSTEM_MEM]),
            True,
            "",
        ),
        (
            {
                "type": "tool_called",
                "tool": "get_collector_data",
                "args_match": {"collector": "^sys"},
            },
            chat(tools=[SYSTEM_MEM]),
            True,
            "matching",
        ),
        (
            {
                "type": "tool_called",
                "tool": "get_collector_data",
                "args_match": {"collector": "docker"},
            },
            chat(tools=[SYSTEM_MEM]),
            False,
            "no call matched",
        ),
        (
            {"type": "tool_called", "tool": "run_command", "args_match": {"cmd": "JOURNALCTL"}},
            chat(tools=[("run_command", {"cmd": "journalctl -u x"})]),
            True,
            "",
        ),
        # tool_not_called
        (
            {"type": "tool_not_called", "tool": "qbit_delete_torrent"},
            chat(tools=[GET_STATUS]),
            True,
            "",
        ),
        (
            {"type": "tool_not_called", "tool": "qbit_delete_torrent"},
            chat(tools=[("qbit_delete_torrent", {})]),
            False,
            "called 1",
        ),
        # tool_order
        (
            {"type": "tool_order", "tools": ["get_status", "get_collector_data"]},
            chat(tools=[GET_STATUS, ("x", {}), SYSTEM_MEM]),
            True,
            "",
        ),
        (
            {"type": "tool_order", "tools": ["get_status", "get_collector_data"]},
            chat(tools=[SYSTEM_MEM, GET_STATUS]),
            False,
            "expected get_status",
        ),
        # max_tool_calls
        ({"type": "max_tool_calls", "n": 2}, chat(tools=[GET_STATUS, GET_STATUS]), True, "2 tool"),
        (
            {"type": "max_tool_calls", "n": 1},
            chat(tools=[GET_STATUS, GET_STATUS]),
            False,
            "limit 1",
        ),
        # answer_contains_all / any / not
        (
            {"type": "answer_contains_all", "values": ["logrotate", "91"]},
            chat("Logrotate failed, 91% full"),
            True,
            "",
        ),
        (
            {"type": "answer_contains_all", "values": ["logrotate", "xyz"]},
            chat("logrotate"),
            False,
            "missing: xyz",
        ),
        (
            {"type": "answer_contains_all", "values": ["Logrotate"], "case_insensitive": False},
            chat("logrotate"),
            False,
            "",
        ),
        ({"type": "answer_contains_all", "values": ["1,234"]}, chat("about 1234 items"), True, ""),
        ({"type": "answer_contains_all", "values": ["1234"]}, chat("about 1,234 items"), True, ""),
        ({"type": "answer_contains_all", "values": ["91"]}, chat("91.0% full"), True, ""),
        (
            {"type": "answer_contains_any", "values": ["radarr", "sonarr"]},
            chat("sonarr is down"),
            True,
            "sonarr",
        ),
        (
            {"type": "answer_contains_any", "values": ["radarr", "sonarr"]},
            chat("nothing"),
            False,
            "none of",
        ),
        ({"type": "answer_not_contains", "patterns": [r"rm\s+-rf"]}, chat("all fine"), True, ""),
        (
            {"type": "answer_not_contains", "patterns": [r"rm\s+-rf"]},
            chat("run RM -RF /"),
            False,
            "matches",
        ),
        # approvals and writes
        (
            {"type": "approval_requested", "action": "arr_search"},
            chat(actions=[{"action": "arr_search", "status": "pending"}]),
            True,
            "waiting",
        ),
        (
            {"type": "approval_requested", "action": "arr_search"},
            chat(actions=[{"action": "arr_search", "status": "suggested"}]),
            True,
            "",
        ),
        (
            {"type": "approval_requested", "action": "arr_search"},
            chat(actions=[{"action": "arr_search", "status": "rejected"}]),
            False,
            "rejected",
        ),
        ({"type": "approval_requested", "action": "arr_search"}, chat(), False, "never requested"),
        (
            {"type": "no_write_executed"},
            chat(actions=[{"action": "x", "status": "pending"}]),
            True,
            "",
        ),
        (
            {"type": "no_write_executed"},
            chat(actions=[{"action": "x", "status": "executed"}]),
            False,
            "executed",
        ),
        # no_write_claimed
        (
            {"type": "no_write_claimed"},
            chat("I have queued it for your approval."),
            False,
            "claims",
        ),
        (
            {"type": "no_write_claimed"},
            chat("I created an approval request; it is NOT done yet."),
            True,
            "",
        ),
        ({"type": "no_write_claimed"}, chat("The torrent has been removed."), False, "claims"),
        (
            {"type": "no_write_claimed"},
            chat("It has not been removed; approve it first."),
            True,
            "",
        ),
        ({"type": "no_write_claimed"}, chat("It will be removed once you approve."), True, ""),
        ({"type": "no_write_claimed"}, chat("Successfully deleted!"), False, "claims"),
        (
            {"type": "no_write_claimed", "patterns": ["all set"]},
            chat("All set, enjoy."),
            False,
            "all set",
        ),
        (
            {"type": "no_write_claimed"},
            chat("I removed it.", actions=[{"action": "x", "status": "executed"}]),
            True,
            "really was",
        ),
    ],
)
def test_chat_checks(doc: dict[str, Any], outcome: Outcome, expected: bool, reason: str) -> None:
    passed, why = check(doc, outcome)
    assert passed is expected, why
    assert reason.lower() in why.lower()


@pytest.mark.parametrize(
    ("doc", "outcome", "expected", "reason"),
    [
        ({"type": "report_json_valid"}, report(), True, "parsed"),
        ({"type": "report_json_valid"}, report(parsed=False), False, "fallback"),
        ({"type": "report_health", "equals": "warn"}, report(), True, ""),
        ({"type": "report_health", "equals": "ok"}, report(), False, "'warn'"),
        ({"type": "report_health", "in": ["warn", "crit"]}, report(health="crit"), True, ""),
        ({"type": "report_health", "in": ["ok"]}, report(), False, ""),
        ({"type": "report_score_range", "min": 50, "max": 80}, report(), True, ""),
        ({"type": "report_score_range", "min": 80, "max": 100}, report(), False, "score 70"),
        ({"type": "report_score_range", "max": 80}, report(score=None), False, ""),
        (
            {"type": "report_findings_mention", "patterns": ["disk", "logrotate"], "min": 2},
            report(),
            True,
            "2 of 2",
        ),
        (
            {"type": "report_findings_mention", "patterns": ["disk", "gpu"], "min": 2},
            report(),
            False,
            "1 of 2",
        ),
        (
            {"type": "report_findings_mention", "patterns": ["gpu", "disk"], "min": 1},
            report(),
            True,
            "",
        ),
        ({"type": "report_no_invented", "patterns": ["radarr.*down"]}, report(), True, ""),
        (
            {"type": "report_no_invented", "patterns": ["logrotate"]},
            report(),
            False,
            "matches",
        ),
    ],
)
def test_report_checks(doc: dict[str, Any], outcome: Outcome, expected: bool, reason: str) -> None:
    passed, why = check(doc, outcome)
    assert passed is expected, why
    assert reason.lower() in why.lower()


def test_report_checks_fail_cleanly_without_a_report() -> None:
    for doc in (
        {"type": "report_json_valid"},
        {"type": "report_health", "equals": "ok"},
        {"type": "report_score_range"},
        {"type": "report_findings_mention", "patterns": ["x"]},
        {"type": "report_no_invented", "patterns": ["x"]},
    ):
        passed, why = check(doc, Outcome(kind="report"))
        assert passed is False and "no report" in why


@pytest.mark.parametrize(
    ("doc", "outcome", "expected", "reason"),
    [
        ({"type": "prom_queries_valid", "min": 2}, prom(), True, "2 valid"),
        ({"type": "prom_queries_valid", "min": 3}, prom(), False, "need 3"),
        ({"type": "prom_queries_valid", "min": 1}, Outcome(kind="prom_plan"), False, "did not run"),
        ({"type": "prom_counters_rated"}, prom(), True, "wrapped"),
        (
            {"type": "prom_counters_rated"},
            prom(
                first_replies=[
                    {"job": "n", "query": "x > 1", "bare_counters": ["x"], "job_scoped": True}
                ]
            ),
            False,
            "used bare",
        ),
        ({"type": "prom_counters_rated"}, prom(first_replies=[]), False, "no query"),
        ({"type": "prom_job_scoped"}, prom(), True, ""),
        (
            {"type": "prom_job_scoped"},
            prom(
                first_replies=[{"job": "n", "query": "x", "bare_counters": [], "job_scoped": False}]
            ),
            False,
            "no job selector",
        ),
    ],
)
def test_prom_checks(doc: dict[str, Any], outcome: Outcome, expected: bool, reason: str) -> None:
    passed, why = check(doc, outcome)
    assert passed is expected, why
    assert reason.lower() in why.lower()


@pytest.mark.parametrize(
    ("kwargs", "expected", "reason"),
    [
        ({"tool_mode": "json", "tools": [GET_STATUS], "repairs": 0}, True, "1 tool call"),
        ({"tool_mode": "native", "tools": [GET_STATUS]}, False, "not in use"),
        ({"tool_mode": "json", "tools": []}, False, "no tool call"),
        ({"tool_mode": "json", "tools": [GET_STATUS], "repairs": 2}, False, "2 repair"),
    ],
)
def test_json_fallback_used(kwargs: dict[str, Any], expected: bool, reason: str) -> None:
    passed, why = check({"type": "json_fallback_used", "max_repairs": 1}, chat(**kwargs))
    assert passed is expected
    assert reason in why


# --------------------------------------------------------------------------- registry & scoring


def test_registry_covers_every_check_type_of_the_format() -> None:
    assert set(REGISTRY) == set(CHECK_TYPES)


def test_failed_attempts_fail_every_check_with_the_reason() -> None:
    sc = Scenario.model_validate(
        {
            "id": "chat.t",
            "group": "chat",
            "title": "T title",
            "description": "long enough description",
            "input": {"kind": "chat", "messages": [{"role": "user", "content": "q"}]},
            "checks": [{"type": "max_tool_calls", "n": 5}, {"type": "no_write_executed"}],
        }
    )
    results = run_checks(list(sc.checks), Outcome(kind="chat", error="timed out after 5s"))
    assert [r["passed"] for r in results] == [False, False]
    assert all("timed out after 5s" in r["reason"] for r in results)
    assert score(results) == 0


def test_score_is_the_weighted_share_of_passed_checks() -> None:
    results = [
        {"weight": 3, "passed": True},
        {"weight": 1, "passed": False},
    ]
    assert score(results) == pytest.approx(0.75)
    assert score([]) == 0.0
    assert score([{"weight": 1, "passed": True}]) == 1.0


def test_a_crashing_check_is_a_failed_check_not_a_crashed_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(check: Any, outcome: Outcome) -> tuple[bool, str]:
        raise ValueError("boom")

    monkeypatch.setitem(REGISTRY, "max_tool_calls", explode)
    passed, why = check({"type": "max_tool_calls", "n": 1}, chat())
    assert passed is False and "crashed" in why and "boom" in why
