"""Prometheus metrics for GET /metrics, on `prometheus_client` with the app's own registry (no
default process metrics). Every value is updated when something happens (an LLM call, an
action, a job run) and only read at scrape time: a scrape does no I/O.

    asm_build_info{version}                         gauge    1
    asm_llm_requests_total{result}                  counter  LLM requests (ok, error)
    asm_llm_tokens_total{direction}                 counter  prompt / completion tokens
    asm_llm_errors_total{type}                      counter  failed LLM requests by error type
    asm_llm_request_duration_seconds                histogram
    asm_actions_total{action,result}                counter  actions by type and status
    asm_pending_approvals                           gauge    action items waiting for a decision
    asm_pending_approval_batches                    gauge    approvals waiting for a decision
    asm_job_status{job}                     gauge    latest run: 0 ok, 1 warn, 2 crit, 3 error
    asm_job_last_run_timestamp_seconds{job}         gauge    when the latest run finished
    asm_job_last_run_duration_seconds{job}          gauge
    asm_job_findings{job,severity}                  gauge    findings of the latest run
    asm_job_runs_total{job,status}                  counter  finished runs since the start
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)

disable_created_metrics()  # no `*_created` series next to every counter

CONTENT_TYPE = CONTENT_TYPE_LATEST
LLM_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300)
STATUS_VALUE = {"ok": 0, "warn": 1, "crit": 2, "error": 3}
SEVERITIES = ("info", "warn", "crit")


class MetricsHub:
    def __init__(self, version: str = "") -> None:
        r = self.registry = CollectorRegistry()
        self.build_info = Gauge("asm_build_info", "Build information.", ["version"], registry=r)
        if version:
            self.build_info.labels(version=version).set(1)
        self.llm_requests = Counter(
            "asm_llm_requests", "LLM requests by result (ok, error).", ["result"], registry=r
        )
        self.llm_tokens = Counter(
            "asm_llm_tokens",
            "LLM tokens (prompt, completion) as the endpoint reported them.",
            ["direction"],
            registry=r,
        )
        self.llm_errors = Counter(
            "asm_llm_errors", "Failed LLM requests by error type.", ["type"], registry=r
        )
        self.llm_duration = Histogram(
            "asm_llm_request_duration_seconds",
            "Duration of successful LLM requests.",
            buckets=LLM_BUCKETS,
            registry=r,
        )
        self.actions = Counter(
            "asm_actions",
            "Actions by type and status (suggested, pending, executed, failed, dry_run, "
            "rejected, deferred, expired).",
            ["action", "result"],
            registry=r,
        )
        self.pending_approvals = Gauge(
            "asm_pending_approvals", "Actions (items) waiting for a human decision.", registry=r
        )
        self.pending_batches = Gauge(
            "asm_pending_approval_batches",
            "Approvals (batches of items) waiting for a human decision.",
            registry=r,
        )
        self.job_status = Gauge(
            "asm_job_status",
            "Status of the job's latest finished run: 0 ok, 1 warn, 2 crit, 3 error.",
            ["job"],
            registry=r,
        )
        self.job_last_run = Gauge(
            "asm_job_last_run_timestamp_seconds",
            "When the job's latest run finished (Unix time).",
            ["job"],
            registry=r,
        )
        self.job_duration = Gauge(
            "asm_job_last_run_duration_seconds",
            "How long the job's latest run took.",
            ["job"],
            registry=r,
        )
        self.job_findings = Gauge(
            "asm_job_findings",
            "Findings of the job's latest run by severity.",
            ["job", "severity"],
            registry=r,
        )
        self.job_runs = Counter(
            "asm_job_runs", "Finished job runs by status.", ["job", "status"], registry=r
        )
        for result in ("ok", "error"):
            self.llm_requests.labels(result=result)
        for direction in ("prompt", "completion"):
            self.llm_tokens.labels(direction=direction)

    # ------------------------------------------------------------------ updates

    def on_llm_call(self, call: Mapping[str, Any]) -> None:
        """One recorded LLM request (the trace's `llm.call` event)."""
        if call.get("status") == "ok":
            if call.get("total_ms") is not None:
                self.llm_duration.observe(call["total_ms"] / 1000)
            self.llm_requests.labels(result="ok").inc()
            for direction in ("prompt", "completion"):
                tokens = call.get(f"{direction}_tokens")
                if tokens:
                    self.llm_tokens.labels(direction=direction).inc(tokens)
        else:
            self.llm_requests.labels(result="error").inc()
            self.llm_errors.labels(type=str(call.get("error_type") or "error")).inc()

    def on_action(self, action: str, result: str, amount: int = 1) -> None:
        self.actions.labels(action=action, result=result).inc(amount)

    def set_pending(self, items: int, batches: int) -> None:
        self.pending_approvals.set(items)
        self.pending_batches.set(batches)

    def set_job_run(self, run: Mapping[str, Any], *, count: bool) -> None:
        """The latest finished run of a job; `count` adds it to `asm_job_runs_total`."""
        job = str(run["job_slug"])
        status = str(run.get("status"))
        if status not in STATUS_VALUE:
            return
        self.job_status.labels(job=job).set(STATUS_VALUE[status])
        if run.get("finished"):
            self.job_last_run.labels(job=job).set(run["finished"])
        if run.get("duration_s") is not None:
            self.job_duration.labels(job=job).set(run["duration_s"])
        found = {s: 0 for s in SEVERITIES}
        for finding in run.get("findings") or []:
            severity = finding.get("severity")
            if severity in found:
                found[severity] += 1
        for severity, n in found.items():
            self.job_findings.labels(job=job, severity=severity).set(n)
        if count:
            self.job_runs.labels(job=job, status=status).inc()

    def forget_jobs(self, keep: Iterable[str]) -> None:
        """Drop the series of jobs that no longer exist."""
        wanted = set(keep)
        for gauge in (self.job_status, self.job_last_run, self.job_duration):
            for (job,) in list(gauge._metrics):
                if job not in wanted:
                    gauge.remove(job)
        for job, severity in list(self.job_findings._metrics):
            if job not in wanted:
                self.job_findings.remove(job, severity)

    # ------------------------------------------------------------------ scrape

    def render(self) -> bytes:
        return generate_latest(self.registry)
