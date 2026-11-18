# Integration branch `integration/m3-m10`

Merge of `claude/m3-ext-collectors-7df402` (19f64f8) into the M4/M5/M8 line (12ab6a5), plus a
wiring commit. Neither source branch was rewritten.

**Conflicts**
- `context.py`: kept both (events/metrics/reports services + action layer, approvals sweep, automations).
- `main.py`: both imports/routers. `test_app.py`: job-name set now includes `approvals:sweep`.
- `0002_placeholder_m4.sql` removed: migrations are `0001_initial`, `0002_reports`, `0003_actions`.
- `manager.py`/`config.py`/`DESIGN.md` merged automatically.

**Wiring** (`outputs/action_events.py`)
- Action notifiers -> event bus (`action.suggested`, `approval.pending|decided|expired`, `action.result`; compact payloads) and `asm_actions_total`.
- `asm_pending_approvals` as a `MetricsHub` provider (one COUNT).
- Digest: pending-approvals info flag.
- `/api/status.pending_approvals`; `frontend/src/api-types.ts` regenerated.

**Notes**
- `approval.decided` is derived: published before `action.result` when `decided_by` is a human/timeout (not `policy*`).
