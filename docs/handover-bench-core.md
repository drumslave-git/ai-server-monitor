# Handover: benchmark core (runner, world, storage, API, page)

State: everything below is implemented on `main` (or in the working tree for the second commit, see
the report). Scenario content belongs to the scenarios worker (`bench/scenarios/`, `bench/snapshots/`).

## Where things are

| Piece | File |
|---|---|
| Scenario format, loader, merge-patch, set hash | `backend/ai_server_monitor/bench/schema.py` (docs: `docs/benchmarks.md`) |
| Fake world: settings profile, fake collectors, HTTP rules (gateway, *arr, PromQL), fake Prometheus, fake MCP, `run_command` / `disk_usage` executors | `bench/world.py` |
| Check registry (19 types), `Outcome`, scoring | `bench/checks.py` |
| One attempt through the real report / chat / planner code, metrics from the trace, summaries | `bench/runner.py` |
| Run orchestration, progress events, private snapshots | `bench/service.py` |
| Backend probe (Ollama `/api/version` + `/api/show`, llama.cpp `/props`, vLLM, `/v1/models`) | `bench/probe.py` |
| Judge | `bench/judge.py` |
| Storage (migration `0008_bench.sql`) | `bench/store.py` |
| HTTP API `/api/bench/*` | `bench/api.py` |
| Settings `bench` section (repeats, attempt_timeout, judge) | `config.py` |
| Page | `frontend/src/pages/Benchmarks.tsx`, `pages/bench/*` |

Small hooks in shared code: `ChatEngine(tools_builder=...)`, `build_tools(assume_linux=...)`,
`host_tools(assume_linux)`, `repairs` in the chat `done` meta, `AppContext.bench`.

## How a run works

- The run takes a frozen copy of Settings → LLM at start; tool mode `auto` = whatever the app's own
  resolver gives for that model (probed once, cached 7 days), recorded as `meta.tool_mode`.
- Each attempt builds a throw-away world: in-memory DB, fake collectors from snapshot + patch
  (timestamps rebased when the snapshot has `now`), the real `ApiGateway` / action layer over a
  mock HTTP transport driven by `world.api`, real tool schemas (host tools offered on any OS).
- LLM calls go through a benchmark-owned `Tracing` (always on, into the real DB), so timings and
  token counts come from `llm_calls`; afterwards the attempt's calls are relabelled
  `purpose=bench`, `link_kind=bench`, `link_id=<run>/<scenario>/<attempt>` (Logs page shows them,
  and links back to the run). They are pruned with the normal Logs retention; the attempt row keeps
  its numbers.
- Report overlap = the report job was running at start/end or finished during the attempt.
- Judge: only for scenarios with `judge`, only when `bench.judge` is enabled + configured; grade
  stored in `judge_score`, never in `score`.

## Verified

- 95+ backend tests: every check type, fake world (commands, API rules, MCP, disk tree, fake
  Prometheus, rebase of timestamps), end-to-end runs with a scripted LLM (report / chat / JSON
  protocol / timeout / error / cancel / delete / export / restart recovery / private snapshot / judge).
- Real run of the 3 smoke scenarios x2 twice against `gemma4:e4b` on Ollama 0.40.16
  (8.0B, Q4_K_M, 131072 ctx): metadata, scores, traces, compare view, at 1280 and 375 px.

## Not verified / known gaps

- Only the smoke scenarios were run for real; the scenarios worker's 25 were loaded (catalog) but
  not executed. First full run may expose scenarios whose world rules do not match what the model asks.
- `prom_plan` was tested with a scripted LLM only, not against the real model.
- Judge was tested with a mock endpoint only.
- Private snapshot capture was tested through the API; the "run a scenario on it" path needs a
  scenario file with `snapshot: "private:<name>"` (none ships).
- Compare view at 375 px scrolls horizontally inside its panel (sticky first column).
- The app's `__version__` reads 0.2.0 while pyproject says 0.5.0 (pre-existing).
- During tests an unrelated `prune` job logs "cannot start a transaction within a transaction" when
  it overlaps another writer on the shared SQLite connection (pre-existing hazard, not caused by the benchmarks).
