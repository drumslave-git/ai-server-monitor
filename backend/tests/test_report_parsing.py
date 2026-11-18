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


def test_warn_from_info_flags_only_is_kept_ok() -> None:
    """A trap: three kernel audit lines are an [INFO] flag, not a problem."""
    text = (
        '```json\n{"health": "warn", "score": 70, "summary": "Audit lines.", '
        '"findings": [{"severity": "warn", "title": "Audit", "detail": "3 lines"}]}\n```\n' + BODY
    )
    p = parse_report(text, [Flag("info", "Kernel audit messages")])
    assert p.health == "ok" and p.score == 90
    assert p.findings[0]["severity"] == "info"
    assert any("kept ok" in n for n in p.notes)


def test_warn_stays_with_a_warn_flag_and_crit_is_never_lowered() -> None:
    text = '```json\n{"health": "warn", "score": 70, "summary": "s"}\n```\n' + BODY
    assert parse_report(text, [Flag("warn", "Unit a failed"), Flag("info", "i")]).health == "warn"
    assert parse_report(text, []).health == "warn"  # nothing to judge by: the model decides
    crit = '```json\n{"health": "crit", "score": 20, "summary": "s"}\n```\n' + BODY
    assert parse_report(crit, [Flag("info", "i")]).health == "crit"


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


# The live report that hit the output-token limit (completion_tokens == max): the header
# is cut off in the middle of a finding, the fence never closes.
TRUNCATED = (
    '```json\n{\n  "health": "crit",\n  "score": 28,\n'
    '  "summary": "Disk / is almost full and a container keeps crashing.",\n'
    '  "findings": [\n'
    '    {"severity": "crit", "title": "Disk / 96% full", "detail": "growing 2 GB/day"},\n'
    '    {"severity": "warn", "title": "web restarting", "detail": "5 restarts"},\n'
    '    {"severity": "wa'
)


def test_truncated_header_is_salvaged() -> None:
    p = parse_report(TRUNCATED, [Flag("warn", "Unit a failed")])
    assert p.parsed
    assert p.health == "crit" and p.score == 28
    assert p.summary == "Disk / is almost full and a container keeps crashing."
    assert [f["title"] for f in p.findings] == ["Disk / 96% full", "web restarting"]
    assert "```" not in p.summary and "```" not in p.markdown
    assert "cut off" in p.markdown and "Disk / 96% full" in p.markdown
    assert any("truncated" in n for n in p.notes)


def test_truncated_inside_the_summary_generates_one_from_the_flags() -> None:
    p = parse_report('```json\n{"health": "warn", "score": 61, "summary": "Disk is fill', FLAGS)
    assert p.health == "warn" and p.score == 61
    assert p.summary.startswith("WARN: Disk / almost full")  # generated, never "```json"


def test_truncated_mid_score_derives_the_score() -> None:
    p = parse_report('```json\n{"health": "crit", "score": 2', FLAGS)
    assert p.parsed and p.health == "crit"
    assert p.score < 50  # derived from the flags: a crit is present


def test_fence_line_is_never_the_summary() -> None:
    p = parse_report("```json\n{ not json at all", FLAGS)
    assert not p.parsed
    assert p.summary.startswith("CRIT: Disk / almost full")
    p = parse_report("```json\n```\nThe server is fine overall. More text.", [])
    assert p.summary == "The server is fine overall. More text."
