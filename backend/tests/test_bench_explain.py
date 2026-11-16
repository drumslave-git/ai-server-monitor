"""The per-scenario explanations of the Compare page and its PDF, the "best value" rule on shown
numbers, the hardware label named after the provider, and the sampling settings a run records."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from ai_server_monitor.bench import pdf
from ai_server_monitor.bench.explain import check_labels, explain_run, explain_scenario
from ai_server_monitor.bench.schema import load_scenarios

from .test_bench_pdf import _bold_spans, _seed, _text, client  # noqa: F401


def attempt(passed: list[bool], *, status: str = "ok", **extra: Any) -> dict[str, Any]:
    checks = [
        {
            "type": "x",
            "describe": f"check {i}",
            "weight": 3.0 if i == 0 else 1.0,
            "passed": ok,
            "reason": "",
        }
        for i, ok in enumerate(passed)
    ]
    failing = [c for c in checks if not c["passed"]]
    return {
        "scenario_id": "s",
        "status": status,
        "error": extra.pop("error", None),
        "score": 0.0 if failing else 1.0,
        "judge_score": None,
        "checks": checks,
        "answer_issue": extra.pop("answer_issue", None),
        **extra,
    }


# --------------------------------------------------------------------------- explanations


def test_all_checks_passed() -> None:
    out = explain_scenario([attempt([True, True]) for _ in range(3)])
    assert out["ok"] and out["lines"] == ["all checks passed"]


def test_missed_checks_are_counted_over_the_attempts_and_the_heaviest_comes_first() -> None:
    attempts = [attempt([True, False]), attempt([False, False]), attempt([True, True])]
    out = explain_scenario(attempts, notes={"check 0": "names the restart count"})
    assert not out["ok"]
    # check 0 weighs more than check 1, so it leads; each is named once per attempt
    assert out["lines"] == ["missed: names the restart count (1 of 3); check 1 (2 of 3)"]


def test_a_single_missed_check_says_attempts_and_the_rest_is_folded() -> None:
    one = explain_scenario([attempt([False, True]), attempt([False, True])])
    assert one["lines"] == ["missed: check 0 (2 of 2 attempts)"]
    many = explain_scenario([attempt([False] * 5)])
    assert many["lines"][0].endswith("; +3 more") and many["lines"][0].count("(1 of 1)") == 2


def test_a_long_note_is_not_used_as_the_name() -> None:
    note = "by hash or by name: the chat resolves a name itself, so the target is judged below"
    out = explain_scenario([attempt([False])], notes={"check 0": note})
    assert "by hash" not in out["lines"][0] and "check 0" in out["lines"][0]


def test_errors_empty_answers_and_cut_offs_are_named() -> None:
    attempts = [
        attempt([True], status="error", error="the model timed out"),
        attempt([True], answer_issue="empty"),
        attempt([True], answer_issue="limit"),
        attempt([True]),
    ]
    out = explain_scenario(attempts)
    assert out["lines"] == [
        "1 attempt errored: the model timed out; 1 empty answer; 1 cut off by the output limit"
    ]
    assert (out["errored"], out["empty"], out["limit"]) == (1, 1, 1)


def test_points_lost_without_a_failed_check_are_not_called_perfect() -> None:
    judged = attempt([True], score=0.6, judge_score=6)
    plain = attempt([True], score=0.9)
    assert explain_scenario([judged])["lines"] == [
        "1 of 1 attempts lost points on the judge's rating"
    ]
    assert explain_scenario([plain])["lines"] == ["1 of 1 attempts scored under 100 %"]


def test_long_descriptions_are_cut_between_list_items() -> None:
    long = "answer mentions any of: " + ", ".join(f"value-{i}" for i in range(30))
    a = attempt([False])
    a["checks"][0]["describe"] = long
    line = explain_scenario([a])["lines"][0]
    assert line.startswith("missed: answer mentions any of: value-0, value-1")
    assert "…" in line and "value-2…" not in line and len(line) < 90


def test_explain_run_groups_by_scenario_and_check_labels_come_from_the_scenarios() -> None:
    scenarios = load_scenarios(include_smoke=True)[0]
    labels = check_labels(scenarios)
    assert labels and all(isinstance(v, dict) and v for v in labels.values())
    rows = [
        {**attempt([True]), "scenario_id": "a"},
        {**attempt([False]), "scenario_id": "b"},
    ]
    out = explain_run(rows)
    assert out["a"]["ok"] and not out["b"]["ok"]


def test_run_detail_and_compare_pdf_carry_the_explanations(client: TestClient) -> None:  # noqa: F811
    first = _seed(client, model="small-model", quality=0.8, failures=True)
    second = _seed(client, model="big-model", quality=1.0, failures=False)
    detail = client.get(f"/api/bench/runs/{first}").json()
    why = detail["explanations"]["report.smoke_disk"]
    assert not why["ok"] and why["lines"][0].startswith("missed: ")
    assert why["errored"] == 1 and any("errored" in line for line in why["lines"])
    good = client.get(f"/api/bench/runs/{second}").json()["explanations"]
    assert all(v["ok"] and v["lines"] == ["all checks passed"] for v in good.values())

    data = client.get(f"/api/bench/compare.pdf?ids={first},{second}").content
    flat = " ".join(_text(data).split())
    assert "all checks passed" in flat and "missed:" in flat and "errored" in flat
    assert "Top P" in flat and "Top K" in flat and "Measured from" in flat
    assert "the process that holds this model in VRAM" in flat


# --------------------------------------------------------------------------- best on shown values


def test_best_ignores_differences_the_cells_do_not_show() -> None:
    same = ["35%", "35%", "35%"]
    assert pdf._best([35.2, 35.4, 35.3], False, same) == set()  # CPU: equal as shown, none marked
    assert pdf._best([35.4, 35.2, 36.1], False, ["35%", "35%", "36%"]) == {0, 1}  # ties all marked
    assert pdf._best([35.4, 34.6, 36.1], False, ["35%", "35%", "36%"]) == {0, 1}
    assert pdf._best([1.0, 2.0], True) == {1} and pdf._best([1.0, 1.0], True) == set()
    assert pdf._best([None, 3.0], True) == set()  # nothing to compare against


def test_best_compares_the_number_not_the_share_after_it() -> None:
    gb = ["8.9 GB · 37%", "8.9 GB · 38%", "9.4 GB · 39%"]
    assert pdf._best([8.91 * 1024**3, 8.94 * 1024**3, 9.4 * 1024**3], False, gb) == {0, 1}


def test_compare_pdf_marks_nothing_when_a_row_prints_the_same(client: TestClient) -> None:  # noqa: F811
    a = _seed(client, model="one", quality=1.0, failures=False)
    b = _seed(client, model="two", quality=1.0, failures=False)
    data = client.get(f"/api/bench/compare.pdf?ids={a},{b}").content
    bold = _bold_spans(data)
    assert "100%" not in bold  # identical overall scores: no winner


# --------------------------------------------------------------------------- labels


def test_hardware_label_names_the_provider_the_run_recorded() -> None:
    meta = {"recorded": True, "gpu": True, "llm_here": "here", "llm_process": "llama-server"}
    assert (
        pdf.hardware_label(meta, "ollama") == "Ollama's model process on this host (llama-server)"
    )
    assert pdf.hardware_label(meta, "vllm") == "vLLM's model process on this host (llama-server)"
    assert pdf.hardware_label(meta, "openai-compatible") == (
        "the LLM server's process on this host (llama-server)"
    )
    assert pdf.hardware_label(meta) == "the LLM server's process on this host (llama-server)"
    assert pdf.hardware_label({"recorded": False}, "ollama") == "not recorded"
    assert pdf.hardware_label({**meta, "llm_here": "elsewhere"}) == (
        "this host; the LLM server is elsewhere"
    )


def test_sampling_text_is_auto_a_number_or_not_recorded() -> None:
    llm = {"temperature": "auto", "top_p": 0.9, "top_k": 40}
    assert [pdf.sampling_text(llm, k) for k in ("temperature", "top_p", "top_k")] == [
        "auto",
        "0.9",
        "40",
    ]
    assert pdf.sampling_text({"temperature": 0.2}, "top_p") == "not recorded"
    assert pdf.sampling_text({"top_k": None}, "top_k") == "not recorded"
