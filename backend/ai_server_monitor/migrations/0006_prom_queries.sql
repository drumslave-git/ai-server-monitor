-- PromQL queries chosen by the planner (source auto/fallback). They are data, not settings:
-- the planner creates and replaces them, the user can disable, pin, edit or delete them.
-- The plan bookkeeping (catalog hash, last plan time, dismissed names) lives in `kv`.

CREATE TABLE prom_queries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    query           TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    unit            TEXT NOT NULL DEFAULT '',
    kind            TEXT NOT NULL DEFAULT 'value',   -- value | alert (empty result is healthy)
    warn_if         TEXT,
    crit_if         TEXT,
    source          TEXT NOT NULL DEFAULT 'auto',    -- auto | fallback
    job             TEXT NOT NULL DEFAULT '',        -- Prometheus job it was planned for
    enabled         INTEGER NOT NULL DEFAULT 1,
    pinned          INTEGER NOT NULL DEFAULT 0,
    edited          INTEGER NOT NULL DEFAULT 0,
    fail_count      INTEGER NOT NULL DEFAULT 0,      -- consecutive failures
    disabled_reason TEXT,
    last_ok_ts      REAL,
    last_error      TEXT,
    last_error_ts   REAL,
    last_value      TEXT,                            -- JSON: compact copy of the last result
    created_ts      REAL NOT NULL,
    updated_ts      REAL NOT NULL
);
