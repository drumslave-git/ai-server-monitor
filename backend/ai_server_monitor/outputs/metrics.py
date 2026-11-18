"""Prometheus exposition for GET /metrics (hand-rolled text format 0.0.4, no dependency).

Two ways to get numbers in:

* **Instruments** held by `MetricsHub`: counters, gauges and histograms updated from hooks
  (collector manager, LLM client, the event bus). Updating one is a dict write; a scrape
  never touches collectors, the LLM or (apart from one "latest report" row) the database.
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

from ..collectors.base import CollectorResult
from ..llm.client import Usage

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
        d("asm_reports_total", "counter", "Report attempts by result (ok, failed, skipped).")
        d("asm_collector_up", "gauge", "1 if the collector's latest run succeeded.")
        d("asm_collector_duration_seconds", "gauge", "Duration of the collector's latest run.")
        d(
            "asm_collector_errors_total",
            "counter",
            "Collector runs that failed since the service started.",
        )
        d(
            "asm_collector_last_success_timestamp_seconds",
            "gauge",
            "Unix time of the collector's last successful run.",
        )
        d(
            "asm_llm_request_duration_seconds",
            "histogram",
            "Duration of successful LLM requests.",
            LLM_BUCKETS,
        )
        d("asm_llm_requests_total", "counter", "LLM requests by result (ok, error).")
        d("asm_llm_tokens_total", "counter", "LLM tokens (prompt, completion); estimated if the "
          "endpoint does not report usage.")  # fmt: skip
        d("asm_llm_errors_total", "counter", "Failed LLM requests by error type.")
        d(
            "asm_actions_total",
            "counter",
            "Actions by type and status (suggested, pending, executed, failed, dry_run, "
            "rejected, deferred, expired).",
        )
        for result in ("ok", "failed", "skipped"):
            self.inc("asm_reports_total", {"result": result}, 0)
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

    # -- hooks (called by the manager / LLM client / event consumers) ------------

    def on_collector_result(self, result: CollectorResult) -> None:
        labels = {"collector": result.name}
        self.set("asm_collector_up", 1 if result.ok else 0, labels)
        self.set("asm_collector_duration_seconds", round(result.duration_s, 3), labels)
        if result.ok:
            self.set("asm_collector_last_success_timestamp_seconds", result.ts, labels)
            self.inc("asm_collector_errors_total", labels, 0)  # series exists from the start
        else:
            self.inc("asm_collector_errors_total", labels)

    def reset_collectors(self) -> None:
        """Collectors were rebuilt (settings changed): drop series of ones that are gone."""
        for name in (
            "asm_collector_up",
            "asm_collector_duration_seconds",
            "asm_collector_last_success_timestamp_seconds",
        ):
            self.clear(name)

    def on_llm_success(self, usage: Usage, latency_s: float) -> None:
        self.observe("asm_llm_request_duration_seconds", latency_s)
        self.inc("asm_llm_requests_total", {"result": "ok"})
        self.inc("asm_llm_tokens_total", {"direction": "prompt"}, usage.prompt_tokens)
        self.inc("asm_llm_tokens_total", {"direction": "completion"}, usage.completion_tokens)

    def on_llm_error(self, exc: Exception) -> None:
        self.inc("asm_llm_requests_total", {"result": "error"})
        self.inc("asm_llm_errors_total", {"type": type(exc).__name__})

    def on_report_event(self, event_type: str, data: Mapping[str, Any]) -> None:
        if event_type == "report.done":
            self.inc("asm_reports_total", {"result": "ok"})
        elif event_type == "report.failed":
            self.inc("asm_reports_total", {"result": str(data.get("status") or "failed")})

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


# --------------------------------------------------------------------------- report gauges


def report_families(latest: Mapping[str, Any] | None, version: str) -> list[Family]:
    """Gauges describing the latest successful report (a row the endpoint just read)."""
    out = [
        Family("asm_build_info", "gauge", "Build information.", [Sample(1, {"version": version})])
    ]
    if latest is None:
        return out
    health = latest.get("health")
    findings = latest.get("findings") or []
    out += [
        Family(
            "asm_health_score",
            "gauge",
            "Health score of the latest report (0-100).",
            [Sample(latest["score"])] if latest.get("score") is not None else [],
        ),
        Family(
            "asm_health_status",
            "gauge",
            "Health of the latest report, one-hot over ok/warn/crit.",
            [Sample(1 if health == s else 0, {"status": s}) for s in ("ok", "warn", "crit")]
            if health in ("ok", "warn", "crit")
            else [],
        ),
        Family(
            "asm_findings",
            "gauge",
            "Findings in the latest report by severity.",
            [
                Sample(sum(1 for f in findings if f.get("severity") == sev), {"severity": sev})
                for sev in ("info", "warn", "crit")
            ],
        ),
        Family(
            "asm_report_last_success_timestamp_seconds",
            "gauge",
            "Unix time of the latest successful report.",
            [Sample(latest["ts"])],
        ),
        Family(
            "asm_report_generation_seconds",
            "gauge",
            "How long the latest successful report took to generate.",
            [Sample(latest["latency_ms"] / 1000)] if latest.get("latency_ms") is not None else [],
        ),
    ]
    return out
