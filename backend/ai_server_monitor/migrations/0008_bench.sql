-- Model benchmarks: runs, their attempts, and snapshots captured from this server.
-- Runs are kept until deleted in the UI (never pruned).

CREATE TABLE bench_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started REAL NOT NULL,
    finished REAL,
    status TEXT NOT NULL,            -- running | done | cancelled | failed
    error TEXT,
    scope TEXT NOT NULL,             -- all | group:<name> | ids
    repeats INTEGER NOT NULL,
    scenario_hash TEXT,
    app_version TEXT,
    model TEXT,
    base_url TEXT,
    backend_kind TEXT,
    backend_version TEXT,
    params TEXT,                     -- parameter size, e.g. "7.9B"
    quantization TEXT,
    context_length INTEGER,
    overall REAL,                    -- 0-1: mean of the group scores
    group_scores TEXT,               -- JSON {group: score}
    summary TEXT,                    -- JSON: medians, pass rate, failures, per-scenario scores
    meta TEXT                        -- JSON: effective LLM settings, backend probe, GPU, judge
);
CREATE INDEX idx_bench_runs_started ON bench_runs(started);

CREATE TABLE bench_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES bench_runs(id) ON DELETE CASCADE,
    scenario_id TEXT NOT NULL,
    grp TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    started REAL NOT NULL,
    status TEXT NOT NULL,            -- ok | error | timeout | cancelled
    score REAL,                      -- 0-1, weighted share of passed checks
    checks TEXT,                     -- JSON [{type, describe, weight, passed, reason}]
    answer TEXT,
    error TEXT,
    ttft_ms INTEGER,
    total_ms INTEGER,
    tokens_in INTEGER,
    tokens_out INTEGER,
    tokens_per_s REAL,
    llm_calls INTEGER,
    tool_calls INTEGER,
    tool_mode TEXT,
    report_overlap INTEGER NOT NULL DEFAULT 0,
    trace_ids TEXT,                  -- JSON [llm_calls.id]
    detail TEXT,                     -- JSON: tool calls, approvals, report/plan details
    judge_score INTEGER,             -- 1-5 from the judge model (never part of `score`)
    judge_reason TEXT
);
CREATE INDEX idx_bench_attempts_run ON bench_attempts(run_id, scenario_id, attempt);

CREATE TABLE bench_snapshots (
    name TEXT PRIMARY KEY,
    created REAL NOT NULL,
    description TEXT,
    doc TEXT NOT NULL                -- JSON snapshot document (bench/schema.py Snapshot)
);
