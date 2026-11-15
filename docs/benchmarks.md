# Benchmarks: picking the best model for this monitor

The Benchmarks page runs a fixed set of **frozen scenarios** through the monitor's real
code (report engine, chat engine with its real prompts and tool schemas, Prometheus
planner) against the **LLM configured in Settings → LLM**, and scores speed and accuracy.
To compare models, switch the setting, run again, and tick the runs in the table.

Everything a scenario shows the model is frozen: collector data from a *snapshot*, tool
answers from a *world*. No host command, Docker, *arr, MCP server or Prometheus is ever
contacted; the only live connection is the LLM. Writes go through the real action layer, but
against a throw-away in-memory database in dry-run, so approvals are observable and nothing
can happen.

Authoritative definition: `backend/ai_server_monitor/bench/schema.py` (pydantic models). A
file that does not load there is not a scenario. Validate with

```
python -m ai_server_monitor.bench.schema
```

## Files

```
backend/ai_server_monitor/bench/
  scenarios/<group>/<name>.json     one scenario; its id is "<group>.<name>"
  scenarios/_smoke/…                the app's own test scenarios (hidden from the catalog)
  snapshots/<name>.json             frozen server data, shared by many scenarios
```

Groups: `report` (status reports), `chat` (chat with tools), `prom_plan` (Prometheus
planning), `robust` (robustness: protocol fallbacks, hostile input, refusals; any input kind).
The id must start with the group, and the file must sit in `<group>/<name>.json` with the id
`<group>.<name>`.

## Scenario

```jsonc
{
  "id": "chat.missing_movies",            // <group>.<name>, lowercase, digits, _
  "group": "chat",                        // report | chat | prom_plan | robust
  "title": "Which movies are missing?",
  "description": "What it tests and why (shown in the catalog).",
  "snapshot": "base",                     // snapshots/base.json, or "private:<name>"
  "patch": { "collectors": { "system": { "data": { "memory": { "percent": 96 } } } } },
  "input": { "kind": "chat", "messages": [ { "role": "user", "content": "…" } ] },
  "tool_mode": "auto",                    // chat only: auto | native | json
  "world": { … },                         // chat only, see below
  "settings": { },                        // optional merge-patch over the benchmark Settings
  "checks": [ { "type": "tool_called", "tool": "api_call", "weight": 2 } ],
  "judge": { "rubric": "what a 5/5 answer contains" },   // optional
  "tags": ["tools"],                      // optional
  "timeout_s": 300                        // optional per-attempt budget
}
```

Unknown fields are rejected (typos fail loudly).

### Snapshot and patch

A snapshot is one JSON document:

```jsonc
{
  "name": "base",
  "description": "…",
  "now": 1780315200,                       // optional: the moment the run pretends it is
  "collectors": {                          // keyed by collector name
    "system":  { "ok": true, "state": "ok", "age_s": 5, "data": { …collector output… } },
    "docker":  { "data": { … } },
    "gpu": …, "systemd": …, "journal": …, "apt": …, "image_updates": …, "prometheus": …, "arr": …
  },
  "history": {                             // optional: older samples, for trends
    "system": [ { "hours_ago": 72, "data": { …same shape as the collector's data… } } ]
  },
  "settings": { }                          // optional merge-patch over the benchmark Settings
}
```

`data` has exactly the shape the real collector produces (see `docs/samples/collectors-live.json`
for the real output of every collector). Absent collectors count as disabled; a collector can
also be given as `{"ok": false, "error": "…"}` or `{"state": "unavailable", "reason": "…"}`.
`now` is the pretend current time (epoch seconds): the digest is built with it, so timestamps
inside the data (`since`, `started_at`) read relative to it, and each collector's `age_s` counts
back from it. Leave it out to use the real time.
`history` lets the digest compute trends (disk growth, restart counts); without it there are none.

`patch` is a **JSON merge-patch (RFC 7386)** applied to the whole document, so scenarios stay
small: objects merge recursively, **arrays are replaced whole**, `null` deletes a key. Patch a
filesystem by replacing the `filesystems` array, not an element of it.

`snapshot: "private:<name>"` runs on a snapshot captured from the live server (Benchmarks →
"Capture this server"); it lives in the database, never in the repo.

### Input

| `input.kind` | What runs | The model's output that is scored |
|---|---|---|
| `report` | the report engine: digest of the snapshot → one LLM call → parsed report | the parsed report (health, score, findings) and its Markdown |
| `chat` | the chat engine with the real system prompt and tool schemas; `messages` is the conversation, the last message is the user's question | tool calls made, the final answer |
| `prom_plan` | the Prometheus planner against a fake Prometheus built from `catalog` | the queries the planner accepted, and the model's first reply per job |

`chat.messages` items are `{"role": "user"|"assistant", "content": "…"}`; earlier ones are
history. `prom_plan.catalog`:

```jsonc
{ "jobs": [ { "job": "node",
              "targets": [ { "instance": "host:9100", "health": "up" } ],
              "metrics": { "node_load5": { "type": "gauge", "help": "5m load", "series": 1, "value": 0.4 },
                           "node_netstat_Tcp_RetransSegs": { "type": "counter", "series": 1, "value": 1234 } } } ],
  "queries": [ { "match": "regex on the query text", "values": [1], "error": null } ] }  // optional overrides
```

The planner reads it through its real HTTP client. A query mentioning a metric of the job
answers with that metric's `value` (`series` copies, unless it aggregates); `queries`
override that (`error` makes Prometheus reject it).

### `tool_mode` (chat)

How tools are offered to the model. `auto` uses what the configured LLM resolves to (probed
and cached like in the app); `native` forces OpenAI tool calls; `json` forces the JSON-in-text
protocol (`{"tool": …, "args": …}`). The mode actually used is stored with each attempt.

### World (chat): what the tools answer

Tools are the real ones (names, schemas, descriptions); only their executors are swapped.
Anything no rule answers returns `world.default` as an error.

```jsonc
"world": {
  "commands": [ { "match": "journalctl .*logrotate", "exit_code": 0, "output": "…", "stderr": "" } ],
  "api": [ { "service": "radarr", "method": "GET", "path": "/api/v3/movie\\?.*",
             "status": 200, "response": [ { "title": "…" } ] } ],
  "disk": { "roots": [ { "path": "/mnt/disks/disk14TB", "size_bytes": 14000000000000,
                         "children": [ { "name": "movies", "size_bytes": 9000000000000,
                                         "owner": "radarr library", "children": [] } ] } ],
            "files": [ { "path": "/mnt/x/big.mkv", "size_bytes": 80000000000 } ] },
  "mcp": [ { "server": "notes", "tool": "search_notes", "description": "…",
             "input_schema": { "type": "object", "properties": { "q": { "type": "string" } } },
             "match": { "q": "backup" }, "result": { "hits": [] } } ],
  "default": "error: not available in this scenario"
}
```

- **commands**: `match` is a regex `re.search`ed against the model's `cmd`; first match wins.
  The real command guard still refuses unsafe commands (`rm`, `;`, `>` …), exactly as in the app.
- **api**: used by `api_call` / `api_endpoints`. `service` is the service name or an alias
  (`movies`, `series`, `all_arr`); `path` is a regex matched against path + query. First match wins.
  A rule can return raw text instead of JSON: `"response_text": "..."` is served as bytes, with
  `"multiplexed": true` wrapping every line in a Docker log frame. This is what the chat tool
  `container_logs` reads (service `docker`, path regex like `/containers/qbittorrent/logs`; the
  tool asks for timestamps, so put an RFC 3339 stamp at the start of each line if the scenario
  needs one). The tool first lists the containers, so add a rule for `/containers/json` that names
  them. `response` and `response_text` cannot both be set.
- **disk**: `disk_usage` answers from the tree, cut at the requested depth; `top_files` lists
  from `files`. Sizes are bytes.
- **mcp**: each rule is one tool of a fake MCP server and is offered to the model with its
  `description` / `input_schema`. Rules of one tool are tried in order; `match` (argument →
  regex) selects by arguments. `error` makes the call fail. `write: true` marks a tool that
  changes things (needs approval, never runs).
- Other tools work off the snapshot (`get_status`, `get_collector_data`, `metric_history`,
  `container_inspect`, `get_arr_overview`), the in-memory report store and the dry-run action layer
  (`list_pending_approvals`, `list_recent_actions` and the write tools).

The benchmark profile configures a fake Radarr, Sonarr, qBittorrent, Prowlarr, Prometheus and
shell so that all tools exist, with every write action set to *approval required*. A scenario's
or snapshot's `settings` merge-patch can change that (e.g. turn a policy `off`).

## Checks

Each check is `{"type": "…", "weight": 1, …}` (weight default 1; optional `note` explains the
check in the UI). Every check returns pass/fail and a short reason, shown in the run detail.
**Scenario score = weighted share of passed checks (0–1); group score = mean of its scenarios;
overall = mean of the groups that ran.** An attempt that fails to produce an answer at all (LLM
error, timeout) scores 0. With N attempts per scenario the scenario score is the mean.

| type | parameters | passes when |
|---|---|---|
| `tool_called` | `tool`, `args_match?` (`{arg: regex}`) | the tool was called with arguments that match (regexes `re.search`ed, case-insensitive, against each argument's text). `tool` may be `a\|b` for any of several |
| `tool_not_called` | `tool` (`a\|b` allowed) | it was never called |
| `tool_order` | `tools: [a, b, …]` | the tools were called in this order (other calls may sit between) |
| `max_tool_calls` | `n` | at most `n` tool calls in total |
| `answer_contains_all` | `values`, `case_insensitive=true` | every value occurs in the answer (report: its Markdown). Plain text; thousands separators and trailing `.0` in numbers are ignored |
| `answer_contains_any` | `values`, `case_insensitive=true` | at least one value occurs |
| `answer_not_contains` | `patterns` (regex, case-insensitive) | none matches |
| `approval_requested` | `action` | the action layer holds a pending approval (or suggestion) for that write action |
| `no_write_executed` | – | no write action reached `executed` |
| `no_write_claimed` | `patterns?` | the answer does not claim something was done ("I have removed…", "successfully deleted") when only an approval was requested or nothing happened; heuristic phrase match, `patterns` adds scenario-specific claims |
| `report_json_valid` | – | the engine parsed the report's JSON header (not the rule-based fallback) |
| `report_health` | `equals` or `in: […]` (`ok`/`warn`/`crit`) | health matches |
| `report_score_range` | `min`, `max` | the score is in range |
| `report_findings_mention` | `patterns`, `min=1` | at least `min` patterns (regex) occur in the findings' titles/details |
| `report_no_invented` | `patterns` | none occurs in the findings: things that are not in the data |
| `prom_queries_valid` | `min` | at least `min` queries survive the planner's validation (final accepted set) |
| `prom_counters_rated` | – | no query uses a counter bare (no `rate()`/`increase()`); judged on the model's **first reply per job**, before the planner's repair round |
| `prom_job_scoped` | – | every query selects its job (`job="x"`); first reply per job |
| `json_fallback_used` | `max_repairs=0` | with the JSON protocol at least one tool call was parsed from the text, with at most `max_repairs` repair prompts |

Check kinds per input: tool, approval, write and `json_fallback_used` checks need a `chat`
input; `report_*` need `report`; `prom_*` need `prom_plan`; `answer_*` work on `chat` and
`report`. The loader rejects a mismatch. Robust scenarios may use any input.

Guidance for writing checks: score what a human owner would call right or wrong, not wording.
Prefer a few robust values (`"logrotate"`) over phrases; give the decisive checks a higher
weight; every scenario needs at least one check that an obviously bad answer fails.

## Judge (optional)

`judge: {"rubric": "…"}` makes the scenario eligible for the judge model: Settings →
Benchmarks → judge (`base_url`, `model`, `api_key`, enabled; off by default) grades each
answer 1–5 against the rubric. The grade is shown **next to** the rule score and never mixed
into it, because a judge is itself a model.

## Running

- One run at a time, in the background; progress, cancel and a per-attempt time budget.
- **Cold start:** every run begins with a warm-up request (the real chat system prompt plus "Reply with OK",
  no tools, a few tokens; one retry after a long timeout) so a freshly switched model is loaded before
  anything is measured. Its time, first-token time and, on Ollama (`/api/ps`), whether the model was
  already loaded and its VRAM size are stored as the run's cold start. It is left out of every score,
  median, pass rate and speed. Its LLM call appears in Logs as a benchmark call linked `<run>/warmup/0`.
- **Reload suspected:** an attempt is flagged (never re-run, never hidden) when Ollama's `/api/ps` shows
  the model gone just before it, or when its first token took more than 5× the run's median (and at
  least 2 s, with at least 4 timed attempts in the run).
- Every scenario runs N times (default 3); the app's own reports keep running meanwhile, so
  timings may be slower while a report is being written. Each attempt records whether a report
  overlapped it.
- Stored per attempt: check results, score, time to first token, total time, tokens in/out,
  tokens/s, number of LLM steps and tool calls, tool mode used, errors, the answer, and the
  LLM trace ids (purpose `bench`; the Logs page shows them).
- Stored per run: model, backend base URL, backend kind and version (Ollama `/api/version` and
  `/api/show` → parameter size, quantization, context length, family; llama.cpp `/props`;
  otherwise the OpenAI-compatible `/v1/models` entry), the effective LLM settings (temperature,
  max output tokens, tool mode, context), app version, scenario-set hash (two runs with
  different hashes did not measure the same thing), start/finish times and a GPU snapshot.
- Runs are kept until deleted in the UI; a run can be exported as JSON.
