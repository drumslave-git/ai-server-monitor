-- 0.11: memory. Notes the main model writes (in the chat, and in a summary run that uses the
-- main model) and keeps across conversations; jobs read the notes of their area, the owner
-- edits and deletes them on the Memory page. Nothing prunes them: not retention, not age.

CREATE TABLE memory_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL UNIQUE COLLATE NOCASE,
    body TEXT NOT NULL,
    -- 'model': the main model wrote it and may change or delete it; 'owner': the owner edited
    -- it on the Memory page, the model can only read it.
    author TEXT NOT NULL DEFAULT 'model' CHECK (author IN ('model', 'owner')),
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    -- What the model last wrote it in: a chat session (source_id = its id) or a job run
    -- (source_id = the run id, source_label = the job's slug). Not foreign keys: runs and
    -- sessions are pruned or deleted, the note stays.
    source_kind TEXT CHECK (source_kind IN ('chat', 'run')),
    source_id TEXT,
    source_label TEXT
);

-- The area of a note: MCP domains, the same ones a job has (a note without one is for the chat
-- and for summary runs of the main model only).
CREATE TABLE memory_note_domains (
    note_id INTEGER NOT NULL REFERENCES memory_notes(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES mcp_domains(id) ON DELETE CASCADE,
    PRIMARY KEY (note_id, domain_id)
);
CREATE INDEX idx_memory_note_domains_domain ON memory_note_domains(domain_id);
