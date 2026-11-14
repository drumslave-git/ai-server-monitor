from __future__ import annotations

import pytest

from ai_server_monitor.digest.models import Flag
from ai_server_monitor.report.parsing import parse_report

FLAGS = [
    Flag("crit", "Disk / almost full", "96%"),
    Flag("warn", "Unit a failed"),
    Flag("info", "i"),
]

BODY = "## Overview\nAll fine.\n## Findings\n- nothing"


def test_clean_fenced_header() -> None:
    text = (
        '```json\n{"health": "warn", "score": 72, "summary": "Disk is filling.", '
        '"findings": [{"severity": "warn", "title": "Disk", "detail": "88% used"}]}\n```\n\n' + BODY
    )
    p = parse_report(text, [])
    assert p.parsed and p.health == "warn" and p.score == 72
    assert p.summary == "Disk is filling."
    assert p.findings == [{"severity": "warn", "title": "Disk", "detail": "88% used"}]
    assert p.markdown == BODY  # header removed from the Markdown


def test_lenient_json_trailing_commas_case_and_wrong_types() -> None:
    text = (
        '```JSON\n{"Health": "CRITICAL", "Score": "35/100", "Summary": "Bad.",\n'
        ' "Findings": [{"Severity": "High", "Title": "Disk", "Detail": "full",},],}\n```\n' + BODY
    )
    p = parse_report(text, [])
    assert p.parsed and p.health == "crit" and p.score == 35 and p.summary == "Bad."
    assert p.findings == [{"severity": "crit", "title": "Disk", "detail": "full"}]


def test_bare_object_without_fence_and_prose_before() -> None:
    text = 'Sure! {"health": "ok", "score": 98, "summary": "Fine", "findings": []}\n\n' + BODY
    p = parse_report(text, FLAGS)
    assert p.parsed and p.health == "ok" and p.score == 98 and p.findings == []
    assert p.markdown.startswith("Sure!") and "{" not in p.markdown


def test_unlabelled_fence_and_float_score_fraction() -> None:
    p = parse_report('```\n{"health": "healthy", "score": 0.85, "summary": "s"}\n```\n' + BODY, [])
    assert p.parsed and p.health == "ok" and p.score == 85


def test_findings_as_strings_and_score_clamped() -> None:
    p = parse_report(
        '```json\n{"health": "warn", "score": 250, "findings": ["Disk is full"]}\n```', []
    )
    assert p.score == 100 and p.findings == [
        {"severity": "info", "title": "Disk is full", "detail": ""}
    ]


def test_invalid_health_is_derived_from_findings_or_flags() -> None:
    text = (
        '```json\n{"health": "great!!", "score": "n/a", '
        '"findings": [{"severity": "crit", "title": "x"}]}\n```'
    )
    p = parse_report(text, [])
    assert p.parsed and p.health == "crit" and p.score < 50
    assert any("derived" in n for n in p.notes)
    q = parse_report('```json\n{"summary": "only a summary"}\n```\n' + BODY, FLAGS)
    assert q.parsed and q.health == "crit" and q.summary == "only a summary"


def test_reasoning_block_is_ignored() -> None:
    text = '<think>{"health": "crit"}</think>```json\n{"health": "ok", "score": 90}\n```' + BODY
    assert parse_report(text, []).health == "ok"


@pytest.mark.parametrize(
    "text",
    [
        BODY,  # no header at all
        "```json\n{not json at all\n```\n" + BODY,
        '```json\n["a", "list"]\n```\n' + BODY,
        '{"unrelated": 1}\n' + BODY,
    ],
)
def test_fallback_uses_rule_flags(text: str) -> None:
    p = parse_report(text, FLAGS)
    assert not p.parsed
    assert p.health == "crit" and p.score == 49
    assert {f["title"] for f in p.findings} >= {"Disk / almost full", "Unit a failed"}
    assert p.summary  # first sentence of the text
    assert BODY.splitlines()[1] in p.markdown


def test_fallback_without_flags_is_ok() -> None:
    p = parse_report("Everything looks good.", [])
    assert not p.parsed and p.health == "ok" and p.score == 100 and p.findings == []
    assert p.markdown == "Everything looks good." and p.summary == "Everything looks good."
