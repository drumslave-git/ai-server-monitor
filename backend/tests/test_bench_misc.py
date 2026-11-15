"""Backend probe, judge, summaries, private snapshots, settings."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.bench import judge
from ai_server_monitor.bench.probe import probe_backend
from ai_server_monitor.bench.runner import summarize
from ai_server_monitor.bench.schema import Scenario
from ai_server_monitor.config import BenchJudgeSettings, LLMSettings, Settings

from .fake_llm import CANNED_REPORT
from .test_bench_run import StubSystem, make_app, probe_transport, run_bench, wait_done  # noqa: F401
from .test_chat import FakeLLM, say

# --------------------------------------------------------------------------- probe


def routes(table: dict[str, Any]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        body = table.get(request.url.path)
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)

    return httpx.MockTransport(handler)


async def test_probe_ollama_reads_version_size_quantization_and_context() -> None:
    cfg = LLMSettings(base_url="https://ollama.example/v1", model="gemma4:e4b")
    found = await probe_backend(
        cfg,
        transport=routes(
            {
                "/api/version": {"version": "0.13.5"},
                "/api/show": {
                    "details": {
                        "parameter_size": "8.0B",
                        "quantization_level": "Q4_K_M",
                        "family": "gemma4",
                        "format": "gguf",
                    },
                    "model_info": {"gemma4.context_length": 131072},
                    "capabilities": ["completion", "tools"],
                },
                "/v1/models": {"data": [{"id": "gemma4:e4b", "owned_by": "library"}]},
            }
        ),
    )
    assert found["kind"] == "ollama" and found["version"] == "0.13.5"
    assert found["params"] == "8.0B" and found["quantization"] == "Q4_K_M"
    assert found["context_length"] == 131072 and found["family"] == "gemma4"
    assert found["models_entry"]["owned_by"] == "library"
    assert "api_key" not in str(found)


async def test_probe_llama_cpp_reads_props_and_guesses_quantization_from_the_file() -> None:
    cfg = LLMSettings(base_url="http://llama:8080/v1", model="chat")
    found = await probe_backend(
        cfg,
        transport=routes(
            {
                "/props": {
                    "default_generation_settings": {"n_ctx": 8192},
                    "build_info": "b6789-abc",
                    "model_path": "/models/Qwen3-14B-Q5_K_M.gguf",
                },
                "/v1/models": {
                    "data": [
                        {"id": "chat", "meta": {"n_params": 14_000_000_000, "size": 9_000_000_000}}
                    ]
                },
            }
        ),
    )
    assert found["kind"] == "llama.cpp" and found["version"] == "b6789-abc"
    assert found["context_length"] == 8192 and found["quantization"] == "Q5_K_M"
    assert found["parameter_count"] == 14_000_000_000


async def test_probe_vllm_and_generic_and_unreachable() -> None:
    cfg = LLMSettings(base_url="http://vllm:8000/v1", model="m")
    vllm = await probe_backend(
        cfg,
        transport=routes(
            {
                "/version": {"version": "0.11.0"},
                "/v1/models": {"data": [{"id": "m", "max_model_len": 32768, "owned_by": "vllm"}]},
            }
        ),
    )
    assert (
        vllm["kind"] == "vllm" and vllm["version"] == "0.11.0" and vllm["context_length"] == 32768
    )
    generic = await probe_backend(
        cfg, transport=routes({"/v1/models": {"data": [{"id": "m", "owned_by": "openai"}]}})
    )
    assert (
        generic["kind"] == "openai-compatible" and generic["models_entry"]["owned_by"] == "openai"
    )

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert (await probe_backend(cfg, transport=httpx.MockTransport(down)))[
        "kind"
    ] == "openai-compatible"
    assert (await probe_backend(LLMSettings(), transport=httpx.MockTransport(down)))[
        "version"
    ] is None


async def test_probe_sends_the_api_key_only_as_a_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404)

    cfg = LLMSettings(base_url="http://x/v1", model="m", api_key="sk-secret-1")
    await probe_backend(cfg, transport=httpx.MockTransport(handler))
    assert seen and all(r.headers["authorization"] == "Bearer sk-secret-1" for r in seen)
    assert all("sk-secret-1" not in str(r.url) for r in seen)


# --------------------------------------------------------------------------- judge


@pytest.mark.parametrize(
    ("text", "score"),
    [
        ('{"score": 4, "reason": "good"}', 4),
        ('```json\n{"score": "5", "reason": "all there"}\n```', 5),
        ('<think>hmm</think>{"score": 2.4, "reason": "x"}', 2),
        ("I would say 3/5 because it misses the second item", 3),
        ('{"score": 9}', None),
        ("no idea", None),
    ],
)
def test_parse_grade(text: str, score: int | None) -> None:
    assert judge.parse_grade(text)[0] == score


async def test_grade_calls_the_judge_endpoint_with_the_rubric() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(
            {"auth": request.headers.get("authorization"), "body": json.loads(request.content)}
        )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"score": 4, "reason": "close"}'}}]}
        )

    cfg = BenchJudgeSettings(enabled=True, base_url="http://judge/v1", model="big", api_key="k-1")
    score, reason = await judge.grade(
        cfg,
        task="Which unit failed?",
        rubric="Names logrotate.service",
        answer="logrotate",
        tools=["get_status"],
        transport=httpx.MockTransport(handler),
    )
    assert (score, reason) == (4, "close")
    body = seen[0]["body"]
    assert body["model"] == "big" and seen[0]["auth"] == "Bearer k-1"
    assert "Names logrotate.service" in body["messages"][1]["content"]
    assert "get_status" in body["messages"][1]["content"]


async def test_grade_never_raises() -> None:
    cfg = BenchJudgeSettings(enabled=True, base_url="http://judge/v1", model="big")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    assert (
        await judge.grade(
            cfg, task="t", rubric="r", answer="a", tools=[], transport=httpx.MockTransport(down)
        )
    )[0] is None
    bad = httpx.MockTransport(lambda r: httpx.Response(500))
    assert (
        "HTTP 500"
        in (await judge.grade(cfg, task="t", rubric="r", answer="a", tools=[], transport=bad))[1]
    )


def test_judge_is_off_until_configured() -> None:
    assert Settings().bench.judge.enabled is False
    assert not BenchJudgeSettings(enabled=True, model="m").configured
    assert BenchJudgeSettings(enabled=True, base_url="http://x/v1", model="m").configured
    assert not BenchJudgeSettings(base_url="http://x/v1", model="m").configured


def test_judge_grade_is_stored_beside_the_rule_score_never_mixed(bootstrap: Any) -> None:
    app = make_app(
        bootstrap,
        FakeLLM(say(CANNED_REPORT)),
        bench={"judge": {"enabled": True, "base_url": "http://judge/v1", "model": "big"}},
    )
    with TestClient(app) as client:
        bench = app.state.ctx.bench
        scenario = bench.select(ids=["report.smoke_disk"])[0]
        scenario.judge = (
            scenario.judge
            or Scenario.model_validate(
                {
                    **scenario.model_dump(mode="json"),
                    "judge": {"rubric": "Mentions disk and logrotate"},
                }
            ).judge
        )
        bench._judge_transport = httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"choices": [{"message": {"content": '{"score": 2, "reason": "meh"}'}}]}
            )
        )
        bench.select = lambda **kw: [scenario]  # type: ignore[method-assign]
        run = run_bench(client, app, ids=["report.smoke_disk"], repeats=1)
        attempt = client.get(
            f"/api/bench/runs/{run['id']}/attempts/{run['attempts'][0]['id']}"
        ).json()
        assert attempt["judge_score"] == 2 and attempt["judge_reason"] == "meh"
        assert attempt["score"] == pytest.approx(1.0)  # the rule score is untouched
        assert run["meta"]["judge"]["model"] == "big"


# --------------------------------------------------------------------------- summary


def row(scenario: str, group: str, score: float, **kw: Any) -> dict[str, Any]:
    return {
        "scenario_id": scenario,
        "grp": group,
        "score": score,
        "status": kw.pop("status", "ok"),
        "total_ms": kw.pop("total_ms", 1000),
        "ttft_ms": kw.pop("ttft_ms", 100),
        "tokens_per_s": kw.pop("tokens_per_s", 50.0),
        "tokens_in": 10,
        "tokens_out": 5,
        **kw,
    }


def test_summary_means_scenarios_then_groups_then_overall() -> None:
    s = summarize(
        [
            row("chat.a", "chat", 1.0, total_ms=1000),
            row("chat.a", "chat", 0.5, total_ms=3000),
            row("chat.b", "chat", 0.0, status="timeout", total_ms=9000),
            row("report.a", "report", 1.0, report_overlap=True),
        ]
    )
    scen = s["summary"]["scenarios"]
    assert scen["chat.a"]["score"] == 0.75 and scen["chat.a"]["passed"] == 1
    assert scen["chat.b"]["failed"] == 1
    assert s["group_scores"] == {"chat": pytest.approx(0.375), "report": 1.0}
    assert s["overall"] == pytest.approx((0.375 + 1.0) / 2)
    summary = s["summary"]
    assert summary["attempts"] == 4 and summary["failures"] == 1
    assert summary["pass_rate"] == pytest.approx(0.5)
    assert summary["median_total_ms"] == 1000  # timeouts are left out of the timings
    assert summary["median_tokens_per_s"] == 50.0
    assert summary["overlapped"] == 1


def test_summary_of_nothing_is_empty() -> None:
    s = summarize([])
    assert s["overall"] is None and s["summary"]["pass_rate"] is None


# --------------------------------------------------------------------------- snapshots


def test_capture_this_server_as_a_private_snapshot_and_run_against_it(bootstrap: Any) -> None:
    app = make_app(bootstrap, FakeLLM(say("the host is box")))
    with TestClient(app) as client:
        ctx = app.state.ctx
        client.portal.call(ctx.collectors.run, "system")
        bad = client.post("/api/bench/snapshots", json={"name": "bad name!"})
        assert bad.status_code == 400
        done = client.post("/api/bench/snapshots", json={"name": "home", "description": "my box"})
        assert done.status_code == 201, done.text
        assert done.json()["reference"] == "private:home" and "system" in done.json()["collectors"]
        listed = client.get("/api/bench/snapshots").json()["items"]
        assert [s["name"] for s in listed] == ["home"] and listed[0]["description"] == "my box"
        doc = client.portal.call(ctx.bench.store.snapshot_doc, "home")
        assert doc["collectors"]["system"]["data"]["hostname"] == "box"
        assert doc["now"] > 0

        scenario = Scenario.model_validate(
            {
                "id": "chat.private",
                "group": "chat",
                "title": "Private snapshot",
                "description": "Runs against the captured server.",
                "snapshot": "private:home",
                "input": {"kind": "chat", "messages": [{"role": "user", "content": "host?"}]},
                "checks": [{"type": "answer_contains_all", "values": ["box"]}],
            }
        )
        ctx.bench.select = lambda **kw: [scenario]  # type: ignore[method-assign]
        run = run_bench(client, app, ids=["chat.private"], repeats=1)
        assert run["attempts"][0]["score"] == pytest.approx(1.0), run["attempts"]
        assert client.delete("/api/bench/snapshots/home").status_code == 204
        assert client.delete("/api/bench/snapshots/home").status_code == 404


def test_capture_before_any_collection_is_an_error(bootstrap: Any) -> None:
    app = make_app(bootstrap, FakeLLM())
    with TestClient(app) as client:
        client.portal.call(app.state.ctx.collectors.close)
        app.state.ctx.collectors.latest.clear()
        assert client.post("/api/bench/snapshots", json={"name": "x"}).status_code == 400


def test_unknown_private_snapshot_fails_the_attempt_with_the_reason(bootstrap: Any) -> None:
    app = make_app(bootstrap, FakeLLM())
    with TestClient(app) as client:
        scenario = Scenario.model_validate(
            {
                "id": "chat.ghost",
                "group": "chat",
                "title": "Ghost snapshot",
                "description": "References a snapshot that does not exist.",
                "snapshot": "private:ghost",
                "input": {"kind": "chat", "messages": [{"role": "user", "content": "x"}]},
                "checks": [{"type": "max_tool_calls", "n": 3}],
            }
        )
        app.state.ctx.bench.select = lambda **kw: [scenario]  # type: ignore[method-assign]
        run = run_bench(client, app, ids=["chat.ghost"], repeats=1)
        attempt = run["attempts"][0]
        assert attempt["status"] == "error" and "ghost" in attempt["error"]


# --------------------------------------------------------------------------- settings


def test_bench_settings_show_up_in_the_settings_schema(bootstrap: Any) -> None:
    app = make_app(bootstrap, FakeLLM())
    with TestClient(app) as client:
        doc = client.get("/api/settings").json()
        assert doc["sections"]["bench"] == "Benchmarks"
        props = doc["schema"]["properties"]["bench"]
        assert props["$ref"].endswith("BenchSettings") or "properties" in props
        assert doc["values"]["bench"]["repeats"] == 3
        assert doc["values"]["bench"]["judge"]["enabled"] is False
        assert doc["values"]["bench"]["judge"]["api_key"] in ("", None) or "*" in str(
            doc["values"]["bench"]["judge"]["api_key"]
        )
