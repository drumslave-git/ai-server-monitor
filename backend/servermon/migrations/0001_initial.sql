-- Initial schema. Timestamps are unix epoch seconds (REAL). JSON stored as TEXT.

CREATE TABLE collector_samples (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    collector   TEXT    NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    data        TEXT    NOT NULL,
    error       TEXT
);
CREATE INDEX ix_samples_collector_ts ON collector_samples (collector, ts);
CREATE INDEX ix_samples_ts ON collector_samples (ts);

CREATE TABLE reports (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    health      TEXT,               -- ok | warn | crit | unknown
    score       INTEGER,            -- 0..100
    findings    TEXT,               -- JSON array
    markdown    TEXT    NOT NULL,
    digest      TEXT,               -- the input the LLM saw
    model       TEXT,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    latency_ms  INTEGER,
    error       TEXT
);
CREATE INDEX ix_reports_ts ON reports (ts);

CREATE TABLE chat_sessions (
    id          TEXT    PRIMARY KEY,
    created     REAL    NOT NULL,
    updated     REAL    NOT NULL,
    channel     TEXT    NOT NULL,   -- web | telegram
    title       TEXT
);
CREATE INDEX ix_chat_sessions_updated ON chat_sessions (updated);

CREATE TABLE chat_messages (
    id           INTEGER PRIMARY KEY,
    session_id   TEXT    NOT NULL REFERENCES chat_sessions (id) ON DELETE CASCADE,
    ts           REAL    NOT NULL,
    role         TEXT    NOT NULL,  -- system | user | assistant | tool
    content      TEXT,
    tool_calls   TEXT,              -- JSON
    tool_call_id TEXT,
    name         TEXT
);
CREATE INDEX ix_chat_messages_session ON chat_messages (session_id, id);

-- Permanent audit log of *arr / qBittorrent actions. Never pruned.
CREATE TABLE actions (
    id          INTEGER PRIMARY KEY,
    ts          REAL    NOT NULL,
    updated     REAL    NOT NULL,
    action      TEXT    NOT NULL,
    target      TEXT,
    args        TEXT,               -- JSON
    policy      TEXT    NOT NULL,
    status      TEXT    NOT NULL,   -- suggested|pending|approved|rejected|expired|executed|failed|dry_run
    source      TEXT    NOT NULL,   -- scheduler | report | chat:<session>
    reason      TEXT,
    result      TEXT,               -- JSON
    decided_by  TEXT,
    expires_at  REAL
);
CREATE INDEX ix_actions_ts ON actions (ts);
CREATE INDEX ix_actions_status ON actions (status);

-- Small persistent key/value state (e.g. probed LLM tool mode).
CREATE TABLE kv (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,   -- JSON
    updated     REAL    NOT NULL
);
