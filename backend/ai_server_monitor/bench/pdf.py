"""PDF export of benchmark results: one run (what the run page shows, plus an appendix of the failed
attempts) or several runs side by side (what the Compare page shows).

Built with fpdf2 (pure Python, so the image needs no system libraries). The text is real text and
the load chart is drawn with vector lines. The look follows the web pages on a white page: scores
in green / amber / red with the page's thresholds, the best value of a row on a light green fill
(and bold), tinted section headings, a tinted heading row per thinking level, rows that alternate
in tint, headers that repeat on every page a table spans. The drawing lives in `pdf_doc.py`;
`render_run` and `render_compare` here are plain functions of the data, and the API loads the data
and runs them in a worker thread."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from .. import __version__
from .hardware import columnar, thin
from .pdf_doc import (
    BEST_FILL,
    CRIT,
    FAINT,
    INK,
    MUTED,
    PALETTE,
    RULE,
    SUB_FILL,
    Band,
    Doc,
    Gap,
    S,
    Sub,
    TCell,
    cell,
    score_colour,
)

__all__ = ["PALETTE", "Doc"]

EFFORTS = ("auto", "none", "low", "medium", "high")
EFFORT_HELP = {
    "auto": "not sent: the model decides",
    "none": "no thinking",
    "low": "a little",
    "medium": "some",
    "high": "a lot",
}
GROUP_SHORT = {"report": "Reports", "chat": "Chat", "prom_plan": "Prom", "robust": "Robust"}

ANSWER_LIMIT = 1200
"""Characters of an answer kept in the appendix; the rest is cut and said so."""

GIB = 1024**3
DASH = "—"


# --------------------------------------------------------------------------- number formats
# The same wording as the web page (frontend/src/pages/bench/format.ts).


def _rnd(v: float) -> int:
    return int(v + 0.5) if v >= 0 else -int(-v + 0.5)


def ms(v: float | None) -> str:
    if v is None:
        return DASH
    if v < 1:
        return "<1 ms"
    if v < 1000:
        return f"{_rnd(v)} ms"
    if v < 60_000:
        return f"{v / 1000:.{2 if v < 10_000 else 1}f} s"
    return f"{int(v // 60_000)}m {_rnd((v % 60_000) / 1000)}s"


def score(v: float | None) -> str:
    return DASH if v is None else f"{_rnd(v * 100)}%"


def tps(v: float | None) -> str:
    return DASH if v is None else f"{v:.{1 if v < 10 else 0}f} t/s"


def gb(v: float | None) -> str:
    return DASH if v is None else f"{v / GIB:.{1 if v / GIB < 10 else 0}f} GB"


def pct(v: float | None) -> str:
    return DASH if v is None else f"{_rnd(v)}%"


def watts(v: float | None) -> str:
    return DASH if v is None else f"{_rnd(v)} W"


def gb_of(v: float | None, total: float | None) -> str:
    if v is None:
        return DASH
    return f"{gb(v)} · {_rnd(v / total * 100)}%" if total else gb(v)


def chars(n: float | None) -> str:
    if n is None:
        return DASH
    if n >= 10_000:
        return f"{_rnd(n / 1000)}k"
    return f"{n / 1000:.1f}k" if n >= 1000 else str(_rnd(n))


def clock(s: float) -> str:
    t = max(0, _rnd(s))
    h, m, sec = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def stamp(ts: float | None) -> str:
    if not ts:
        return DASH
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")


def day(ts: float | None) -> str:
    return datetime.fromtimestamp(ts or 0, UTC).strftime("%Y-%m-%d")


def elapsed(start: float, end: float | None) -> str:
    s = max(0, _rnd((end or start) - start))
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def effort_label(e: str | None) -> str:
    return e or DASH


def backend_label(run: dict[str, Any]) -> str:
    base = run.get("base_url") or ""
    host = re.sub(r"/v1/?$", "", re.sub(r"^https?://", "", base)) if base else ""
    kind = run.get("backend_kind")
    if kind and kind != "openai-compatible":
        version = run.get("backend_version")
        kind = f"{kind} {version}" if version else kind
    else:
        kind = "OpenAI-compatible"
    return f"{kind} · {host}" if host else kind


def size_label(run: dict[str, Any]) -> str:
    ctx = run.get("context_length")
    parts = [
        run.get("params"),
        run.get("quantization"),
        f"{_rnd(ctx / 1024)}k ctx" if ctx else None,
    ]
    return " · ".join(str(p) for p in parts if p) or DASH


def run_label(run: dict[str, Any]) -> str:
    quant = run.get("quantization")
    return f"{run.get('model') or '?'}{f' {quant}' if quant else ''}"


def routing_label(run: dict[str, Any]) -> str:
    chat = (run.get("meta") or {}).get("chat") or {}
    routing = chat.get("tool_routing")
    if not routing:
        return "not recorded"
    if routing == "auto" and chat.get("tool_routing_tokens"):
        return f"auto (above {chat['tool_routing_tokens']} tokens)"
    return str(routing)


def cold_start_text(c: dict[str, Any] | None) -> str:
    if not c or not c.get("ok"):
        return DASH
    loaded = c.get("loaded_before")
    state = (
        "load state unknown"
        if loaded is None
        else "model was already loaded"
        if loaded
        else "model was not loaded"
    )
    return f"{ms(c.get('total_ms'))} ({state})"


def automation_text(a: dict[str, Any] | None) -> str:
    if not a or not a.get("paused"):
        return "running (not paused)"
    return "paused for the run" if a.get("by_run") else "paused (already paused before the run)"


def gpu_text(gpu: dict[str, Any] | None, hardware: dict[str, Any] | None = None) -> str:
    """The GPU line of the run's facts: from the start-of-run GPU snapshot when it has one, else
    the name and count the hardware recorder noted ("NVIDIA GeForce RTX 3090 ×2")."""
    gpus = (gpu or {}).get("gpus") or []
    if not gpus:
        name = (hardware or {}).get("gpu_name")
        if not name:
            return "not available"
        count = (hardware or {}).get("gpu_count") or 0
        return f"{name} ×{count}" if count > 1 else str(name)
    out = []
    for g in gpus:
        text = str(g.get("name") or "GPU")
        total, used = g.get("mem_total"), g.get("mem_used")
        if total:
            text += f", {_rnd(total / GIB)} GB"
        if used is not None and total:
            text += f", {_rnd(used / total * 100)}% used at start"
        if g.get("util_percent") is not None:
            text += f", {g['util_percent']}% busy"
        out.append(text)
    return "; ".join(out)


def hw_scope(run: dict[str, Any]) -> str:
    """ "old" (not recorded), "here", "elsewhere" (the LLM server is on another machine) or
    "unknown"."""
    hw = (run.get("meta") or {}).get("hardware")
    if not hw or not hw.get("recorded"):
        return "old"
    where = hw.get("llm_here")
    return where if where in ("here", "elsewhere") else "unknown"


PROVIDERS = {"ollama": "Ollama", "llama.cpp": "llama.cpp", "vllm": "vLLM"}


def hardware_label(meta: dict[str, Any] | None, backend_kind: str | None = None) -> str:
    """Where the hardware numbers were taken from: the model's process on this host, named after
    the provider the run recorded (Ollama's model server is a process called llama-server)."""
    if not meta or not meta.get("recorded"):
        return "not recorded"
    if not meta.get("gpu"):
        return "no GPU on this host"
    if meta.get("llm_here") == "elsewhere":
        return "this host; the LLM server is elsewhere"
    if meta.get("llm_here") == "here":
        provider = PROVIDERS.get(backend_kind or "")
        who = f"{provider}'s model process" if provider else "the LLM server's process"
        return f"{who} on this host ({meta.get('llm_process')})"
    return "this host; model process not recognised"


def sampling_text(llm: dict[str, Any], key: str) -> str:
    """temperature / top_p / top_k as they applied: the value and where it came from ("1 (model
    default)"); runs made before the field was recorded say so."""
    shown = (llm.get("effective") or {}).get(key)
    if shown and shown.get("text"):
        return str(shown["text"])
    if key not in llm or llm[key] is None:
        return "not recorded"
    return f"{llm[key]} (set)" if llm[key] != "auto" else "auto · not recorded"


def vram_stat(hw: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if not hw else hw.get("llm_vram") or hw.get("vram_used")


def levels_of(run: dict[str, Any]) -> list[str]:
    levels = ((run.get("summary") or {}).get("levels")) or {}
    return [e for e in EFFORTS if levels.get(e)]


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")[:limit] or "run"


def run_filename(run: dict[str, Any]) -> str:
    model = slug(str(run.get("model") or "model"))
    return f"bench-run-{run['id']}-{model}-{day(run.get('started'))}.pdf"


def compare_filename(runs: Sequence[dict[str, Any]]) -> str:
    ids = "-".join(str(r["id"]) for r in runs)
    return f"bench-compare-{ids}-{day(max(r.get('started') or 0 for r in runs))}.pdf"


# --------------------------------------------------------------------------- run report


def _meta(run: dict[str, Any]) -> dict[str, Any]:
    return run.get("meta") or {}


def _facts(run: dict[str, Any]) -> list[tuple[str, str]]:
    meta = _meta(run)
    llm = meta.get("llm") or {}
    backend = meta.get("backend") or {}
    levels = meta.get("levels")
    thinking = (
        f"tested at {', '.join(levels['tested'])}"
        if levels and levels.get("tested")
        else str(llm.get("reasoning_effort") or DASH)
    )
    size = size_label(run)
    if backend.get("family"):
        size += f" · family {backend['family']}"
    tool_mode = str(llm.get("tool_mode") or DASH)
    if meta.get("tool_mode") and llm.get("tool_mode") != meta["tool_mode"]:
        tool_mode += f" → used {meta['tool_mode']}"
    scope = (
        "all"
        if run.get("scope") == "all"
        else str(run.get("scope", "")).replace("group:", "group ")
    )
    items = [
        ("Model", str(run.get("model") or DASH)),
        ("Backend", backend_label(run)),
        ("Size", size),
        (
            "Settings",
            f"temperature {sampling_text(llm, 'temperature')} · "
            f"top P {sampling_text(llm, 'top_p')} · top K {sampling_text(llm, 'top_k')} · "
            "max output "
            f"{llm.get('max_output_tokens', DASH)} tokens · thinking {thinking} · context setting "
            f"{llm.get('max_context_tokens', DASH)}",
        ),
        ("Tool mode", tool_mode),
        (
            "Tool routing",
            f"{routing_label(run)}. The chat setting at the start of the run: which tool groups "
            "the model gets per question. The classifier call counts in an attempt's LLM calls.",
        ),
        (
            "Cold start",
            f"{cold_start_text(meta.get('cold_start'))}. Warm-up request before the first "
            "attempt; not part of any score or timing below.",
        ),
        (
            "Scenarios",
            f"{scope} · {run.get('repeats')} attempt(s) each · "
            f"set {run.get('scenario_hash') or DASH}"
            f" · app {run.get('app_version') or DASH}",
        ),
        (
            "Time",
            f"{stamp(run.get('started'))} · took "
            f"{elapsed(run['started'], run['finished']) if run.get('finished') else DASH}",
        ),
        ("GPU", gpu_text(meta.get("gpu"), meta.get("hardware"))),
        ("Automation", automation_text(meta.get("automation"))),
    ]
    if meta.get("judge"):
        items.append(("Judge", str(meta["judge"].get("model"))))
    return items


def _results(doc: Doc, run: dict[str, Any]) -> None:
    summary = run.get("summary") or {}
    failures = summary.get("failures")
    tiles: list[tuple[str, str, Any]] = [
        ("Overall", score(run.get("overall")), score_colour(run.get("overall"))),
        ("Pass rate", score(summary.get("pass_rate")), None),
        ("Median first token", ms(summary.get("median_ttft_ms")), None),
        ("Median attempt", ms(summary.get("median_total_ms")), None),
        ("Speed", tps(summary.get("median_tokens_per_s")), None),
        (
            "Failed attempts",
            str(failures) if failures is not None else DASH,
            CRIT if failures else None,
        ),
    ]
    if summary.get("empty_answers"):
        tiles.append(("Empty answers", str(summary["empty_answers"]), CRIT))
    doc.tiles(tiles)
    groups = run.get("group_scores") or {}
    if groups:
        doc.grid(
            ["Group", "Score"],
            [
                [
                    GROUP_SHORT.get(g, g),
                    cell(S(score(v), color=score_colour(v)), align="R"),
                ]
                for g, v in groups.items()
            ],
            [60, 24],
            "LR",
        )


def _levels(doc: Doc, run: dict[str, Any]) -> None:
    levels = levels_of(run)
    if not levels:
        return
    stats = run["summary"]["levels"]
    best = max((stats[e].get("overall") or 0) for e in levels)
    rows = []
    for e in levels:
        s = stats[e]
        vram = vram_stat(s.get("hw"))
        issues = " · ".join(
            p
            for p in (
                f"{s['empty']} empty" if s.get("empty") else "",
                f"{s['limit']} cut off" if s.get("limit") else "",
            )
            if p
        )
        top = len(levels) > 1 and (s.get("overall") or 0) == best
        rows.append(
            [
                cell(
                    S(effort_label(e), bold=True, mono=True),
                    S(f"{EFFORT_HELP[e]} · {s['attempts']} attempts", color=MUTED, size=8),
                ),
                _score_cell(s.get("overall"), best=top),
                score(s.get("pass_rate")),
                ms(s.get("median_total_ms")),
                str(s.get("median_tokens_out") if s.get("median_tokens_out") is not None else DASH),
                f"{chars(s['median_reasoning_chars'])} chars"
                if s.get("median_reasoning_chars")
                else DASH,
                cell(S(issues, color=CRIT)) if issues else "0",
                f"{s.get('failed')}/{s['attempts']}",
                gb(vram["peak"]) if vram else DASH,
            ]
        )
    doc.h2("Thinking levels", "every scenario at each level")
    doc.grid(
        [
            "Level",
            "Accuracy",
            "Pass rate",
            "Time per answer",
            "Tokens out",
            "Reasoning",
            "Empty / cut off",
            "Failed",
            "VRAM peak",
        ],
        rows,
        [40, 18, 16, 19, 16, 20, 21, 15, 17],
        "LRRRRRRRR",
        size=7.5,
    )
    note = _meta(run).get("levels")
    text = (
        "Time, tokens out and reasoning are medians per attempt; an attempt that ended without an "
        "answer counts with the time and tokens it spent. Tokens out include the reasoning; the "
        "API "
        "reports no split, so reasoning is counted in characters. Green fill: the best accuracy."
    )
    if note:
        text += f" The level changes {note.get('applies_to')}. {note.get('fixed')}"
    doc.para(text, size=7.5)


def _score_cell(v: float | None, *, best: bool = False, align: str | None = None) -> TCell:
    """A score in its colour; the best one of a row also bold on a light green fill."""
    return cell(
        S(score(v), bold=best, color=score_colour(v)),
        fill=BEST_FILL if best else None,
        align=align,
    )


def _avg_peak(stat: dict[str, Any] | None, fmt: Any) -> tuple[str, str]:
    return (fmt(stat["peak"]), fmt(stat["avg"])) if stat else (DASH, DASH)


def _hardware(
    doc: Doc, run: dict[str, Any], samples: list[dict[str, Any]], attempts: list[dict[str, Any]]
) -> None:
    scope = hw_scope(run)
    meta = _meta(run).get("hardware") or {}
    hw = (run.get("summary") or {}).get("hardware")
    chip = "this host · LLM elsewhere" if scope == "elsewhere" else "this host"
    doc.h2("Hardware", chip if scope != "old" else "")
    if scope == "old":
        doc.para("Hardware was not recorded for this run.", size=8.5, gray=False)
        if meta and not meta.get("recorded"):
            doc.para("Recording was switched off in Settings → Benchmarks when it ran.")
        return
    if scope == "elsewhere":
        doc.para(
            "These are this host's numbers, not the model's. The LLM server is elsewhere: its "
            "endpoint is not a local address and no LLM process showed up on this host's GPU. Host "
            "load while the run went is shown for reference only.",
            size=8.5,
            gray=False,
            bold=True,
        )
    if not hw:
        doc.para("No hardware samples fall inside the attempts of this run.", size=8.5, gray=False)
    else:
        host = "Host " if scope == "elsewhere" else ""
        loaded = _meta(run).get("loaded_model")
        rows: list[list[str]] = []
        if meta.get("gpu"):
            p, a = _avg_peak(hw.get("vram_used"), lambda v: gb_of(v, hw.get("vram_total")))
            rows.append([f"{host}VRAM", p, a, "whole GPU, all programs together"])
            own = hw.get("llm_vram")
            if own:
                p, a = _avg_peak(own, gb)
                rows.append(
                    [
                        "Model's VRAM",
                        p,
                        a,
                        f"held by {meta.get('llm_process') or 'the LLM server'}",
                    ]
                )
            else:
                note = "server is elsewhere" if scope == "elsewhere" else "process not recognised"
                rows.append(["Model's VRAM", DASH, DASH, note])
            p, a = _avg_peak(hw.get("gpu_util"), pct)
            rows.append([f"{host}GPU busy", p, a, ""])
            p, a = _avg_peak(hw.get("power_w"), watts)
            temp = hw.get("temp_c")
            rows.append(
                [f"{host}GPU power", p, a, f"up to {_rnd(temp['peak'])} °C" if temp else ""]
            )
        else:
            rows.append(["GPU", DASH, DASH, "No GPU on this host: no VRAM, GPU or power figures."])
        p, a = _avg_peak(hw.get("cpu"), pct)
        rows.append([f"{host}CPU", p, a, "share of all CPU cores in use"])
        p, a = _avg_peak(hw.get("ram_used"), lambda v: gb_of(v, hw.get("ram_total")))
        rows.append([f"{host}RAM", p, a, "memory in use (total minus available)"])
        if loaded:
            gp = loaded.get("gpu_percent")
            sub = [
                None if gp is None else f"{100 - gp}% on CPU" if gp < 100 else "fully on GPU",
                f"{gb(loaded['size'])} loaded" if loaded.get("size") else None,
                f"{_rnd(loaded['context_length'] / 1024)}k context"
                if loaded.get("context_length")
                else None,
                f"from the backend ({loaded.get('source')})",
            ]
            rows.append(
                [
                    "Model on GPU",
                    DASH if gp is None else f"{gp}%",
                    "",
                    " · ".join(s for s in sub if s),
                ]
            )
        doc.grid(
            ["", "Peak", "Average", "Note"],
            [[cell(S(r[0], bold=True)), r[1], r[2], cell(S(r[3], color=MUTED))] for r in rows],
            [34, 30, 30, doc.inner - 94],
            "LRRL",
            size=8,
        )
        if scope == "unknown" and meta.get("gpu"):
            doc.para(
                "The endpoint is this machine, but the GPU listed no process recognised as an LLM "
                "server, so the VRAM figures cover the whole GPU, including other programs."
            )
        if scope == "here":
            doc.para(
                "Peak and average over the attempts (the warm-up request and the judge are left "
                "out). The whole-GPU figure includes other programs; the model's own VRAM is what "
                "its process holds."
            )
    baseline = meta.get("baseline") or {}
    gpu_name = meta.get("gpu_name")
    count = meta.get("gpu_count") or 0
    line = f"{gpu_name}{f' ×{count}' if count > 1 else ''} · " if gpu_name else ""
    line += f"{meta.get('samples') or 0} samples, about every {meta.get('interval_s') or '?'} s"
    if baseline.get("vram_used"):
        line += f" · {gb(baseline['vram_used'])} of VRAM was already in use before the model loaded"
    doc.para(line)
    if len(samples) > 1:
        doc.load_chart(columnar(thin(samples)), sorted(a["started"] for a in attempts))


def _level_matrix(doc: Doc, run: dict[str, Any], titles: dict[str, str]) -> None:
    levels = levels_of(run)
    stats = (run.get("summary") or {}).get("scenarios") or {}
    ids = [i for i in stats if stats[i].get("levels")]
    if len(levels) < 2 or not ids:
        return
    rows = []
    for sid in ids:
        row: list[TCell | str] = [titles.get(sid, sid)]
        for e in levels:
            s = (stats[sid]["levels"] or {}).get(e)
            if not s:
                row.append(cell(S(DASH, color=FAINT)))
                continue
            lines: list[Any] = [
                [
                    S(score(s["score"]), color=score_colour(s["score"])),
                    S(f" · {ms(s.get('median_total_ms'))}", color=MUTED),
                ]
            ]
            if s.get("empty"):
                lines.append(S(f"{s['empty']} empty", color=CRIT, size=8))
            row.append(cell(*lines))
        rows.append(row)
    first = 62.0
    each = (doc.inner - first) / len(levels)
    doc.h2("Scenarios by level", "score · median time")
    doc.grid(
        ["Scenario", *(effort_label(e) for e in levels)],
        rows,
        [first, *[each] * len(levels)],
        "L" + "R" * len(levels),
        size=7.5,
    )


def _scenario_table(doc: Doc, run: dict[str, Any], titles: dict[str, str]) -> None:
    stats = (run.get("summary") or {}).get("scenarios") or {}
    doc.h2("Scenarios", "every attempt, its checks and its answer are in the JSON export")
    if not stats:
        doc.para("No attempts finished.", size=8.5, gray=False)
        return
    scope = hw_scope(run)
    show_hw = scope != "old"
    host = "Host " if scope == "elsewhere" else ""
    header = ["Scenario", "Group", "Score", "Passed", "Time", "TTFT"]
    widths = [56.0, 17, 14, 17, 15, 15]
    aligns = "LLRRRR"
    if show_hw:
        header += [f"{host}VRAM peak", f"{host}GPU avg", f"{host}CPU avg"]
        widths += [15.0, 13, 13]
        aligns += "RRR"
    widths[0] += doc.inner - sum(widths)
    rows = []
    for sid, s in stats.items():
        passed = [f"{s['passed']}/{s['attempts']}"]
        if s.get("failed"):
            passed.append(S(f"{s['failed']} failed", color=CRIT, size=8))
        row = [
            _scenario_label(titles.get(sid, sid), sid),
            GROUP_SHORT.get(s.get("group"), s.get("group") or DASH),
            _score_cell(s.get("score")),
            cell(*passed),
            ms(s.get("median_total_ms")),
            ms(s.get("median_ttft_ms")),
        ]
        if show_hw:
            hw = s.get("hw")
            vram = vram_stat(hw)
            row += [
                gb(vram["peak"]) if vram else DASH,
                pct(hw["gpu_util"]["avg"]) if hw and hw.get("gpu_util") else DASH,
                pct(hw["cpu"]["avg"]) if hw and hw.get("cpu") else DASH,
            ]
        rows.append(row)
    doc.grid(header, rows, widths, aligns, size=7.8)


def _scenario_label(title: str, sid: str) -> TCell:
    """A scenario's name with its id under it in small grey mono."""
    return cell(S(title), S(sid, color=FAINT, size=8, mono=True))


def _appendix(
    doc: Doc, run: dict[str, Any], failed: list[dict[str, Any]], titles: dict[str, str]
) -> None:
    doc.add_page()
    doc.h2("Appendix: failed attempts", f"{len(failed)} attempt(s) that did not pass")
    doc.para(
        "Every attempt that did not pass (it ended in an error or timeout, or scored under 100%): "
        "the checks that failed with the reason, and the model's answer as plain text. Answers "
        f"longer than {ANSWER_LIMIT} characters are cut and say so; the JSON export holds them "
        "whole. Passed attempts are left out."
    )
    if not failed:
        doc.para("No attempt failed.", size=9, gray=False)
        return
    for a in failed:
        doc.need(34)
        doc.ln(2)
        title = titles.get(a["scenario_id"], a["scenario_id"])
        flags = [
            f"level {effort_label(a.get('effort'))}" if a.get("effort") else None,
            f"attempt #{a['attempt']}",
            a["status"] if a["status"] != "ok" else None,
            {"empty": "no answer", "limit": "cut off"}.get(a.get("answer_issue") or ""),
        ]
        # the heading strip: the scenario, and its score in the score's colour
        top = doc.get_y()
        doc.set_fill_color(*SUB_FILL)
        doc.rect(doc.l_margin, top, doc.inner, 5.2, style="F")
        doc.set_fill_color(*CRIT)
        doc.rect(doc.l_margin, top, 1.2, 5.2, style="F")
        doc.ink(True, 8.5)
        doc.set_xy(doc.l_margin + 3, top)
        doc.cell(doc.inner - 22, 5.2, doc.t(title))
        doc.ink(True, 8.5, score_colour(a.get("score")))
        doc.cell(0, 5.2, doc.t(f"score {score(a.get('score'))}"), align="R")
        doc.set_y(top + 5.2)
        doc.ink(False, 7, MUTED, mono=True)
        doc.set_x(doc.l_margin + 3)
        doc.multi_cell(
            doc.inner - 3,
            3.8,
            doc.t(" · ".join(m for m in (a["scenario_id"], *flags) if m), mono=True),
            align="L",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        doc.ink()
        if a.get("error"):
            doc.ink(True, 8, CRIT)
            doc.set_x(doc.l_margin + 3)
            doc.multi_cell(
                doc.inner - 3,
                4.2,
                doc.t(f"Error: {a['error']}"),
                align="L",
                new_x="LMARGIN",
                new_y="NEXT",
            )
            doc.ink()
        checks = [c for c in (a.get("checks") or []) if not c.get("passed")]
        if not a.get("checks"):
            doc.ink(False, 8, MUTED)
            doc.set_x(doc.l_margin + 3)
            doc.multi_cell(
                doc.inner - 3,
                4.2,
                "No checks were recorded.",
                align="L",
                new_x="LMARGIN",
                new_y="NEXT",
            )
            doc.ink()
        for c in checks:
            line = f"{c.get('describe')}"
            if c.get("weight") not in (None, 1):
                line += f" (weight {c['weight']})"
            doc.ink(True, 8, CRIT)
            doc.set_x(doc.l_margin + 3)
            doc.cell(4, 4.2, "✗")
            doc.ink(True, 8)
            doc.multi_cell(
                doc.inner - 7, 4.2, doc.t(line), align="L", new_x="LMARGIN", new_y="NEXT"
            )
            if c.get("reason"):
                doc.ink(False, 7.5, MUTED)
                doc.set_x(doc.l_margin + 7)
                doc.multi_cell(
                    doc.inner - 7, 3.9, doc.t(c["reason"]), align="L", new_x="LMARGIN", new_y="NEXT"
                )
        answer = a.get("answer")
        doc.ink(True, 8, MUTED)
        doc.set_x(doc.l_margin + 3)
        if not answer:
            doc.multi_cell(
                doc.inner - 3,
                4.2,
                "Answer: none was produced.",
                align="L",
                new_x="LMARGIN",
                new_y="NEXT",
            )
            continue
        text = str(answer).strip()
        cut = len(text) - ANSWER_LIMIT
        if cut > 0:
            text = text[:ANSWER_LIMIT]
        doc.multi_cell(doc.inner - 3, 4.2, "Answer:", align="L", new_x="LMARGIN", new_y="NEXT")
        doc.ink(False, 7, INK, mono=True)
        doc.set_x(doc.l_margin + 5)
        doc.set_draw_color(*RULE)
        doc.set_line_width(0.5)
        doc.multi_cell(
            doc.inner - 5,
            3.5,
            doc.t(text, mono=True),
            border="L",
            align="L",
            new_x="LMARGIN",
            new_y="NEXT",
        )
        if cut > 0:
            doc.ink(False, 7.5, MUTED)
            doc.set_x(doc.l_margin + 5)
            doc.multi_cell(
                doc.inner - 5,
                3.9,
                f"… trimmed: {cut} more characters",
                align="L",
                new_x="LMARGIN",
                new_y="NEXT",
            )
        doc.ink()


def render_run(
    run: dict[str, Any],
    *,
    titles: dict[str, str],
    samples: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    failed: list[dict[str, Any]],
) -> bytes:
    """The PDF of one run. `attempts` are the light rows (for the chart's attempt marks), `failed`
    the full rows of the attempts that did not pass."""
    order = {e: i for i, e in enumerate(EFFORTS)}
    failed = sorted(
        failed, key=lambda a: (a["scenario_id"], order.get(a.get("effort") or "", -1), a["attempt"])
    )
    doc = Doc(
        landscape=False,
        footer_left=f"AI Server Monitor v{__version__} · benchmark run #{run['id']}",
    )
    doc.add_page()
    doc.h1(f"Benchmark run #{run['id']}")
    doc.set_font("mono", "B", 11)
    doc.multi_cell(
        0, 5.5, doc.t(run.get("model") or DASH, mono=True), new_x="LMARGIN", new_y="NEXT"
    )
    doc.para(
        f"{stamp(run.get('started'))} · status {run.get('status')} · report made by "
        "AI Server Monitor "
        f"v{__version__} on {_today()}",
        size=8.5,
    )
    if run.get("error"):
        doc.para(f"Run error: {run['error']}", size=8.5, gray=False, bold=True)

    doc.h2("What was measured")
    doc.facts([(k, v) for k, v in _facts(run)])
    summary = run.get("summary") or {}
    if (summary.get("reload_suspected") or 0) > 0:
        doc.para(
            f'{summary["reload_suspected"]} attempt(s) are marked "reload suspected": the backend '
            "may have unloaded the model meanwhile. They are included in the numbers.",
            gray=False,
        )
    if _meta(run).get("automation", {}) and _meta(run)["automation"].get("paused"):
        doc.para(
            "The monitor's scheduled jobs and automations were paused during this run, so no "
            "report "
            "or collector loaded the backend."
        )
    if (summary.get("overlapped") or 0) > 0:
        doc.para(
            f"{summary['overlapped']} attempt(s) overlapped a report being written by the monitor; "
            "their timings can be slower than they would be on an idle backend."
        )

    doc.h2("Results")
    _results(doc, run)
    _levels(doc, run)
    _hardware(doc, run, samples, attempts)
    _level_matrix(doc, run, titles)
    _scenario_table(doc, run, titles)
    _appendix(doc, run, failed, titles)
    return bytes(doc.output())


# --------------------------------------------------------------------------- comparison


def _best(
    cells: Sequence[float | None], high: bool, shown: Sequence[str] | None = None
) -> set[int]:
    """Index (or indices) of the best value, judged on what the cells show: values that print the
    same tie and are all marked; when every cell prints the same, nothing is marked. `shown` is
    each cell's text (its number is the part before " · "); without it the raw values are
    compared."""
    real = [(c, i) for i, c in enumerate(cells) if c is not None]
    if len(real) < 2:
        return set()
    _, at = (max if high else min)(real, key=lambda p: p[0])

    def key(i: int) -> Any:
        return shown[i].split(" · ")[0] if shown is not None else cells[i]

    winners = {i for c, i in real if key(i) == key(at)}
    return set() if len(winners) == len(real) else winners


# A comparison cell: the text it shows and the number behind it (None: not comparable).
Cell = tuple[str, float | None]

_QUIET = {"not recorded", DASH, "no GPU"}


class _Rows:
    """The rows of the comparison table, built the way the Compare page builds them."""

    def __init__(self, doc: Doc, runs: list[dict[str, Any]]):
        self.doc = doc
        self.runs = runs
        self.rows: list[Any] = []
        self._last = ""

    def _add(self, kind: str, row: Any) -> None:
        self.rows.append(row)
        self._last = kind

    def band(self, label: str) -> None:
        if self._last:
            self.rows.append(Gap(7.0))
        self._add("band", Band(label))

    def sub(self, label: str, aside: str = "") -> None:
        if self._last == "row":
            self.rows.append(Gap(3.5))
        self._add("sub", Sub(label, aside))

    def plain(self, text: str) -> Cell:
        return text, None

    def row(
        self,
        label: str,
        cells: Sequence[Cell],
        high: bool | None,
        sub: str = "",
        *,
        scored: bool = False,
        strong: bool = False,
    ) -> None:
        """One row. `high`: True when the biggest value is the best, False the smallest, None when
        the row is only shown. `scored` colours the numbers as scores."""
        winners = (
            _best([c[1] for c in cells], high, [c[0] for c in cells]) if high is not None else set()
        )
        head = cell(S(label, bold=strong), *([S(sub, color=FAINT, size=8)] if sub else []))
        out: list[TCell | str] = [head]
        for i, (text, raw) in enumerate(cells):
            best = i in winners
            colour = score_colour(raw) if scored and raw is not None else None
            if colour is None and text in _QUIET:
                colour = FAINT
            out.append(
                cell(
                    S(text, bold=best, color=colour),
                    fill=BEST_FILL if best else None,
                )
            )
        self._add("row", out)


def _scenario_cells(
    stats: Sequence[dict[str, Any] | None],
    runs: list[dict[str, Any]],
    sid: str,
    notes: dict[int, dict[str, Any]],
) -> list[TCell | str]:
    """The cells of one scenario row: the score in its colour and the median time on one line,
    the explanation under them in small grey text, all in the same cell. The best score, and the
    best time among the runs that tie on the best score, sit on a light green fill."""
    scores = [s["score"] if s else None for s in stats]
    times = [s.get("median_total_ms") if s else None for s in stats]
    best_score = _best(scores, True, [score(v) for v in scores])
    real = [s for s in scores if s is not None]
    top = max(real) if real else None
    # among the runs that tie on the best score as shown, the fastest one wins the time
    eligible = [
        t if scores[i] is not None and top is not None and score(scores[i]) == score(top) else None
        for i, t in enumerate(times)
    ]
    best_time = _best(eligible, False, [ms(v) for v in eligible])
    cells: list[TCell | str] = []
    for i, s in enumerate(stats):
        if not s:
            cells.append(cell(S("not run", color=FAINT)))
            continue
        line = [
            S(
                score(s["score"]),
                bold=i in best_score,
                color=score_colour(s["score"]),
                hl=BEST_FILL if i in best_score else None,
            ),
            S(" · ", color=MUTED),
            S(
                ms(s.get("median_total_ms")),
                bold=i in best_time,
                color=INK if i in best_time else MUTED,
                hl=BEST_FILL if i in best_time else None,
            ),
        ]
        why = (notes.get(runs[i]["id"]) or {}).get(sid) or {}
        grey = FAINT if why.get("ok") else MUTED
        cells.append(cell(line, *(S(text, color=grey, size=8) for text in why.get("lines") or [])))
    return cells


def render_compare(
    runs: list[dict[str, Any]],
    *,
    titles: dict[str, str],
    explanations: dict[int, dict[str, Any]] | None = None,
) -> bytes:
    """The PDF of the Compare page: the same rows, the best value of each on a green fill."""
    ids = ", ".join(f"#{r['id']}" for r in runs)
    doc = Doc(
        landscape=len(runs) > 3,
        footer_left=f"AI Server Monitor v{__version__} · benchmark comparison {ids}",
    )
    doc.add_page()
    doc.h1(f"Comparing {len(runs)} benchmark runs")
    doc.para(
        f"Runs {ids} · made by AI Server Monitor v{__version__} on {_today()}",
        size=8.5,
    )
    if len({r.get("scenario_hash") for r in runs}) != 1:
        doc.para(
            "These runs used different scenario sets or scopes, so scores are only comparable on "
            "the "
            "scenarios they share.",
            size=8.5,
            gray=False,
            bold=True,
        )
    doc.ln(1.5)

    b = _Rows(doc, runs)
    plain, row, band = b.plain, b.row, b.band

    def pct_cell(v: float | None) -> Cell:
        return score(v), v

    def ms_cell(v: float | None) -> Cell:
        return ms(v), v

    def summary(r: dict[str, Any]) -> dict[str, Any]:
        return r.get("summary") or {}

    band("Setup")
    for label, key in (("Temperature", "temperature"), ("Top P", "top_p"), ("Top K", "top_k")):
        row(label, [plain(sampling_text(_meta(r).get("llm") or {}, key)) for r in runs], None)
    row("Tool mode used", [plain(str(_meta(r).get("tool_mode") or DASH)) for r in runs], None)
    row("Tool routing", [plain(routing_label(r)) for r in runs], None)
    row("Attempts each", [plain(str(r.get("repeats"))) for r in runs], None)
    row("Automation", [plain(automation_text(_meta(r).get("automation"))) for r in runs], None)
    row("Scenario set", [plain(str(r.get("scenario_hash") or DASH)) for r in runs], None)
    band("Accuracy")
    row("Overall", [pct_cell(r.get("overall")) for r in runs], True, scored=True, strong=True)
    groups = list(dict.fromkeys(g for r in runs for g in (r.get("group_scores") or {})))
    for g in groups:
        row(
            GROUP_SHORT.get(g, g),
            [pct_cell((r.get("group_scores") or {}).get(g)) for r in runs],
            True,
            scored=True,
        )
    row("Pass rate", [pct_cell(summary(r).get("pass_rate")) for r in runs], True)
    row(
        "Failed attempts",
        [
            (str(summary(r)["failures"]), summary(r)["failures"])
            if "failures" in summary(r)
            else plain(DASH)
            for r in runs
        ],
        False,
    )
    band("Speed")
    row("Median first token", [ms_cell(summary(r).get("median_ttft_ms")) for r in runs], False)
    row("Median attempt time", [ms_cell(summary(r).get("median_total_ms")) for r in runs], False)
    row(
        "Cold start (not in the numbers)",
        [plain(cold_start_text(_meta(r).get("cold_start"))) for r in runs],
        False,
    )
    row(
        "Generation speed",
        [
            (tps(summary(r).get("median_tokens_per_s")), summary(r).get("median_tokens_per_s"))
            for r in runs
        ],
        True,
    )
    _compare_hardware(runs, b)
    _compare_levels(runs, b)
    band("Per scenario: score · median time")
    ids_all = sorted({sid for r in runs for sid in (summary(r).get("scenarios") or {})})
    notes = explanations or {}
    for sid in ids_all:
        stats = [(summary(r).get("scenarios") or {}).get(sid) for r in runs]
        label = _scenario_label(titles.get(sid, sid), sid)
        b._add("row", [label, *_scenario_cells(stats, runs, sid, notes)])

    first = 58.0 if len(runs) <= 3 else 66.0
    each = (doc.inner - first) / len(runs)
    header: list[TCell | str] = [" "]
    for r in runs:
        header.append(
            cell(
                S(run_label(r), bold=True, size=8.4),
                S(f"#{r['id']} · {stamp(r.get('started'))}", color=MUTED, size=7.5),
                S(backend_label(r), color=MUTED, size=8),
                S(size_label(r), color=MUTED, size=8),
            )
        )
    doc.grid(header, b.rows, [first, *[each] * len(runs)], "L" * (len(runs) + 1), size=8)
    doc.para(
        "The best value in each row is bold on a green fill. Scores: green is 90% and up, amber "
        "50% to 89%, red under 50%. Time, failures and hardware load: lower is better; scores, "
        "speed and the share of the model on the GPU: higher is better. Hardware numbers of a "
        'model served from another machine are the host\'s, labelled "(host)" and never marked.'
    )
    return bytes(doc.output())


def _compare_hardware(runs: list[dict[str, Any]], b: _Rows) -> None:
    plain, row = b.plain, b.row

    def cell_of(r: dict[str, Any], pick: Any, *, gpu: bool = False) -> Cell:
        meta = _meta(r).get("hardware") or {}
        if gpu and meta.get("recorded") and not meta.get("gpu"):
            return plain("no GPU")
        if hw_scope(r) == "old":
            return plain("not recorded")
        got = pick(r) if (r.get("summary") or {}).get("hardware") else None
        if not got:
            return plain(DASH)
        return (f"{got[0]} (host)", None) if hw_scope(r) == "elsewhere" else got

    def vram(r: dict[str, Any]) -> Cell | None:
        hw = r["summary"]["hardware"]
        s = hw.get("llm_vram") or hw.get("vram_used")
        return (gb_of(s["peak"], hw.get("vram_total")), s["peak"]) if s else None

    def stat(key: str, part: str, fmt: Any) -> Any:
        def pick(r: dict[str, Any]) -> Cell | None:
            hw = r["summary"]["hardware"]
            s = hw.get(key)
            if not s:
                return None
            return (fmt(s[part], hw), s[part])

        return pick

    def loaded(r: dict[str, Any]) -> Cell:
        if hw_scope(r) == "old":
            return plain("not recorded")
        g = (_meta(r).get("loaded_model") or {}).get("gpu_percent")
        return plain(DASH) if g is None else (f"{g}%", g)

    own = any(((r.get("summary") or {}).get("hardware") or {}).get("llm_vram") for r in runs)
    b.band("Hardware")
    row(
        "Measured from",
        [plain(hardware_label(_meta(r).get("hardware"), r.get("backend_kind"))) for r in runs],
        None,
        "the process that holds this model in VRAM",
    )
    row(
        "VRAM, peak",
        [cell_of(r, vram, gpu=True) for r in runs],
        False,
        "model's own process when seen" if own else "whole GPU",
    )
    row(
        "GPU busy, average",
        [cell_of(r, stat("gpu_util", "avg", lambda v, h: pct(v)), gpu=True) for r in runs],
        None,
    )
    row(
        "GPU power, average",
        [cell_of(r, stat("power_w", "avg", lambda v, h: watts(v)), gpu=True) for r in runs],
        False,
    )
    row("CPU, average", [cell_of(r, stat("cpu", "avg", lambda v, h: pct(v))) for r in runs], False)
    row(
        "RAM, peak",
        [
            cell_of(r, stat("ram_used", "peak", lambda v, h: gb_of(v, h.get("ram_total"))))
            for r in runs
        ],
        False,
    )
    row("Model on GPU", [loaded(r) for r in runs], True, "rest runs on the CPU")


def _compare_levels(runs: list[dict[str, Any]], b: _Rows) -> None:
    levels = [e for e in EFFORTS if any(e in levels_of(r) for r in runs)]
    if not levels:
        return
    plain, row = b.plain, b.row
    b.band("Per thinking level")
    none: Cell = plain(DASH)

    def cells(level: str, make: Callable[[dict[str, Any], dict[str, Any]], Cell]) -> list[Cell]:
        """One cell per run: made from the run's numbers at `level`, a dash where it has none."""
        out: list[Cell] = []
        for r in runs:
            stat = ((r.get("summary") or {}).get("levels") or {}).get(level)
            out.append(make(r, stat) if stat else none)
        return out

    def vram(r: dict[str, Any], stat: dict[str, Any]) -> Cell:
        v = vram_stat(stat.get("hw"))
        if not v:
            return none
        return plain(f"{gb(v['peak'])} (host)" if hw_scope(r) == "elsewhere" else gb(v["peak"]))

    def median(key: str) -> Callable[[dict[str, Any], dict[str, Any]], Cell]:
        return lambda r, st: plain(str(DASH if st.get(key) is None else st[key]))

    for e in levels:
        b.sub(effort_label(e), EFFORT_HELP[e])
        row(
            "Accuracy",
            cells(e, lambda r, st: (score(st["overall"]), st["overall"])),
            True,
            scored=True,
            strong=True,
        )
        row(
            "Time per answer",
            cells(e, lambda r, st: (ms(st.get("median_total_ms")), st.get("median_total_ms"))),
            False,
        )
        row("Tokens out", cells(e, median("median_tokens_out")), None)
        row(
            "Reasoning",
            cells(
                e,
                lambda r, st: plain(
                    f"{chars(st['median_reasoning_chars'])} chars"
                    if st.get("median_reasoning_chars")
                    else DASH
                ),
            ),
            None,
        )
        row(
            "Empty / cut off",
            cells(
                e,
                lambda r, st: (
                    f"{st.get('empty')} / {st.get('limit')}",
                    (st.get("empty") or 0) + (st.get("limit") or 0),
                ),
            ),
            False,
        )
        row("VRAM peak", cells(e, vram), None)
