"""The drawing half of the PDF export: the colours, a table engine that draws its own rows (so a
cell can mix a coloured score, a muted time and a small grey explanation, and a row is never split
across pages), headings, tiles and the load chart. `pdf.py` holds what the two reports say."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import MethodReturnValue

from .. import __version__

FONT_DIR = Path(__file__).parent / "fonts"

RGB = tuple[int, int, int]

# --------------------------------------------------------------------------- colours
# The web page's colours (frontend styles.css), made a touch darker where the text sits on a tint,
# so they print well on white.

INK: RGB = (24, 24, 27)
MUTED: RGB = (92, 92, 102)
FAINT: RGB = (116, 116, 126)
RULE: RGB = (208, 211, 218)
ZEBRA: RGB = (246, 247, 250)
HEAD_FILL: RGB = (236, 239, 245)
SUB_FILL: RGB = (238, 239, 243)
ACCENT: RGB = (33, 88, 199)
ACCENT_DARK: RGB = (22, 56, 135)
ACCENT_TINT: RGB = (228, 236, 252)
OK: RGB = (22, 101, 52)
WARN: RGB = (150, 84, 0)
CRIT: RGB = (176, 42, 32)
CRIT_TINT: RGB = (252, 234, 232)
BEST_FILL: RGB = (205, 232, 214)

LEVEL_COLOUR: dict[str, RGB] = {"ok": OK, "warn": WARN, "crit": CRIT, "off": MUTED}


def score_level(v: float | None) -> str:
    """The page's thresholds (frontend/src/pages/bench/format.ts `scoreLevel`): 90 % and up is ok,
    50 % and up is warn, under that crit."""
    if v is None:
        return "off"
    return "ok" if v >= 0.9 else "warn" if v >= 0.5 else "crit"


def score_colour(v: float | None) -> RGB:
    return LEVEL_COLOUR[score_level(v)]


# chart lines: distinct hues that stay apart in grey too, plus a dash pattern each
PALETTE: dict[str, tuple[RGB, tuple[float, float] | None, str]] = {
    "gpu": ((33, 88, 199), None, "GPU busy"),
    "vram": ((214, 110, 0), (6.0, 2.2), "VRAM"),
    "cpu": ((125, 62, 176), (0.6, 1.4), "CPU"),
    "ram": ((0, 130, 120), (2.6, 1.8), "RAM"),
}

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# --------------------------------------------------------------------------- cell building blocks


@dataclass(frozen=True)
class S:
    """A piece of text with its own look. `hl` paints a rounded highlight behind it."""

    text: str
    bold: bool = False
    color: RGB | None = None
    size: float | None = None
    mono: bool = False
    hl: RGB | None = None


@dataclass(frozen=True)
class TCell:
    """A table cell: lines, each a tuple of pieces. `fill` tints the whole cell."""

    lines: tuple[tuple[S, ...], ...]
    fill: RGB | None = None
    align: str | None = None


@dataclass(frozen=True)
class Band:
    """A section heading row across the table (large, tinted, with a rule under it)."""

    text: str
    aside: str = ""


@dataclass(frozen=True)
class Sub:
    """A sub-heading row (a thinking level): a tinted strip."""

    text: str
    aside: str = ""


@dataclass(frozen=True)
class Gap:
    height: float = 6.0


Line = str | S | Sequence[S | str]
Row = Sequence[TCell | str] | Band | Sub | Gap


def _pieces(line: Line) -> tuple[S, ...]:
    if isinstance(line, str):
        return (S(line),)
    if isinstance(line, S):
        return (line,)
    return tuple(S(p) if isinstance(p, str) else p for p in line)


def cell(*lines: Line, fill: RGB | None = None, align: str | None = None) -> TCell:
    """A cell from lines; a line is a string, a piece, or a list of those drawn side by side."""
    return TCell(tuple(_pieces(line) for line in lines), fill, align)


def _as_cell(value: TCell | str) -> TCell:
    return value if isinstance(value, TCell) else cell(*str(value).split("\n"))


def _bold_cell(text: str) -> TCell:
    return cell(*(S(line, bold=True) for line in text.split("\n")))


@dataclass
class _Seg:
    piece: S
    text: str
    width: float
    size: float


@dataclass
class _Line:
    segs: list[_Seg]
    height: float


# --------------------------------------------------------------------------- the document


class Doc(FPDF):
    """A4 document with Unicode fonts, a footer with page numbers and the building blocks the two
    reports share."""

    def __init__(self, *, landscape: bool, footer_left: str):
        super().__init__(orientation="L" if landscape else "P", unit="mm", format="A4")
        self.footer_left = footer_left
        self._pending: tuple[str, str] | None = None
        self.set_margins(14, 14, 14)
        self.c_margin = 0.0
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
        self.set_text_color(*INK)
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

    def ink(
        self, bold: bool = False, size: float = 8, color: RGB = INK, mono: bool = False
    ) -> None:
        self.set_font("mono" if mono else "body", "B" if bold else "", size)
        self.set_text_color(*color)

    # ---- chrome

    def footer(self) -> None:
        self.set_y(-11)
        self.ink(size=7, color=MUTED)
        half = self.inner / 2
        self.cell(half, 4, self.t(self.footer_left))
        self.cell(0, 4, f"Page {self.page_no()} of {{nb}}", align="R")

    def need(self, height: float) -> None:
        if self.will_page_break(height):
            self.add_page()

    @property
    def inner(self) -> float:
        return self.w - self.l_margin - self.r_margin

    @property
    def limit(self) -> float:
        """The lowest y a row may end at."""
        return self.h - self.b_margin

    def h1(self, text: str) -> None:
        self.ink(True, 18)
        self.multi_cell(0, 8.5, self.t(text), align="L", new_x="LMARGIN", new_y="NEXT")

    def h2(self, text: str, aside: str = "") -> None:
        """A section heading: a tinted band with a coloured rule, space above it. It is drawn
        together with what follows it (the table's header and first rows, or the next paragraph or
        chart), so it can never be the last thing on a page."""
        self._flush_heading()
        self._pending = (text, aside)

    def _flush_heading(self, room: float = 40.0) -> None:
        """Draw a heading that waits for its content, which needs `room` mm under it."""
        if self._pending is None:
            return
        text, aside = self._pending
        self._pending = None
        self.ln(4)
        self.need(8.6 + 2 + room)
        self._band(self.get_y(), self.l_margin, self.inner, text, aside)
        self.set_y(self.get_y() + 8.6 + 2)

    def para(self, text: str, *, size: float = 8, gray: bool = True, bold: bool = False) -> None:
        self._flush_heading(12)
        self.ink(bold, size, MUTED if gray else INK)
        self.multi_cell(
            0, size * 0.42 + 1.2, self.t(text), align="L", new_x="LMARGIN", new_y="NEXT"
        )
        self.ink()
        self.ln(0.8)

    # ---- headings

    def _band(self, y: float, x: float, w: float, text: str, aside: str) -> None:
        """A heading band at (x, y): 8 mm of tint and a 0.6 mm accent rule under it."""
        self.set_fill_color(*ACCENT_TINT)
        self.rect(x, y, w, 8, style="F")
        self.set_fill_color(*ACCENT)
        self.rect(x, y + 8, w, 0.6, style="F")
        self.rect(x, y, 1.3, 8, style="F")
        self.ink(True, 11.5, ACCENT_DARK)
        title = self.t(text)
        self.set_xy(x + 4, y)
        self.cell(self.get_string_width(title) + 1, 8, title)
        if aside:
            self.ink(False, 8, MUTED)
            self.cell(0, 8, "   " + self.t(aside))
        self.ink()

    def _sub(self, y: float, x: float, w: float, text: str, aside: str) -> None:
        self.set_fill_color(*SUB_FILL)
        self.rect(x, y, w, 6.4, style="F")
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.line(x, y, x + w, y)
        self.line(x, y + 6.4, x + w, y + 6.4)
        self.ink(True, 8.8, INK, mono=False)
        title = self.t(text)
        self.set_xy(x + 3, y)
        self.cell(self.get_string_width(title) + 1, 6.4, title)
        if aside:
            self.ink(False, 8, MUTED)
            self.cell(0, 6.4, "  · " + self.t(aside))
        self.ink()

    # ---- tables

    def _font_of(self, piece: S, size: float) -> float:
        use = piece.size or size
        self.set_font("mono" if piece.mono else "body", "B" if piece.bold else "", use)
        return use

    def _layout(self, c: TCell, width: float, size: float) -> list[_Line]:
        """The lines a cell takes at `width`: one piece wraps, several pieces share a line."""
        out: list[_Line] = []
        for pieces in c.lines:
            if len(pieces) == 1:
                p = pieces[0]
                use = self._font_of(p, size)
                lh = use * 0.40 + 1.35
                text = self.t(p.text, mono=p.mono)
                parts = self.multi_cell(
                    width, lh, text, dry_run=True, output=MethodReturnValue.LINES, align="L"
                )
                for part in parts or [""]:
                    self._font_of(p, size)
                    out.append(_Line([_Seg(p, part, self.get_string_width(part), use)], lh))
                continue
            segs = []
            for p in pieces:
                use = self._font_of(p, size)
                text = self.t(p.text, mono=p.mono)
                segs.append(_Seg(p, text, self.get_string_width(text), use))
            out.append(_Line(segs, max(s.size for s in segs) * 0.40 + 1.35))
        return out

    def _draw_cell(
        self, x: float, y: float, w: float, lines: list[_Line], align: str, pad_x: float
    ) -> None:
        for line in lines:
            total = sum(s.width for s in line.segs)
            room = w - 2 * pad_x
            if align == "R":
                cx = x + pad_x + max(0.0, room - total)
            elif align == "C":
                cx = x + pad_x + max(0.0, (room - total) / 2)
            else:
                cx = x + pad_x
            for seg in line.segs:
                self._font_of(seg.piece, seg.size)
                if seg.piece.hl and seg.text.strip():
                    self.set_fill_color(*seg.piece.hl)
                    self.rect(
                        cx - 0.9,
                        y + 0.35,
                        seg.width + 1.8,
                        line.height - 0.7,
                        style="F",
                        round_corners=True,
                        corner_radius=0.9,
                    )
                self.set_text_color(*(seg.piece.color or INK))
                self.set_xy(cx, y)
                self.cell(seg.width + 0.01, line.height, seg.text)
                cx += seg.width
            y += line.height
        self.set_text_color(*INK)

    def grid(
        self,
        header: Sequence[TCell | str] | None,
        rows: Sequence[Row],
        widths: Sequence[float],
        aligns: str,
        *,
        size: float = 8,
        zebra: bool = True,
        pad_x: float = 1.8,
        pad_y: float = 1.4,
    ) -> None:
        """A table drawn row by row. `widths` in mm, `aligns` one of L/R/C per column. Cells are
        strings or `cell(...)`; `Band`/`Sub`/`Gap` rows are headings and spacing. The header
        repeats on every page the table spans. A row is never split across pages, and a heading
        (with the gap before it) always moves together with the first rows under it, so it can
        never be the last thing on a page."""
        x0 = self.l_margin
        total = sum(widths)
        xs = [x0 + sum(widths[:i]) for i in range(len(widths))]
        limit = self.limit
        self.set_auto_page_break(False)

        def measure(row: Sequence[TCell | str]) -> tuple[list[TCell], list[list[_Line]], float]:
            cells = [_as_cell(c) for c in row]
            laid = [
                self._layout(c, widths[i] - 2 * pad_x, size)
                for i, c in enumerate(cells[: len(widths)])
            ]
            height = max((sum(ln.height for ln in lines) for lines in laid), default=0.0)
            return cells, laid, height + 2 * pad_y

        head = (
            measure([c if isinstance(c, TCell) else _bold_cell(str(c)) for c in header])
            if header is not None
            else None
        )
        # (kind, payload, height) for every row; data rows are measured once
        plan: list[tuple[str, Any, float]] = []
        for r in rows:
            if isinstance(r, Band):
                plan.append(("band", r, 8.6))
            elif isinstance(r, Sub):
                plan.append(("sub", r, 6.4))
            elif isinstance(r, Gap):
                plan.append(("gap", r, r.height))
            else:
                data = measure(r)
                plan.append(("row", data, data[2]))

        def keep(i: int) -> float:
            """Height from row i down to and including the second data row after it."""
            h, rows_seen = 0.0, 0
            for kind, _, rh in plan[i:]:
                h += rh
                if kind == "row":
                    rows_seen += 1
                    if rows_seen == 2:
                        break
            return h

        y = self.get_y()

        def draw_head(at: float) -> float:
            assert head is not None
            cells, laid, h = head
            self.set_fill_color(*HEAD_FILL)
            self.rect(x0, at, total, h, style="F")
            for i, lines in enumerate(laid):
                self._draw_cell(
                    xs[i], at + pad_y, widths[i], lines, cells[i].align or aligns[i], pad_x
                )
            self.set_draw_color(*INK)
            self.set_line_width(0.35)
            self.line(x0, at + h, x0 + total, at + h)
            return at + h

        def new_page() -> float:
            self.add_page()
            return self.t_margin

        title, self._pending = self._pending, None
        lead = (4.0 + 8.6 + 2.0) if title else 0.0
        first_need = lead + (head[2] if head else 0.0) + (keep(0) if plan else 0.0)
        if y + first_need > limit:
            y = new_page()
        fresh = y <= self.t_margin + 0.01
        if title:
            if not fresh:
                y += 4.0
            self._band(y, self.l_margin, self.inner, *title)
            y += 8.6 + 2.0
        if head:
            y = draw_head(y)
        stripe = 0
        group: Sub | None = None
        for i, (kind, payload, h) in enumerate(plan):
            if kind == "gap":
                if fresh:
                    continue
                if y + keep(i) > limit:
                    y = new_page()
                    fresh = True
                    if head:
                        y = draw_head(y)
                    continue
                y += h
                continue
            need = keep(i) if kind in ("band", "sub") else h
            if y + need > limit:
                y = new_page()
                if head:
                    y = draw_head(y)
                if kind == "row" and group is not None:
                    # a group that goes on over the page says which one it is
                    self._sub(y, x0, total, f"{group.text} (continued)", group.aside)
                    y += 6.4
                    stripe = 0
            fresh = False
            if kind == "band":
                self._band(y, x0, total, payload.text, payload.aside)
                stripe = 0
                group = None
            elif kind == "sub":
                self._sub(y, x0, total, payload.text, payload.aside)
                stripe = 0
                group = payload
            else:
                cells, laid, _ = payload
                if zebra and stripe % 2 == 1:
                    self.set_fill_color(*ZEBRA)
                    self.rect(x0, y, total, h, style="F")
                for i2, c in enumerate(cells[: len(widths)]):
                    if c.fill:
                        self.set_fill_color(*c.fill)
                        self.rect(xs[i2], y, widths[i2], h, style="F")
                for i2, lines in enumerate(laid):
                    self._draw_cell(
                        xs[i2], y + pad_y, widths[i2], lines, cells[i2].align or aligns[i2], pad_x
                    )
                self.set_draw_color(*RULE)
                self.set_line_width(0.15)
                self.line(x0, y + h, x0 + total, y + h)
                stripe += 1
            y += h
        self.set_auto_page_break(True, margin=16)
        self.set_xy(self.l_margin, y + 2)

    def facts(self, items: Sequence[tuple[str, str]], label_w: float = 34) -> None:
        """Label / value pairs (the page's definition lists)."""
        rows = [[cell(S(k, bold=True, color=MUTED)), cell(v)] for k, v in items]
        self.grid(None, rows, (label_w, self.inner - label_w), "LL", size=8.5, zebra=False)

    def tiles(self, items: Sequence[tuple[str, str, RGB | None]], cols: int = 4) -> None:
        """Big numbers in cards: label above, the value large (in a colour when it has one)."""
        self._flush_heading(24)
        gap, card_h = 3.0, 15.5
        w = (self.inner - gap * (cols - 1)) / cols
        for start in range(0, len(items), cols):
            self.need(card_h + 2)
            y = self.get_y()
            for n, (label, value, colour) in enumerate(items[start : start + cols]):
                x = self.l_margin + n * (w + gap)
                self.set_fill_color(*ZEBRA)
                self.set_draw_color(*RULE)
                self.set_line_width(0.2)
                self.rect(x, y, w, card_h, style="DF", round_corners=True, corner_radius=1.5)
                self.ink(False, 7.5, MUTED)
                self.set_xy(x + 3, y + 1.8)
                self.cell(w - 6, 4, self.t(label))
                self.ink(True, 14, colour or INK)
                self.set_xy(x + 3, y + 6.6)
                self.cell(w - 6, 7, self.t(value))
            self.set_y(y + card_h + gap)
        self.ink()

    # ---- chart

    def load_chart(self, points: dict[str, list[Any]], attempt_starts: Sequence[float]) -> None:
        """CPU, RAM, GPU and VRAM in percent over the run: coloured vector lines (each with its
        own dash pattern as well, for a black-and-white print) and a legend."""
        self._flush_heading(60)
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
        height, label_h = 46.0, 10.0
        self.need(height + label_h + 12)
        self.ink(True, 9.5)
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

        self.set_fill_color(250, 250, 252)
        self.rect(left, top, width, height, style="F")
        self.ink(False, 7, MUTED)
        self.set_line_width(0.15)
        for v in (0, 25, 50, 75, 100):
            self.set_draw_color(*(RULE if v not in (0, 100) else (130, 130, 138)))
            self.line(left, y(v), left + width, y(v))
            self.set_xy(self.l_margin, y(v) - 1.6)
            self.cell(7.5, 3.2, f"{v}%", align="R")
        self.set_draw_color(130, 130, 138)
        self.line(left, top, left, top + height)
        ticks = 6
        for i in range(ticks):
            tick = t0 + span * i / (ticks - 1)
            self.line(x(tick), top + height, x(tick), top + height + 1.2)
            label = _clock(tick - t0)
            w = self.get_string_width(label)
            lx = min(max(x(tick) - w / 2, left - 3), left + width - w)
            self.set_xy(lx, top + height + 1.6)
            self.cell(w + 0.5, 3.2, label)
        self.set_draw_color(150, 150, 158)
        for start in attempt_starts:
            if t0 - 1 <= start <= t0 + span + 1:
                self.line(x(start), top + height - 1.6, x(start), top + height)
        for key in keys:
            colour, dash, _ = PALETTE[key]
            self.set_draw_color(*colour)
            self.set_line_width(0.5)
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
            self.set_line_width(0.6)
            if dash:
                self.set_dash_pattern(dash=dash[0], gap=dash[1])
            self.line(gx, gy, gx + 14, gy)
            self.set_dash_pattern()
            self.set_x(gx + 15.5)
            self.ink(False, 7.8, INK)
            self.cell(self.get_string_width(name) + 6, 3.6, name)
        self.set_draw_color(150, 150, 158)
        self.set_line_width(0.15)
        gx, gy = self.get_x(), self.get_y() + 1.7
        self.line(gx, gy - 0.6, gx, gy + 1.2)
        self.set_x(gx + 1.5)
        self.ink(False, 7.8, MUTED)
        self.cell(40, 3.6, "an attempt starts")
        self.set_draw_color(*RULE)
        self.set_line_width(0.2)
        self.set_y(top + height + 12)
        self.ink()


def _clock(s: float) -> str:
    t = max(0, int(s + 0.5))
    h, m, sec = t // 3600, (t % 3600) // 60, t % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
