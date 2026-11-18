"""Prometheus exposition for GET /metrics (hand-rolled text format 0.0.4, no dependency).

Two ways to get numbers in:

* **Instruments** held by `MetricsHub`: counters, gauges and histograms updated from hooks
  (the event bus: LLM calls recorded by the trace, actions). Updating one is a dict write; a
  scrape never touches the LLM.
* **Providers**: `hub.add_provider(fn)` where `fn()` returns `Family` objects (sync or
  async), evaluated at scrape time. Other modules plug in this way.

The action layer series are added by `AppContext` (a provider for the two gauges) and
`outputs/action_events.py` (the `asm_actions_total` counter):

    asm_actions_total{action,result}          counter  actions by type and status
    asm_pending_approvals                     gauge    action items waiting for a decision
    asm_pending_approval_batches              gauge    approvals (batches of items) waiting
"""

from __future__ import annotations

import inspect
import logging
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
LLM_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300)

Labels = Mapping[str, str]


@dataclass
class Sample:
    value: float
    labels: Labels = field(default_factory=dict)
    suffix: str = ""
    """'' for plain series; '_bucket', '_sum', '_count' for histograms."""


@dataclass
class Family:
    name: str
    type: str  # counter | gauge | histogram
    help: str
    samples: list[Sample] = field(default_factory=list)


Provider = Callable[[], Iterable[Family] | Awaitable[Iterable[Family]]]


# --------------------------------------------------------------------------- text format


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_value(value: float) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def render_families(families: Iterable[Family]) -> str:
    lines: list[str] = []
    for fam in families:
        if not fam.samples:
            continue
        lines.append(f"# HELP {fam.name} {_escape_help(fam.help)}")
        lines.append(f"# TYPE {fam.name} {fam.type}")
        for s in fam.samples:
            labels = ""
            if s.labels:
                inner = ",".join(f'{k}="{_escape_label(str(v))}"' for k, v in s.labels.items())
                labels = "{" + inner + "}"
            lines.append(f"{fam.name}{s.suffix}{labels} {_fmt_value(s.value)}")
    return "\n".join(lines) + "\n" if lines else ""


# --------------------------------------------------------------------------- instruments


def _key(labels: Labels) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


@dataclass
class _Hist:
    buckets: tuple[float, ...]
    counts: list[int]
    total: float = 0.0
    n: int = 0


class MetricsHub:
    def __init__(self) -> None:
        self._meta: dict[
            str, tuple[str, str, tuple[float, ...]]
        ] = {}  # name -> type, help, buckets
        self._values: dict[str, dict[tuple[tuple[str, str], ...], Any]] = {}
        self._providers: list[Provider] = []
        self._define_builtin()

    # -- definitions ------------------------------------------------------------

    def define(self, name: str, kind: str, help_: str, buckets: tuple[float, ...] = ()) -> None:
        self._meta[name] = (kind, help_, buckets)
        self._values.setdefault(name, {})
        if kind == "histogram":  # an unobserved histogram still exposes its (zero) buckets
            self._values[name][()] = _Hist(buckets, [0] * len(buckets))

    def _define_builtin(self) -> None:
        d = self.define
        d(
            "asm_llm_request_duration_seconds",
            "histogram",
            "Duration of successful LLM requests.",
            LLM_BUCKETS,
        )
        d("asm_llm_requests_total", "counter", "LLM requests by result (ok, error).")
        d("asm_llm_tokens_total", "counter", "LLM tokens (prompt, completion) as the endpoint "
          "reported them.")  # fmt: skip
        d("asm_llm_errors_total", "counter", "Failed LLM requests by error type.")
        d(
            "asm_actions_total",
            "counter",
            "Actions by type and status (suggested, pending, executed, failed, dry_run, "
            "rejected, deferred, expired).",
        )
        for result in ("ok", "error"):
            self.inc("asm_llm_requests_total", {"result": result}, 0)
        for direction in ("prompt", "completion"):
            self.inc("asm_llm_tokens_total", {"direction": direction}, 0)

    # -- updates ----------------------------------------------------------------

    def inc(self, name: str, labels: Labels | None = None, amount: float = 1) -> None:
        series = self._values[name]
        k = _key(labels or {})
        series[k] = series.get(k, 0) + amount

    def set(self, name: str, value: float, labels: Labels | None = None) -> None:
        self._values[name][_key(labels or {})] = value

    def observe(self, name: str, value: float, labels: Labels | None = None) -> None:
        series = self._values[name]
        k = _key(labels or {})
        hist = series.get(k)
        if hist is None:
            buckets = self._meta[name][2]
            hist = series[k] = _Hist(buckets, [0] * len(buckets))
        for i, bound in enumerate(hist.buckets):
            if value <= bound:
                hist.counts[i] += 1
        hist.total += value
        hist.n += 1

    def clear(self, name: str) -> None:
        self._values[name].clear()

    def add_provider(self, provider: Provider) -> None:
        self._providers.append(provider)

    # -- hooks (called by event consumers) ----------------------------------------

    def on_llm_call(self, call: Mapping[str, Any]) -> None:
        """One recorded LLM request (the trace's `llm.call` event)."""
        if call.get("status") == "ok":
            if call.get("total_ms") is not None:
                self.observe("asm_llm_request_duration_seconds", call["total_ms"] / 1000)
            self.inc("asm_llm_requests_total", {"result": "ok"})
            for direction in ("prompt", "completion"):
                tokens = call.get(f"{direction}_tokens")
                if tokens:
                    self.inc("asm_llm_tokens_total", {"direction": direction}, tokens)
        else:
            self.inc("asm_llm_requests_total", {"result": "error"})
            self.inc("asm_llm_errors_total", {"type": str(call.get("error_type") or "error")})

    # -- rendering --------------------------------------------------------------

    def families(self) -> list[Family]:
        out: list[Family] = []
        for name, (kind, help_, _buckets) in self._meta.items():
            samples: list[Sample] = []
            for k, value in sorted(self._values[name].items()):
                labels = dict(k)
                if kind == "histogram":
                    for bound, count in zip(value.buckets, value.counts, strict=True):
                        samples.append(
                            Sample(count, {**labels, "le": _fmt_value(bound)}, "_bucket")
                        )
                    samples.append(Sample(value.n, {**labels, "le": "+Inf"}, "_bucket"))
                    samples.append(Sample(value.total, labels, "_sum"))
                    samples.append(Sample(value.n, labels, "_count"))
                else:
                    samples.append(Sample(value, labels))
            out.append(Family(name, kind, help_, samples))
        return out

    async def render(self, extra: Iterable[Family] = ()) -> str:
        families = [*self.families(), *extra]
        for provider in self._providers:
            try:
                result = provider()
                if inspect.isawaitable(result):
                    result = await result
                families.extend(result)
            except Exception:
                log.exception("metrics provider %r failed", provider)
        return render_families(families)


# --------------------------------------------------------------------------- build info


def build_info_families(version: str) -> list[Family]:
    return [
        Family("asm_build_info", "gauge", "Build information.", [Sample(1, {"version": version})])
    ]
