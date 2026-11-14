"""Health / indexer triage.

Health problems of the *arr apps and Prowlarr reach the report as `findings` in the `arr`
collector result (see `collectors.arr.build_findings`). What this adds is action: for every
indexer Prowlarr currently reports as failing, request a connectivity test.
"""

from __future__ import annotations

from typing import Any

from .base import SOURCE, AutomationEnv, recent_args, short, tally


async def run(env: AutomationEnv) -> dict[str, Any]:
    result = env.collector_results().get("arr")
    if result is None or not result.ok:
        return {"skipped": "no usable arr collector result yet"}
    prowlarr = result.data.get("prowlarr") or {}
    failing = [i for i in prowlarr.get("indexers", []) if i.get("failing") and i.get("enabled")]
    seen = {a["indexer_id"] for a in await recent_args(env, "prowlarr_test_indexer")}
    records = []
    for indexer in failing:
        if indexer["id"] in seen:
            continue
        records.append(
            await env.service.request(
                "prowlarr_test_indexer",
                {"indexer_id": indexer["id"], "title": short(str(indexer["name"]))},
                SOURCE,
                reason=f"Prowlarr reports it failing until {indexer['disabled_till']}",
            )
        )
    return {"failing": len(failing), "requested": len(records), "outcomes": tally(records)}
