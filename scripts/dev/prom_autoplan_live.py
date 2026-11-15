"""One real planning run: a live Prometheus (GET only) and a live OpenAI-compatible LLM.

    python scripts/dev/prom_autoplan_live.py OUT.md --prom https://prometheus.example \
        --llm https://ollama.example/v1 --model gemma4:e4b

Uses a throw-away SQLite database (nothing is written to the live instance), prints what the
model was asked and answered, which queries were accepted or rejected and why, runs the
collector once and shows the digest section the report model would see.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from ai_server_monitor.collectors.base import HostEnv  # noqa: E402
from ai_server_monitor.collectors.prometheus import PrometheusCollector  # noqa: E402
from ai_server_monitor.config import (
    ReportSettings,
    Settings,
)
from ai_server_monitor.db import Database  # noqa: E402
from ai_server_monitor.digest import build_digest  # noqa: E402
from ai_server_monitor.llm import LLMClient  # noqa: E402
from ai_server_monitor.llm.text import estimate_tokens  # noqa: E402
from ai_server_monitor.promauto import Planner, QueryStore  # noqa: E402
from ai_server_monitor.promauto.catalog import build_catalog  # noqa: E402
from ai_server_monitor.promql import PromQL  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--prom", required=True)
    ap.add_argument("--llm", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--timeout", default="5m")
    args = ap.parse_args()

    settings = Settings(
        llm={"base_url": args.llm, "model": args.model, "timeout": args.timeout, "max_retries": 1},
        collectors={"prometheus": {"url": args.prom, "queries": []}},
    )
    tmp = tempfile.mkdtemp(prefix="asm-live-")
    db = Database(Path(tmp) / "live.db")
    await db.connect()
    llm = LLMClient(settings.llm)
    exchanges: list[tuple[str, str, float]] = []
    original = llm.complete

    async def traced(messages, **kw):  # type: ignore[no-untyped-def]
        started = time.perf_counter()
        completion = await original(messages, **kw)
        exchanges.append(
            (messages[-1]["content"], completion.content, time.perf_counter() - started)
        )
        return completion

    llm.complete = traced  # type: ignore[method-assign]
    planner = Planner(settings, db, llm)

    prom_cfg = settings.collectors.prometheus
    client = PromQL(prom_cfg.url)
    big = PromQL(prom_cfg.url, max_series=5000)
    catalog = await build_catalog(client, big)
    await client.close()
    await big.close()

    started = time.perf_counter()
    outcome = await planner.run(force=True)
    took = time.perf_counter() - started
    store = QueryStore(db)
    rows = await store.fetch()
    plan = await db.kv_get("prom_plan")

    collector = PrometheusCollector(prom_cfg, HostEnv(), store=store)
    data = await collector.collect()
    await collector.close()
    snap = {
        "prometheus": {
            "state": "active",
            "reason": None,
            "ok": True,
            "error": None,
            "last_ts": time.time(),
            "result": {"ts": time.time(), "ok": True, "data": data, "error": None},
        }
    }
    digest = await build_digest(snap, db, ReportSettings())
    await llm.aclose()
    await db.close()

    out = [f"# Prometheus auto-plan, live run ({time.strftime('%Y-%m-%d %H:%M')})", ""]
    out.append(
        f"Prometheus `{args.prom}`, model `{args.model}` at `{args.llm}`. GET requests only; a"
    )
    out.append("throw-away database, nothing written to the live monitor.\n")
    out.append("## Catalog slices (what the model was shown)\n")
    for j in catalog.jobs:
        out.append(
            f"- `{j.job}`: {len(j.targets)} target(s), {len(j.metrics)} metrics after filtering, "
            f"slice {len(j.text)} chars ≈ {estimate_tokens(j.text)} tokens"
            + (f", other host {j.other_host}" if j.other_host else "")
        )
    out.append(f"\nCatalog hash `{catalog.hash}`; {catalog.total_metrics} metric names in all.\n")
    out.append(
        f"## Outcome\n\n`{outcome.status}` — {outcome.message} ({took:.0f}s, "
        f"{len(exchanges)} LLM calls). Counts: {outcome.counts}\n"
    )
    out.append("## Accepted queries (each was run against Prometheus)\n")
    out.append("| name | job | query | limits | last result |")
    out.append("|---|---|---|---|---|")
    for q in rows:
        lv = q["last_value"] or {}
        series = lv.get("series") or []
        shown = "; ".join(
            f"{json.dumps(s['labels'], ensure_ascii=False) if s['labels'] else ''} {s['value']}".strip()
            for s in series[:2]
        ) or ("(empty — alert-style)" if q["kind"] == "alert" else "")
        limits = ", ".join(
            x
            for x in (
                q["warn_if"] and f"warn {q['warn_if']}",
                q["crit_if"] and f"crit {q['crit_if']}",
            )
            if x
        )
        out.append(
            f"| {q['name']} | {q['job']} | `{q['query']}` | {limits} | {shown} ({lv.get('total', 0)} series) |"
        )
    out.append("\n## Rejected proposals\n")
    for r in (plan or {}).get("rejected") or []:
        out.append(f"- `{r['job']}` **{r['name']}**: {r['reason']}")
    if not (plan or {}).get("rejected"):
        out.append("none")
    out.append("\n## What the model saw and answered\n")
    for i, (prompt, reply, secs) in enumerate(exchanges, start=1):
        out.append(f"### call {i} ({secs:.1f}s)\n\n<details><summary>last message sent</summary>\n")
        out.append("```\n" + prompt[:3500] + "\n```\n</details>\n")
        out.append("Reply:\n\n```\n" + reply.strip()[:2500] + "\n```\n")
    out.append("## Resulting digest section\n")
    text = digest.text
    start = text.find("## Prometheus")
    section = text[start:].split("\n\n## ")[0] if start >= 0 else "(no Prometheus section)"
    out.append("```\n" + section + "\n```\n")
    out.append(f"Flags: {[f.to_dict() for f in digest.flags] or 'none'}\n")
    Path(args.out).write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {args.out}: {outcome.status}, {len(rows)} queries, {len(exchanges)} LLM calls")


if __name__ == "__main__":
    asyncio.run(main())
