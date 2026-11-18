-- Scheduled LLM jobs: topic checks on the worker model and a summary on the main model.
-- `jobs` is the source of truth for the scheduler; builtins are seeded from code.

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('topic', 'summary')),
    prompt TEXT NOT NULL,
    interval_s INTEGER,               -- every N seconds, or
    cron TEXT,                        -- a crontab expression (exactly one of the two is set)
    model TEXT NOT NULL CHECK (model IN ('worker', 'main')),
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    edited INTEGER NOT NULL DEFAULT 0, -- a builtin the user changed: no longer follows releases
    timeout_s INTEGER,                -- NULL = Settings → Jobs default
    max_steps INTEGER,                -- NULL = Settings → Jobs default
    created_by TEXT NOT NULL CHECK (created_by IN ('builtin', 'user', 'llm')),
    created REAL NOT NULL,
    updated REAL NOT NULL,
    CHECK ((interval_s IS NULL) <> (cron IS NULL))
);

CREATE TABLE job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_slug TEXT NOT NULL REFERENCES jobs(slug) ON DELETE CASCADE ON UPDATE CASCADE,
    trigger TEXT NOT NULL CHECK (trigger IN ('schedule', 'manual', 'llm')),
    started REAL NOT NULL,
    finished REAL,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'warn', 'crit', 'error')),
    summary TEXT,                     -- markdown
    findings TEXT,                    -- JSON list of {severity, title, detail}
    error TEXT,
    model TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    steps INTEGER,
    trace_turn TEXT                   -- the run's LLM and tool calls on the Logs page
);
CREATE INDEX idx_job_runs_job ON job_runs(job_slug, started);
CREATE INDEX idx_job_runs_started ON job_runs(started);
