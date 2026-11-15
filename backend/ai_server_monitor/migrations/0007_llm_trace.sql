-- LLM trace: every request to the model as sent and received, the tool calls made by the
-- chat, and the app log (WARNING and above). Large fields are zlib-compressed BLOBs.

CREATE TABLE llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,                 -- when the request was sent
    purpose TEXT NOT NULL,            -- report | chat | chat_title | probe | test | prom_plan ...
    link_kind TEXT,                   -- report | chat | ...
    link_id TEXT,
    link_step INTEGER,
    turn TEXT,                        -- one chat turn: its LLM calls and tool calls share it
    group_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    model TEXT,
    base_url TEXT,
    stream INTEGER NOT NULL DEFAULT 0,
    params TEXT,                      -- JSON: temperature, max_tokens, tool_choice, ...
    status TEXT NOT NULL,             -- ok | error | cancelled
    http_status INTEGER,
    error TEXT,
    finish_reason TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    usage_estimated INTEGER NOT NULL DEFAULT 0,
    queued_ms INTEGER,
    ttft_ms INTEGER,
    total_ms INTEGER,
    tokens_per_s REAL,
    messages_count INTEGER,
    tools_count INTEGER,
    tools_tokens INTEGER,
    request_bytes INTEGER,
    response_chars INTEGER,
    tool_calls_count INTEGER NOT NULL DEFAULT 0,
    request_preview TEXT,
    response_preview TEXT,
    messages BLOB,
    tools BLOB,
    content BLOB,
    reasoning BLOB,
    tool_calls BLOB,
    truncated TEXT,                   -- JSON list of fields that hit the size cap
    bodies INTEGER NOT NULL DEFAULT 1,
    size_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_llm_calls_ts ON llm_calls(ts);
CREATE INDEX idx_llm_calls_purpose ON llm_calls(purpose, ts);
CREATE INDEX idx_llm_calls_link ON llm_calls(link_kind, link_id, ts);
CREATE INDEX idx_llm_calls_turn ON llm_calls(turn);

CREATE TABLE llm_tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    link_kind TEXT,
    link_id TEXT,
    link_step INTEGER,
    turn TEXT,
    tool TEXT NOT NULL,
    ok INTEGER NOT NULL,
    duration_ms INTEGER,
    result_size INTEGER,
    error TEXT,
    args BLOB,
    result BLOB,
    truncated INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_llm_tool_calls_ts ON llm_tool_calls(ts);
CREATE INDEX idx_llm_tool_calls_turn ON llm_tool_calls(link_kind, link_id, turn);

CREATE TABLE app_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    levelno INTEGER NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    exc TEXT
);
CREATE INDEX idx_app_log_ts ON app_log(ts);
