-- 0.10.0: summaries run an ordered queue of jobs.
--
-- * jobs.notify: when a summary or a detached job sends its result to Telegram
--   (always | warn_crit | never). A job inside a summary sends nothing itself.
-- * jobs.retired: an old builtin this release replaced. Hidden from the job list and the
--   scheduler; its runs stay readable (history, run detail).
-- * A job in a summary has no own schedule, so interval_s and cron may both be empty now.
-- * summary_members: the ordered jobs of a summary (a job may be in several summaries).
-- * job_runs.queue_run_id: the summary run a job run was part of.
-- * job_runs no longer references jobs: deleting a job deletes its runs in code, and retiring
--   one keeps them.
--
-- The tables are rebuilt (SQLite cannot change a CHECK or drop a foreign key in place).
-- Foreign keys are on during migrations, and dropping `jobs` would delete the rows that
-- reference it, so job_runs and job_domains are moved aside first.

CREATE TABLE job_domains_keep AS SELECT job_id, domain_id FROM job_domains;
DROP TABLE job_domains;

CREATE TABLE job_runs_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_slug TEXT NOT NULL,
    queue_run_id INTEGER,             -- the summary run this run was part of (NULL: alone)
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
    trace_turn TEXT,                  -- the run's LLM and tool calls on the Logs page
    topics TEXT,
    mcp TEXT
);
INSERT INTO job_runs_new (
    id, job_slug, trigger, started, finished, status, summary, findings, error, model,
    prompt_tokens, completion_tokens, steps, trace_turn, topics, mcp
)
SELECT
    id, job_slug, trigger, started, finished, status, summary, findings, error, model,
    prompt_tokens, completion_tokens, steps, trace_turn, topics, mcp
FROM job_runs;
DROP TABLE job_runs;
ALTER TABLE job_runs_new RENAME TO job_runs;
CREATE INDEX idx_job_runs_job ON job_runs(job_slug, started);
CREATE INDEX idx_job_runs_started ON job_runs(started);
CREATE INDEX idx_job_runs_queue ON job_runs(queue_run_id);

CREATE TABLE jobs_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('topic', 'summary')),
    prompt TEXT NOT NULL,
    interval_s INTEGER,               -- every N seconds, or
    cron TEXT,                        -- a crontab expression, or neither (only in a summary,
                                      -- or run by hand)
    model TEXT NOT NULL CHECK (model IN ('worker', 'main')),
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    edited INTEGER NOT NULL DEFAULT 0, -- a builtin the user changed: no longer follows releases
    timeout_s INTEGER,                -- NULL = Settings → Jobs default
    max_steps INTEGER,                -- NULL = Settings → Jobs default
    created_by TEXT NOT NULL CHECK (created_by IN ('builtin', 'user', 'llm')),
    created REAL NOT NULL,
    updated REAL NOT NULL,
    notify TEXT NOT NULL DEFAULT 'never' CHECK (notify IN ('always', 'warn_crit', 'never')),
    retired INTEGER NOT NULL DEFAULT 0,
    CHECK (interval_s IS NULL OR cron IS NULL)
);
INSERT INTO jobs_new (
    id, slug, name, kind, prompt, interval_s, cron, model, enabled, builtin, edited,
    timeout_s, max_steps, created_by, created, updated
)
SELECT
    id, slug, name, kind, prompt, interval_s, cron, model, enabled, builtin, edited,
    timeout_s, max_steps, created_by, created, updated
FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

CREATE TABLE job_domains (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES mcp_domains(id),
    PRIMARY KEY (job_id, domain_id)
);
INSERT INTO job_domains (job_id, domain_id) SELECT job_id, domain_id FROM job_domains_keep;
DROP TABLE job_domains_keep;

CREATE TABLE summary_members (
    summary_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (summary_id, job_id)
);
CREATE INDEX idx_summary_members_job ON summary_members(job_id);

-- The builtins of 0.9.1 are replaced by smaller ones (docker:status, docker:updates, ...) and
-- the summaries Health and Daily. An unedited old builtin is retired. An edited one stays as
-- the owner's own job with its schedule; an edited old summary stays as an own summary,
-- disabled and without members (the owner adds them). Notify 'never' keeps 0.9.1's behaviour
-- for kept jobs (only the summary sent to Telegram).
UPDATE jobs SET retired = 1, enabled = 0
WHERE builtin = 1 AND edited = 0
  AND slug IN ('docker', 'journal', 'resources', 'storage', 'systemd', 'updates', 'summary');
UPDATE jobs SET enabled = 0
WHERE builtin = 1 AND edited = 1 AND slug = 'summary';
UPDATE jobs SET builtin = 0, edited = 0, created_by = 'user'
WHERE builtin = 1 AND edited = 1
  AND slug IN ('docker', 'journal', 'resources', 'storage', 'systemd', 'updates', 'summary');
