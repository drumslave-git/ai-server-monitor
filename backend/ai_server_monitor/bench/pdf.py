"""PDF export of benchmark results: one run (what the run page shows, plus an appendix of the failed
attempts) or several runs side by side (what the Compare page shows).

Built with fpdf2 (pure Python, so the image needs no system libraries). The text is real text, the
load chart is drawn with vector lines, and the layout reads in black and white: the best value in a
row is bold, chart lines differ by dash pattern, headers repeat on every page a table spans.
`render_run` and `render_compare` are plain functions of the data; the API loads the data and runs
them in a worker thread."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.fonts import FontFace

from .. import __version__
from .hardware import columnar, thin

FONT_DIR = Path(__file__).parent / "fonts"

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

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


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


def gpu_text(gpu: dict[str, Any] | None) -> str:
    gpus = (gpu or {}).get("gpus") or []
    if not gpus:
        return "not available"
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


def hardware_label(meta: dict[str, Any] | None) -> str:
    if not meta or not meta.get("recorded"):
        return "not recorded"
    if not meta.get("gpu"):
        return "no GPU on this host"
    if meta.get("llm_here") == "elsewhere":
        return "this host; the LLM server is elsewhere"
    if meta.get("llm_here") == "here":
        return f"LLM seen on this host ({meta.get('llm_process')})"
    return "this host; LLM process not recognised"


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


# --------------------------------------------------------------------------- the document

PALETTE = {
    "gpu": ((20, 20, 20), None, "GPU busy"),
    "vram": ((30, 90, 170), (6.0, 2.5), "VRAM"),
    "cpu": ((110, 110, 110), (0.5, 1.3), "CPU"),
    "ram": ((20, 130, 70), (2.5, 1.8), "RAM"),
}

_MD = re.compile(r"(\*\*|__|--)")


class Doc(FPDF):
    """A4 document with Unicode fonts, a footer with page numbers and the few building blocks the
    two reports share."""

    def __init__(self, *, landscape: bool, footer_left: str):
        super().__init__(orientation="L" if landscape else "P", unit="mm", format="A4")
        self.footer_left = footer_left
        self.set_margins(14, 14, 14)
        self.set_auto_page_break(True, margin=16)
        self.alias_nb_pages()
        self.add_font("body", "", str(FONT_DIR / "DejaVuSans.ttf"))
        self.add_font("body", "B", str(FONT_DIR / "DejaVuSans-Bold.ttf"))
        self.add_font("mono", "", str(FONT_DIR / "DejaVuSansMono.ttf"))
        self.add_font("mono", "B", str(FONT_DIR / "DejaVuSansMono-Bold.ttf"))
        self._cmaps = {
            "body": set(self.fonts["body"].cmap),
            "mono": set(self.fonts["mono"].cmap),
        }
        self.set_font("body", "", 9)
        self.set_text_color(0)
        self.set_title(footer_left)
        self.set_author("AI Server Monitor")
        self.set_creator(f"AI Server Monitor {__version__}")

    # ---- text

    def t(self, value: Any, *, mono: bool = False) -> str:
        """Text the fonts can print: control characters go, glyphs the font lacks become "?"."""
        text = _CTRL.sub("", str(value)).replace("\r", "").replace("\t", "    ")
        have = self._cmaps["mono" if mono else "body"]
        if all(ord(c) in have or c == "\n" for c in text):
            return text
        return "".join(c if ord(c) in have or c == "\n" else "?" for c in text)

    def md(self, value: Any, *, bold: bool = False, mono: bool = False) -> str:
        """Text for a table that reads **bold**: a literal ** __ -- in the data is defused."""
        text = _MD.sub(lambda m: m.group(1)[0] + "​" + m.group(1)[1], self.t(value, mono=mono))
        if "​" in text and "​" not in self._cmaps["body"]:
            text = text.replace("​", " ")
        return f"**{text}**" if bold and text else text

    # ---- chrome

    def footer(self) -> None:
        self.set_y(-11)
        self.set_font("body", "", 7)
        self.set_text_color(90)
        half = (self.w - self.l_margin - self.r_margin) / 2
        self.cell(half, 4, self.t(self.footer_left))
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")
        self.set_text_color(0)

    def need(self, height: float) -> None:
        if self.will_page_break(height):
            self.add_page()

    @property
    def inner(self) -> float:
        return self.w - self.l_margin - self.r_margin

    def h1(self, text: str) -> None:
        self.set_font("body", "B", 17)
        self.multi_cell(0, 8, self.t(text), new_x="LMARGIN", new_y="NEXT")

    def h2(self, text: str, aside: str = "") -> None:
        self.ln(3)
        self.need(30)
        self.set_font("body", "B", 11.5)
        self.cell(self.get_string_width(self.t(text)) + 1, 6, self.t(text))
        if aside:
            self.set_font("body", "", 8)
            self.set_text_color(90)
            self.cell(0, 6, "  " + self.t(aside))
            self.set_text_color(0)
        self.ln(6)
        self.set_draw_color(120)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.l_margin + self.inner, self.get_y())
        self.ln(1.6)

    def para(self, text: str, *, size: float = 8, gray: bool = True, bold: bool = False) -> None:
        self.set_font("body", "B" if bold else "", size)
        self.set_text_color(70 if gray else 0)
        self.multi_cell(0, size * 0.5 + 0.9, self.t(text), new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0)
        self.ln(0.8)

    # ---- tables

    def grid(
        self,
        header: Sequence[str] | None,
        rows: Sequence[Sequence[str] | tuple[str, ...]],
        widths: Sequence[float],
        aligns: str,
        *,
        size: float = 8,
        section: set[int] | None = None,
        raw_header: bool = False,
    ) -> None:
        """A table: `widths` in mm, `aligns` one of L/R/C per column; text may hold **bold**; rows
        in `section` are one grey band over all columns. The header repeats on every page; its cells
        are made bold unless `raw_header` (the caller marked them up)."""
        section = section or set()
        self.set_font("body", "", size)
        self.set_text_color(0)
        self.set_draw_color(150)
        align_map = {"L": "LEFT", "R": "RIGHT", "C": "CENTER"}
        with self.table(
            col_widths=tuple(widths),
            text_align=tuple(align_map[a] for a in aligns),
            line_height=size * 0.5 + 0.9,
            borders_layout="HORIZONTAL_LINES",
            first_row_as_headings=header is not None,
            headings_style=FontFace(emphasis=None, fill_color=(228, 228, 228)),
            markdown=True,
            align="LEFT",
            v_align="TOP",
            padding=(0.9, 1.2),
            width=sum(widths),
        ) as table:
            if header is not None:
                head = table.row()
                for h in header:
                    head.cell(h if raw_header else self.md(h, bold=True))
            for i, cells in enumerate(rows):
                row = table.row()
                if i in section:
                    row.cell(
                        self.md(cells[0], bold=True),
                        colspan=len(widths),
                        style=FontFace(fill_color=(240, 240, 240)),
                    )
                    continue
                for cell in cells:
                    row.cell(cell)
        self.ln(1.5)

    def facts(self, items: Sequence[tuple[str, str]], label_w: float = 34) -> None:
        """Label / value pairs (the page's definition lists)."""
        self.set_font("body", "", 8.5)
        self.set_draw_color(190)
        with self.table(
            col_widths=(label_w, self.inner - label_w),
            text_align=("LEFT", "LEFT"),
            line_height=4.4,
            borders_layout="HORIZONTAL_LINES",
            first_row_as_headings=False,
            markdown=True,
            align="LEFT",
            v_align="TOP",
            padding=(0.8, 1.2),
        ) as table:
            for label, value in items:
                row = table.row()
                row.cell(self.md(label, bold=True))
                row.cell(self.md(value))
        self.ln(1.5)

    # ---- chart

    def load_chart(self, points: dict[str, list[Any]], attempt_starts: Sequence[float]) -> None:
        """CPU, RAM, GPU and VRAM in percent over the run. Vector lines that differ by dash
        pattern as well as colour."""
        ts = points["ts"]
        series: dict[str, list[float | None]] = {}

        def ratio(used: list[Any], total: list[Any]) -> list[float | None]:
            return [
                None if u is None or not t else min(100.0, u / t * 100)
                for u, t in zip(used, total, strict=True)
            ]

        series["gpu"] = points["gpu_util"]
        series["vram"] = ratio(points["vram_used"], points["vram_total"])
        series["cpu"] = points["cpu"]
        series["ram"] = ratio(points["ram_used"], points["ram_total"])
        keys = [k for k in PALETTE if any(v is not None for v in series[k])]
        if not keys or len(ts) < 2:
            return
        height, label_h = 52.0, 10.0
        self.need(height + label_h + 12)
        self.set_font("body", "B", 9)
        self.cell(0, 5, "Over the run (percent)", new_x="LMARGIN", new_y="NEXT")
        left = self.l_margin + 9
        width = self.inner - 9 - 2
        top = self.get_y() + 2
        t0 = ts[0]
        span = max(ts[-1] - t0, 1)

        def x(t: float) -> float:
            return left + (t - t0) / span * width

        def y(v: float) -> float:
            return top + (1 - v / 100) * height

        self.set_font("body", "", 7)
        self.set_line_width(0.15)
        for v in (0, 25, 50, 75, 100):
            self.set_draw_color(205 if v not in (0, 100) else 120)
            self.line(left, y(v), left + width, y(v))
            self.set_xy(self.l_margin, y(v) - 1.6)
            self.cell(7.5, 3.2, f"{v}%", align="R")
        self.set_draw_color(120)
        self.line(left, top, left, top + height)
        ticks = 6
        for i in range(ticks):
            tick = t0 + span * i / (ticks - 1)
            self.line(x(tick), top + height, x(tick), top + height + 1.2)
            label = clock(tick - t0)
            w = self.get_string_width(label)
            lx = min(max(x(tick) - w / 2, left - 3), left + width - w)
            self.set_xy(lx, top + height + 1.6)
            self.cell(w + 0.5, 3.2, label)
        self.set_draw_color(90)
        for start in attempt_starts:
            if t0 - 1 <= start <= t0 + span + 1:
                self.line(x(start), top + height - 1.6, x(start), top + height)
        for key in keys:
            colour, dash, _ = PALETTE[key]
            self.set_draw_color(*colour)
            self.set_line_width(0.45)
            if dash:
                self.set_dash_pattern(dash=dash[0], gap=dash[1])
            run: list[tuple[float, float]] = []
            for t, v in zip(ts, series[key], strict=True):
                if v is None:
                    if len(run) > 1:
                        self.polyline(run, style="D")
                    run = []
                else:
                    run.append((x(t), y(v)))
            if len(run) > 1:
                self.polyline(run, style="D")
            self.set_dash_pattern()
        # legend
        self.set_xy(left, top + height + 6.2)
        for key in keys:
            colour, dash, name = PALETTE[key]
            gx, gy = self.get_x(), self.get_y() + 1.7
            self.set_draw_color(*colour)
            self.set_line_width(0.45)
            if dash:
                self.set_dash_pattern(dash=dash[0], gap=dash[1])
            self.line(gx, gy, gx + 14, gy)
            self.set_dash_pattern()
            self.set_x(gx + 15.5)
            self.set_font("body", "", 7.5)
            self.cell(self.get_string_width(name) + 6, 3.6, name)
        self.set_draw_color(90)
        self.set_line_width(0.15)
        gx, gy = self.get_x(), self.get_y() + 1.7
        self.line(gx, gy - 0.6, gx, gy + 1.2)
        self.set_x(gx + 1.5)
        self.cell(40, 3.6, "an attempt starts")
        self.set_draw_color(150)
        self.set_line_width(0.2)
        self.set_y(top + height + 12)


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
            f"temperature {llm.get('temperature', DASH)} · max output "
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
        ("GPU", gpu_text(meta.get("gpu"))),
        ("Automation", automation_text(meta.get("automation"))),
    ]
    if meta.get("judge"):
        items.append(("Judge", str(meta["judge"].get("model"))))
    return items


def _results(doc: Doc, run: dict[str, Any]) -> None:
    summary = run.get("summary") or {}
    tiles = [
        ("Overall", score(run.get("overall"))),
        ("Pass rate", score(summary.get("pass_rate"))),
        ("Median first token", ms(summary.get("median_ttft_ms"))),
        ("Median attempt", ms(summary.get("median_total_ms"))),
        ("Speed", tps(summary.get("median_tokens_per_s"))),
        ("Failed attempts", str(summary["failures"]) if "failures" in summary else DASH),
    ]
    if summary.get("empty_answers"):
        tiles.append(("Empty answers", str(summary["empty_answers"])))
    cols = 4
    width = doc.inner / cols
    rows = []
    for i in range(0, len(tiles), cols):
        chunk = tiles[i : i + cols]
        chunk += [("", "")] * (cols - len(chunk))
        rows.append([f"{label}\n**{value}**" if label else "" for label, value in chunk])
    doc.grid(None, rows, [width] * cols, "L" * cols, size=10)
    groups = run.get("group_scores") or {}
    if groups:
        doc.grid(
            ["Group", "Score"],
            [[GROUP_SHORT.get(g, g), score(s)] for g, s in groups.items()],
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
                f"**{doc.t(effort_label(e))}**\n{EFFORT_HELP[e]} · {s['attempts']} attempts",
                doc.md(score(s.get("overall")), bold=top),
                score(s.get("pass_rate")),
                ms(s.get("median_total_ms")),
                str(s.get("median_tokens_out") if s.get("median_tokens_out") is not None else DASH),
                f"{chars(s['median_reasoning_chars'])} chars"
                if s.get("median_reasoning_chars")
                else DASH,
                issues or "0",
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
        "reports no split, so reasoning is counted in characters. Bold: the best accuracy."
    )
    if note:
        text += f" The level changes {note.get('applies_to')}. {note.get('fixed')}"
    doc.para(text, size=7.5)


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
                        doc.md(f"held by {meta.get('llm_process') or 'the LLM server'}"),
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
                doc.md(f"from the backend ({loaded.get('source')})"),
            ]
            rows.append(
                [
                    "Model on GPU",
                    DASH if gp is None else f"{gp}%",
                    "",
                    " · ".join(s for s in sub if s),
                ]
            )
        doc.grid(["", "Peak", "Average", "Note"], rows, [34, 30, 30, doc.inner - 94], "LRRL")
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
        row = [doc.md(titles.get(sid, sid))]
        for e in levels:
            s = (stats[sid]["levels"] or {}).get(e)
            if not s:
                row.append(DASH)
                continue
            text = f"{score(s['score'])} · {ms(s.get('median_total_ms'))}"
            if s.get("empty"):
                text += f"\n{s['empty']} empty"
            row.append(text)
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
    widths = [56.0, 14, 12, 18, 15, 15]
    aligns = "LLRRRR"
    if show_hw:
        header += [f"{host}VRAM peak", f"{host}GPU avg", f"{host}CPU avg"]
        widths += [15.0, 13, 13]
        aligns += "RRR"
    widths[0] += doc.inner - sum(widths)
    rows = []
    for sid, s in stats.items():
        passed = f"{s['passed']}/{s['attempts']}"
        if s.get("failed"):
            passed += f"\n{s['failed']} failed"
        row = [
            f"{doc.md(titles.get(sid, sid))}\n{doc.md(sid, mono=True)}",
            GROUP_SHORT.get(s.get("group"), s.get("group") or DASH),
            score(s.get("score")),
            passed,
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
    doc.grid(header, rows, widths, aligns, size=7.5)


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
        doc.ln(1.5)
        title = titles.get(a["scenario_id"], a["scenario_id"])
        head = f"{title}"
        meta = [
            a["scenario_id"],
            f"level {effort_label(a.get('effort'))}" if a.get("effort") else None,
            f"attempt #{a['attempt']}",
            f"score {score(a.get('score'))}",
            a["status"] if a["status"] != "ok" else None,
            {"empty": "no answer", "limit": "cut off"}.get(a.get("answer_issue") or ""),
        ]
        doc.set_fill_color(235)
        doc.set_font("body", "B", 8.5)
        doc.set_text_color(0)
        doc.multi_cell(0, 4.8, doc.t(head), fill=True, new_x="LMARGIN", new_y="NEXT")
        doc.set_font("mono", "", 7)
        doc.set_text_color(70)
        doc.multi_cell(
            0,
            3.8,
            doc.t(" · ".join(m for m in meta if m), mono=True),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        doc.set_text_color(0)
        if a.get("error"):
            doc.set_font("body", "B", 8)
            doc.multi_cell(0, 4.2, doc.t(f"Error: {a['error']}"), new_x="LMARGIN", new_y="NEXT")
        checks = [c for c in (a.get("checks") or []) if not c.get("passed")]
        if not a.get("checks"):
            doc.set_font("body", "", 8)
            doc.multi_cell(0, 4.2, "No checks were recorded.", new_x="LMARGIN", new_y="NEXT")
        for c in checks:
            doc.set_x(doc.l_margin + 2)
            doc.set_font("body", "B", 8)
            line = f"✗ {c.get('describe')}"
            if c.get("weight") not in (None, 1):
                line += f" (weight {c['weight']})"
            doc.multi_cell(doc.inner - 2, 4.2, doc.t(line), new_x="LMARGIN", new_y="NEXT")
            if c.get("reason"):
                doc.set_x(doc.l_margin + 6)
                doc.set_font("body", "", 7.5)
                doc.set_text_color(60)
                doc.multi_cell(
                    doc.inner - 6, 3.9, doc.t(c["reason"]), new_x="LMARGIN", new_y="NEXT"
                )
                doc.set_text_color(0)
        answer = a.get("answer")
        doc.set_font("body", "B", 8)
        doc.set_x(doc.l_margin + 2)
        if not answer:
            doc.multi_cell(
                doc.inner - 2, 4.2, "Answer: none was produced.", new_x="LMARGIN", new_y="NEXT"
            )
            continue
        text = str(answer).strip()
        cut = len(text) - ANSWER_LIMIT
        if cut > 0:
            text = text[:ANSWER_LIMIT]
        doc.multi_cell(doc.inner - 2, 4.2, "Answer:", new_x="LMARGIN", new_y="NEXT")
        doc.set_font("mono", "", 7)
        doc.set_x(doc.l_margin + 4)
        doc.multi_cell(
            doc.inner - 4, 3.5, doc.t(text, mono=True), border="L", new_x="LMARGIN", new_y="NEXT"
        )
        if cut > 0:
            doc.set_font("body", "", 7.5)
            doc.set_x(doc.l_margin + 4)
            doc.multi_cell(
                doc.inner - 4,
                3.9,
                f"… trimmed: {cut} more characters",
                new_x="LMARGIN",
                new_y="NEXT",
            )


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


def _best(cells: Sequence[float | None], high: bool) -> set[int]:
    """Index (or indices) of the best value; ties all count; all equal marks nothing."""
    real = [(c, i) for i, c in enumerate(cells) if c is not None]
    if len(real) < 2:
        return set()
    target = (max if high else min)(c for c, _ in real)
    winners = {i for c, i in real if abs(c - target) < 1e-9}
    return set() if len(winners) == len(real) else winners


Cell = tuple[str, float | None]


def render_compare(runs: list[dict[str, Any]], *, titles: dict[str, str]) -> bytes:
    """The PDF of the Compare page: the same rows, the best value of each in bold."""
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

    rows: list[list[str]] = []
    section: set[int] = set()

    def band(label: str) -> None:
        section.add(len(rows))
        rows.append([label])

    def row(label: str, cells: Sequence[Cell], high: bool | None, sub: str = "") -> None:
        winners = _best([c[1] for c in cells], high) if high is not None else set()
        head = doc.md(label) + (f"\n{doc.md(sub)}" if sub else "")
        rows.append([head, *(doc.md(t, bold=i in winners) for i, (t, _) in enumerate(cells))])

    def num(v: float | None) -> float | None:
        return v

    def pct_cell(v: float | None) -> Cell:
        return score(v), num(v)

    def ms_cell(v: float | None) -> Cell:
        return ms(v), num(v)

    def plain(text: str) -> Cell:
        return text, None

    def summary(r: dict[str, Any]) -> dict[str, Any]:
        return r.get("summary") or {}

    band("Setup")
    row(
        "Temperature",
        [
            plain(str(((r.get("meta") or {}).get("llm") or {}).get("temperature", DASH)))
            for r in runs
        ],
        None,
    )
    row("Tool mode used", [plain(str(_meta(r).get("tool_mode") or DASH)) for r in runs], None)
    row("Tool routing", [plain(routing_label(r)) for r in runs], None)
    row("Attempts each", [plain(str(r.get("repeats"))) for r in runs], None)
    row("Automation", [plain(automation_text(_meta(r).get("automation"))) for r in runs], None)
    row("Scenario set", [plain(str(r.get("scenario_hash") or DASH)) for r in runs], None)
    band("Accuracy")
    row("Overall", [pct_cell(r.get("overall")) for r in runs], True)
    groups = list(dict.fromkeys(g for r in runs for g in (r.get("group_scores") or {})))
    for g in groups:
        row(
            GROUP_SHORT.get(g, g),
            [pct_cell((r.get("group_scores") or {}).get(g)) for r in runs],
            True,
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
    _compare_hardware(runs, band, row, plain)
    _compare_levels(runs, band, row, plain)
    band("Per scenario: score · median time")
    ids_all = sorted({sid for r in runs for sid in (summary(r).get("scenarios") or {})})
    for sid in ids_all:
        stats = [(summary(r).get("scenarios") or {}).get(sid) for r in runs]
        scores = [s["score"] if s else None for s in stats]
        times = [s.get("median_total_ms") if s else None for s in stats]
        best_score = _best(scores, True)
        real = [s for s in scores if s is not None]
        top = max(real) if real else None
        eligible = [
            t if scores[i] is not None and top is not None and abs(scores[i] - top) < 1e-9 else None
            for i, t in enumerate(times)
        ]
        best_time = _best(eligible, False)
        cells = []
        for i, s in enumerate(stats):
            if not s:
                cells.append("not run")
                continue
            when = doc.md(ms(s.get("median_total_ms")), bold=i in best_time)
            cells.append(f"{doc.md(score(s['score']), bold=i in best_score)} · {when}")
        rows.append([f"{doc.md(titles.get(sid, sid))}\n{doc.md(sid, mono=True)}", *cells])

    first = 58.0 if len(runs) <= 3 else 66.0
    each = (doc.inner - first) / len(runs)
    header = [" "]
    for r in runs:
        header.append(
            f"{doc.md(run_label(r), bold=True)}\n#{r['id']} · {stamp(r.get('started'))}\n"
            f"{doc.md(backend_label(r))}\n{doc.md(size_label(r))}"
        )
    doc.grid(
        header,
        rows,
        [first, *[each] * len(runs)],
        "L" + "C" * len(runs),
        size=7.5,
        section=section,
        raw_header=True,
    )
    doc.para(
        "The best value in each row is bold. Time, failures and hardware load: lower is better; "
        "scores, speed and the share of the model on the GPU: higher is better. Hardware numbers "
        "of a model "
        'served from another machine are the host\'s, labelled "(host)" and never marked.'
    )
    return bytes(doc.output())


def _compare_hardware(runs: list[dict[str, Any]], band: Any, row: Any, plain: Any) -> None:
    def cell(r: dict[str, Any], pick: Any, *, gpu: bool = False) -> Cell:
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
    band("Hardware")
    row("Measured on", [plain(hardware_label(_meta(r).get("hardware"))) for r in runs], None)
    row(
        "VRAM, peak",
        [cell(r, vram, gpu=True) for r in runs],
        False,
        "model's own process when seen" if own else "whole GPU",
    )
    row(
        "GPU busy, average",
        [cell(r, stat("gpu_util", "avg", lambda v, h: pct(v)), gpu=True) for r in runs],
        None,
    )
    row(
        "GPU power, average",
        [cell(r, stat("power_w", "avg", lambda v, h: watts(v)), gpu=True) for r in runs],
        False,
    )
    row("CPU, average", [cell(r, stat("cpu", "avg", lambda v, h: pct(v))) for r in runs], False)
    row(
        "RAM, peak",
        [
            cell(r, stat("ram_used", "peak", lambda v, h: gb_of(v, h.get("ram_total"))))
            for r in runs
        ],
        False,
    )
    row("Model on GPU", [loaded(r) for r in runs], True, "rest runs on the CPU")


def _compare_levels(runs: list[dict[str, Any]], band: Any, row: Any, plain: Any) -> None:
    levels = [e for e in EFFORTS if any(e in levels_of(r) for r in runs)]
    if not levels:
        return
    band("Per thinking level")
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
        name = effort_label(e)
        row(
            f"{name} · accuracy",
            cells(e, lambda r, st: (score(st["overall"]), st["overall"])),
            True,
        )
        row(
            f"{name} · time per answer",
            cells(e, lambda r, st: (ms(st.get("median_total_ms")), st.get("median_total_ms"))),
            False,
        )
        row(f"{name} · tokens out", cells(e, median("median_tokens_out")), None)
        row(
            f"{name} · reasoning",
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
            f"{name} · empty / cut off",
            cells(
                e,
                lambda r, st: (
                    f"{st.get('empty')} / {st.get('limit')}",
                    (st.get("empty") or 0) + (st.get("limit") or 0),
                ),
            ),
            False,
        )
        row(f"{name} · VRAM peak", cells(e, vram), None)
