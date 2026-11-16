"""The per-scenario explanations of the Compare page and its PDF, the "best value" rule on shown
numbers, the hardware label named after the provider, and the sampling settings a run records."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.bench import pdf
from ai_server_monitor.bench import service as service_mod
from ai_server_monitor.bench.check_labels import CHECK_LABELS, GENERIC
from ai_server_monitor.bench.explain import check_labels, explain_run, explain_scenario
from ai_server_monitor.bench.sampling import effective_sampling
from ai_server_monitor.bench.schema import load_scenarios, scenario_set_hash
from ai_server_monitor.llm.modelinfo import ModelInfo

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
    out = explain_scenario(attempts, labels={"check 0": "names the restart count"})
    assert not out["ok"]
    # check 0 weighs more than check 1, so it leads; each is named once per attempt
    assert out["lines"] == [
        "missed: names the restart count (1 of 3); meets an expected check (2 of 3)"
    ]


def test_a_single_missed_check_says_attempts_and_the_rest_is_folded() -> None:
    one = explain_scenario([attempt([False, True]), attempt([False, True])])
    assert one["lines"] == ["missed: meets an expected check (2 of 2 attempts)"]
    many = explain_scenario(
        [attempt([False] * 5)], labels={f"check {i}": f"name {i}" for i in range(5)}
    )
    assert many["lines"][0].endswith("; +3 more") and many["lines"][0].count("(1 of 1)") == 2


def test_an_unlabeled_check_reads_by_its_type_never_by_its_raw_description() -> None:
    a = attempt([False])
    a["checks"][0].update(type="answer_not_contains", describe="answer does not match: /critical/")
    line = explain_scenario([a])["lines"][0]
    assert line == "missed: avoids wording it should not use (1 of 1 attempts)"
    assert "critical" not in line and "/" not in line


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


def test_sampling_text_shows_the_value_that_applied_and_where_it_came_from() -> None:
    llm = {"temperature": 0.2, "top_p": "auto", "top_k": "auto"}
    info = ModelInfo(parameters={"top_k": 64, "top_p": 0.95})
    llm["effective"] = effective_sampling(llm, info, "ollama")
    assert [pdf.sampling_text(llm, k) for k in ("temperature", "top_p", "top_k")] == [
        "0.2 (set)",
        "0.95 (model default)",
        "64 (model default)",
    ]
    assert pdf.sampling_text({"top_k": "auto"}, "top_k") == "auto · not recorded"
    assert pdf.sampling_text({"temperature": 0.2}, "top_p") == "not recorded"
    assert pdf.sampling_text({"top_k": None}, "top_k") == "not recorded"


def test_a_parameter_the_model_does_not_list_is_ollamas_default_or_not_reported() -> None:
    llm = {"temperature": "auto", "top_p": "auto", "top_k": "auto"}
    info = ModelInfo(parameters={"temperature": 1})
    got = effective_sampling(llm, info, "ollama")
    assert got["temperature"]["text"] == "1 (model default)"
    assert got["top_p"]["text"] == "0.9 (Ollama default)"
    assert got["top_k"]["text"] == "40 (Ollama default)"
    # another server answers no model info and is never given a made-up value
    other = effective_sampling(llm, None, "vllm")
    assert {v["text"] for v in other.values()} == {"server default (not reported)"}
    assert all(v["value"] is None for v in other.values())


def test_a_new_run_records_the_values(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    seen: list[str] = []

    async def fake(cfg: Any, **_: Any) -> ModelInfo:
        seen.append(cfg.model)
        return ModelInfo(parameters={"temperature": 1, "top_k": 64, "top_p": 0.95})

    monkeypatch.setattr(service_mod, "read_model_info", fake)
    bench = client.app.state.ctx.bench
    cfg = bench.ctx.settings.llm.model_copy(update={"temperature": 0.4})
    info = client.portal.call(bench._model_info, cfg)
    got = effective_sampling({"temperature": 0.4, "top_p": "auto", "top_k": "auto"}, info, "ollama")
    assert got["temperature"] == {
        "setting": 0.4,
        "value": 0.4,
        "source": "set",
        "text": "0.4 (set)",
    }
    assert got["top_k"]["text"] == "64 (model default)" and seen == [cfg.model]


def test_an_old_run_gets_the_models_current_defaults_labelled_as_read_now(
    client: TestClient,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _seed(client)
    store = client.app.state.ctx.bench.store

    async def old() -> None:
        run = await store.get_run(run_id)
        llm = {"temperature": "auto", "top_p": "auto", "top_k": 20}
        await store.update_run(run_id, meta={**run["meta"], "llm": llm})

    client.portal.call(old)

    async def fake(cfg: Any, **_: Any) -> ModelInfo:
        return ModelInfo(parameters={"temperature": 1, "top_p": 0.95})

    monkeypatch.setattr(service_mod, "read_model_info", fake)
    llm = client.get(f"/api/bench/runs/{run_id}").json()["meta"]["llm"]["effective"]
    assert llm["temperature"]["text"] == "1 (model default, read now)"
    assert llm["top_p"]["text"] == "0.95 (model default, read now)"
    assert llm["top_k"]["text"] == "20 (set)"

    async def unreadable(cfg: Any, **_: Any) -> None:
        return None

    monkeypatch.setattr(service_mod, "read_model_info", unreadable)
    llm = client.get(f"/api/bench/runs/{run_id}").json()["meta"]["llm"]["effective"]
    assert llm["temperature"]["text"] == "auto · not recorded"
    flat = " ".join(_text(client.get(f"/api/bench/runs/{run_id}/export.pdf").content).split())
    assert "temperature auto · not recorded" in flat and "top K 20 (set)" in flat


# --------------------------------------------------------------------------- check labels

RAW = ("\\", "~", "{", "*", "[", "(?")  # a regex or a cmd pattern leaking into a label


def test_every_check_of_every_scenario_has_a_plain_label() -> None:
    scenarios = load_scenarios(include_smoke=True)[0]
    assert scenarios
    labels = check_labels(scenarios)
    for s in scenarios:
        if s.smoke:
            continue
        checks = s.public()["checks"]
        entries = CHECK_LABELS.get(s.id)
        assert entries is not None, f"{s.id}: no labels in bench/check_labels.py"
        assert [t for t, _ in entries] == [c["type"] for c in checks], (
            f"{s.id}: labels and checks differ (a check was added or changed: label it)"
        )
        for c in checks:
            label = labels[s.id][c["describe"]]
            assert label and not any(bit in label for bit in RAW), f"{s.id}: {label!r}"
            assert label != c["describe"]


def test_every_check_type_has_a_generic_label() -> None:
    scenarios = load_scenarios(include_smoke=True)[0]
    types = {c["type"] for s in scenarios for c in s.public()["checks"]}
    assert types <= set(GENERIC)


def test_the_labels_do_not_change_the_scenario_set_hash() -> None:
    """Labels sit beside the scenario files, so runs 3, 4 and 5 stay comparable with new ones."""
    scenarios = load_scenarios()[0]
    assert scenario_set_hash(scenarios) == "b52eeec8d48a"


def test_example_labels_read_plainly() -> None:
    labels = check_labels(load_scenarios()[0])
    assert "reads the logrotate config before answering" in labels["chat.logrotate_fails"].values()
    assert "does not blame a wrong username/password" in labels["chat.qbit_cannot_connect"].values()
