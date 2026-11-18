-- Chat runs that stopped at calls waiting for approval (a changing shell command, an MCP
-- write). When every action of a run is decided, the run is resumed and the chat continues.

CREATE TABLE chat_pending_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    source TEXT,                      -- who asked: chat:<session> or telegram:<chat id>
    state TEXT NOT NULL               -- JSON: the stopped run (messages, settled and pending calls)
);

CREATE TABLE chat_pending_actions (
    action_id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES chat_pending_runs(id) ON DELETE CASCADE,
    call_id TEXT NOT NULL
);
CREATE INDEX idx_chat_pending_actions_run ON chat_pending_actions(run_id);
