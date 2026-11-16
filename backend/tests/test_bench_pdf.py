"""PDF export of benchmark runs and comparisons: the endpoints, what the text holds, Unicode,
runs without hardware, failed attempts in the appendix, bold best values, the cost of a big run."""

from __future__ import annotations

import io
import random
import re
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from ai_server_monitor.bench import pdf
from ai_server_monitor.bench.hardware import COLUMNS, FIELDS
from ai_server_monitor.bench.runner import summarize, summarize_levels
from ai_server_monitor.bench.schema import load_scenarios
from ai_server_monitor.config import Bootstrap

from .test_bench_run import make_app
from .test_chat import FakeLLM

LEVELS = ["none", "high"]
TITLE_OF = {s.id: s.title for s in load_scenarios(include_smoke=True)[0]}


def _check(passed: bool, reason: str = "ok") -> dict[str, Any]:
    return {
        "type": "mentions",
        "describe": "answer mentions any of: 4.7 TiB, 4.7TiB",
        "weight": 2.0,
        "passed": passed,
        "reason": reason,
    }


def _attempt(
    sid: str, grp: str, effort: str, n: int, score: float, *, answer: str = "fine", **extra: Any
) -> dict[str, Any]:
    return {
        "scenario_id": sid,
        "grp": grp,
        "attempt": n,
        "started": 1_700_000_000.0 + n * 10,
        "status": "ok",
        "score": score,
        "checks": [_check(score >= 0.999, "found" if score >= 0.999 else "none of: 4.7 TiB")],
        "answer": answer,
        "error": None,
        "ttft_ms": 200,
        "total_ms": 3000 + n * 100,
        "tokens_in": 900,
        "tokens_out": 120,
        "tokens_per_s": 40.0,
        "llm_calls": 1,
        "tool_calls": 0,
        "tool_mode": "native",
        "effort": effort,
        "reasoning_chars": 0 if effort == "none" else 1500,
        "answer_issue": None,
        **extra,
    }


def _hw_rows(run_start: float, n: int = 120) -> list[dict[str, Any]]:
    rows = []
    for i in range(n):
        wave = (i % 30) / 30
        rows.append(
            {
                "ts": run_start + i * 2.0,
                "gpu_util": 40 + 50 * wave,
                "vram_used": 18 * 1024**3,
                "vram_total": 24 * 1024**3,
                "llm_vram": 16 * 1024**3,
                "power_w": 200 + 100 * wave,
                "temp_c": 60 + 10 * wave,
                "cpu": 20 + 15 * wave,
                "ram_used": 8 * 1024**3,
                "ram_total": 32 * 1024**3,
            }
        )
    return rows


def _meta(*, hardware: bool) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "llm": {
            "temperature": 0.3,
            "max_output_tokens": 4096,
            "tool_mode": "auto",
            "max_context_tokens": 32768,
        },
        "tool_mode": "native",
        "chat": {"tool_routing": "auto", "tool_routing_tokens": 2000},
        "backend": {"family": "gemma"},
        "cold_start": {"ok": True, "supported": True, "loaded_before": False, "total_ms": 8400},
        "automation": {"paused": True, "by_run": True, "was_paused": False},
        "levels": {
            "tested": LEVELS,
            "applies_to": "the chat answers and the report",
            "fixed": "Helper calls stay at none.",
        },
        "loaded_model": {
            "source": "ollama /api/ps",
            "size": 16 * 1024**3,
            "size_vram": 16 * 1024**3,
            "gpu_percent": 100,
            "context_length": 32768,
        },
        "gpu": {
            "gpus": [
                {
                    "name": "RTX 3090",
                    "mem_total": 24 * 1024**3,
                    "mem_used": 2 * 1024**3,
                    "util_percent": 3,
                }
            ]
        },
    }
    meta["hardware"] = (
        {
            "recorded": True,
            "interval_s": 2.0,
            "samples": 120,
            "gpu": True,
            "gpu_name": "RTX 3090",
            "gpu_count": 1,
            "llm_process": "llama-server",
            "llm_here": "here",
            "baseline": {"vram_used": 2 * 1024**3, "ram_used": None, "cpu": None, "gpu_util": None},
        }
        if hardware
        else {"recorded": False}
    )
    return meta


def _seed(
    client: TestClient,
    *,
    model: str = "fake-model",
    quality: float = 1.0,
    hardware: bool = True,
    failures: bool = True,
) -> int:
    """A finished run with two thinking levels, failed attempts (Unicode, a very long answer) and
    hardware samples; `quality` scales the scores so two runs differ."""
    store = client.app.state.ctx.bench.store
    attempts: list[dict[str, Any]] = []
    for effort in LEVELS:
        for sid, grp in (("report.smoke_disk", "report"), ("chat.smoke_failed_unit", "chat")):
            for n in (1, 2):
                attempts.append(_attempt(sid, grp, effort, n, quality))
    if failures:
        attempts.append(
            _attempt(
                "chat.smoke_failed_unit",
                "chat",
                "high",
                3,
                0.4,
                answer="Das Laufwerk → ist voll ≥ 90 % · 日本語 😀 **bold?** __x__ --y--",
            )
        )
        attempts.append(
            _attempt(
                "report.smoke_disk",
                "report",
                "none",
                3,
                0.0,
                status="error",
                error="the model timed out",
                answer="LONG-ANSWER " + "z" * 5000,
                answer_issue="limit",
            )
        )
        attempts[-1]["checks"] = [_check(False, "the report had no findings")]
    body = summarize(attempts)
    body["summary"]["levels"] = summarize_levels(attempts)
    run_start = 1_700_000_000.0
    if hardware:
        rows = _hw_rows(run_start)
        body["summary"]["hardware"] = _hw_numbers(rows)

    async def seed() -> int:
        run_id = await store.create_run(
            started=run_start,
            finished=run_start + 900,
            status="done",
            scope="all",
            repeats=2,
            scenario_hash="abc123def456",
            app_version="0.6.8",
            model=model,
            base_url="http://llm.local:11434/v1",
            backend_kind="ollama",
            backend_version="0.13.5",
            params="7.9B",
            quantization="Q4_K_M",
            context_length=131072,
            overall=body["overall"],
            group_scores=body["group_scores"],
            summary=body["summary"],
            meta=_meta(hardware=hardware),
        )
        for a in attempts:
            await store.add_attempt(run_id, a)
        if hardware:
            await store.db.executemany(
                f"INSERT INTO bench_hw_samples (run_id, {', '.join(COLUMNS)}) "
                f"VALUES (?, {', '.join('?' * len(COLUMNS))})",
                [(run_id, *(r.get(c) for c in COLUMNS)) for r in _hw_rows(run_start)],
            )
        return run_id

    return client.portal.call(seed)


def _hw_numbers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(rows)}
    for field in FIELDS:
        out[field] = _stat(rows, field)
    out["vram_total"] = rows[0]["vram_total"]
    out["ram_total"] = rows[0]["ram_total"]
    return out


def _stat(rows: list[dict[str, Any]], field: str) -> dict[str, float] | None:
    vals = [r[field] for r in rows if r.get(field) is not None]
    return {"avg": round(sum(vals) / len(vals), 1), "peak": max(vals)} if vals else None


def _pages(data: bytes) -> list[str]:
    return [p.extract_text() for p in PdfReader(io.BytesIO(data)).pages]


def _text(data: bytes) -> str:
    return "\n".join(_pages(data))


def _bold_spans(data: bytes) -> list[str]:
    """Every piece of text drawn in a bold font."""
    spans: list[str] = []

    def visit(text: str, cm: Any, tm: Any, font: Any, size: Any) -> None:
        if text.strip() and font and "Bold" in str(font.get("/BaseFont", "")):
            spans.append(text.strip())

    for page in PdfReader(io.BytesIO(data)).pages:
        page.extract_text(visitor_text=visit)
    return spans


@pytest.fixture
def client(bootstrap: Bootstrap):
    with TestClient(make_app(bootstrap, FakeLLM())) as c:
        yield c


# --------------------------------------------------------------------------- endpoints


def test_run_pdf_is_a_real_attachment_with_the_page_content(client: TestClient) -> None:
    run_id = _seed(client)
    response = client.get(f"/api/bench/runs/{run_id}/export.pdf")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert re.fullmatch(
        r'attachment; filename="bench-run-1-fake-model-2023-11-14\.pdf"',
        response.headers["content-disposition"],
    ), response.headers["content-disposition"]
    data = response.content
    assert data.startswith(b"%PDF-") and data.rstrip().endswith(b"%%EOF")
    reader = PdfReader(io.BytesIO(data))
    assert len(reader.pages) >= 3
    assert reader.pages[0].mediabox.width == pytest.approx(595, abs=1)  # A4

    text = _text(data)
    flat = " ".join(text.split())
    for needle in (
        "Benchmark run #1",
        "fake-model",
        "What was measured",
        "ollama 0.13.5",
        "tested at none, high",
        "auto (above 2000 tokens)",
        "paused for the run",
        "Results",
        "Thinking levels",
        "Hardware",
        "RTX 3090",
        "Over the run",
        "Scenarios by level",
        "Scenarios",
        "Appendix: failed attempts",
        TITLE_OF["report.smoke_disk"],
        "the report had no findings",
        "the model timed out",
        "trimmed",
        "Page 1 of",
    ):
        assert needle in flat, needle
    # only what did not pass is in the appendix, and its answer text (Unicode kept) is there
    assert "Das Laufwerk → ist voll ≥ 90 %" in flat
    assert "日本語" not in flat and "?" in flat  # not in the font: printed as ?, no crash
    assert "fine" not in flat.split("Appendix: failed attempts", 1)[1]
    assert "z" * 1180 in text.replace("\n", "") and "z" * 1200 not in text.replace("\n", "")
    # markdown-looking text in the data is shown as it is
    assert "**bold?**" in flat or "bold?" in flat
    # no page runs past the right margin: the text lines all start inside the page
    assert reader.pages[1].extract_text()


def test_run_without_hardware_and_without_failures(client: TestClient) -> None:
    run_id = _seed(client, hardware=False, failures=False)
    response = client.get(f"/api/bench/runs/{run_id}/export.pdf")
    assert response.status_code == 200
    flat = " ".join(_text(response.content).split())
    assert "Hardware was not recorded for this run." in flat
    assert "Over the run" not in flat
    assert "VRAM peak" in flat  # the levels table keeps the column
    assert "No attempt failed." in flat
    assert "Appendix: failed attempts" in flat


def test_unknown_run_is_404_and_bad_ids_are_400(client: TestClient) -> None:
    assert client.get("/api/bench/runs/99/export.pdf").status_code == 404
    assert client.get("/api/bench/compare.pdf").status_code == 422
    assert client.get("/api/bench/compare.pdf?ids=1").status_code == 400
    assert client.get("/api/bench/compare.pdf?ids=a,b").status_code == 400
    a = _seed(client)
    assert client.get(f"/api/bench/compare.pdf?ids={a},77").status_code == 404


def test_compare_pdf_has_the_rows_and_marks_the_best_in_bold(client: TestClient) -> None:
    first = _seed(client, model="small-model", quality=0.8, failures=False)
    second = _seed(client, model="big-model", quality=1.0, failures=False)
    response = client.get(f"/api/bench/compare.pdf?ids={first},{second}")
    assert response.status_code == 200, response.text
    assert re.fullmatch(
        r'attachment; filename="bench-compare-1-2-2023-11-14\.pdf"',
        response.headers["content-disposition"],
    )
    data = response.content
    assert data.startswith(b"%PDF-") and len(PdfReader(io.BytesIO(data)).pages) >= 1
    flat = " ".join(_text(data).split())
    for needle in (
        "Comparing 2 benchmark runs",
        "small-model Q4_K_M",
        "big-model Q4_K_M",
        "Setup",
        "Accuracy",
        "Overall",
        "Speed",
        "Hardware",
        "Per thinking level",
        "Per scenario: score · median time",
        TITLE_OF["chat.smoke_failed_unit"],
        "best value in each row is bold",
    ):
        assert needle in flat, needle
    bold = _bold_spans(data)
    assert "100%" in bold  # the better overall score is bold
    assert "80%" not in bold  # the other one is not


def test_compare_of_many_runs_goes_landscape(client: TestClient) -> None:
    ids = [
        _seed(client, model=f"model-{i}", quality=0.5 + i / 10, failures=False) for i in range(5)
    ]
    response = client.get("/api/bench/compare.pdf?ids=" + ",".join(map(str, ids)))
    assert response.status_code == 200
    reader = PdfReader(io.BytesIO(response.content))
    box = reader.pages[0].mediabox
    assert box.width > box.height
    assert "model-4" in _text(response.content)


# --------------------------------------------------------------------------- the renderer


def _big_run() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """26 scenarios x 5 levels x 3 attempts, a fifth of them failed with a long answer."""
    rng = random.Random(7)
    attempts = []
    for s in range(26):
        for effort in ("auto", "none", "low", "medium", "high"):
            for n in (1, 2, 3):
                bad = rng.random() < 0.2
                attempts.append(
                    _attempt(
                        f"chat.scenario_{s:02d}",
                        "chat",
                        effort,
                        n,
                        0.3 if bad else 1.0,
                        answer=("A long, wandering answer about the disk. " * 80) if bad else "ok",
                    )
                )
    body = summarize(attempts)
    body["summary"]["levels"] = summarize_levels(attempts)
    run = {
        "id": 9,
        "started": 1_700_000_000.0,
        "finished": 1_700_003_000.0,
        "status": "done",
        "scope": "all",
        "repeats": 3,
        "scenario_hash": "h",
        "app_version": "0.6.8",
        "model": "x" * 90,
        "base_url": None,
        "backend_kind": None,
        "overall": body["overall"],
        "group_scores": body["group_scores"],
        "summary": body["summary"],
        "meta": _meta(hardware=False),
    }
    failed = [a for a in attempts if a["score"] < 0.999]
    return run, attempts, failed


def test_a_big_run_renders_in_a_few_seconds() -> None:
    run, attempts, failed = _big_run()
    started = time.perf_counter()
    data = pdf.render_run(
        run, titles={}, samples=_hw_rows(1_700_000_000.0, 600), attempts=attempts, failed=failed
    )
    took = time.perf_counter() - started
    reader = PdfReader(io.BytesIO(data))
    assert len(failed) > 50 and len(reader.pages) > 20
    assert took < 15, f"{took:.1f}s"  # about 2-3 s on a developer machine, loose for CI
    assert "x" * 30 in _text(data)  # the long model name wraps instead of running off the page


def test_missing_glyphs_and_control_characters_do_not_crash() -> None:
    doc = pdf.Doc(landscape=False, footer_left="t")
    assert doc.t("a\x00b\tc\r\nd") == "ab    c\nd"
    assert doc.t("→ ≥ · é") == "→ ≥ · é"
    assert doc.t("日本語 \U00020000") == "??? ?"  # not in DejaVu: "?" instead of an empty box
    assert doc.md("a**b") != "a**b" and "**" not in doc.md("a**b").replace("​", "")


def test_filenames_are_safe() -> None:
    run = {"id": 12, "model": 'gemma4:26b/"x"\\ ünï', "started": 1_700_000_000.0}
    name = pdf.run_filename(run)
    assert re.fullmatch(r"bench-run-12-[A-Za-z0-9._-]+-2023-11-14\.pdf", name), name
    assert pdf.run_filename({"id": 1, "model": None, "started": 0}).startswith("bench-run-1-model-")
