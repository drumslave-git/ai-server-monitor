-- 0.8: MCP tools are sorted into domains (an area a person asks about: an application, a
-- device, a service). The chat gets the tools of the domains a request needs; a job gets the
-- tools of its domains. Domains exist only where tools are: none is predefined.

CREATE TABLE mcp_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    set_by TEXT NOT NULL DEFAULT 'model' CHECK (set_by IN ('model', 'owner')),
    updated_ts REAL NOT NULL
);

-- Every tool of every configured server as it was last listed, and its domain.
CREATE TABLE mcp_tools (
    server TEXT NOT NULL,
    tool TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    read_only INTEGER,                -- the server's readOnlyHint (NULL: not given)
    domain_id INTEGER REFERENCES mcp_domains(id) ON DELETE SET NULL,
    set_by TEXT NOT NULL DEFAULT 'model' CHECK (set_by IN ('model', 'owner')),
    sort_error TEXT,                  -- why the last sorting could not place it
    seen_ts REAL NOT NULL,
    PRIMARY KEY (server, tool)
);
CREATE INDEX idx_mcp_tools_domain ON mcp_tools(domain_id);

-- The last attempt to list each server's tools (the MCP tools page shows the server's state).
CREATE TABLE mcp_listings (
    server TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    error TEXT                        -- NULL: listed
);

-- The domains of a job (its runs get exactly their enabled tools).
CREATE TABLE job_domains (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES mcp_domains(id),
    PRIMARY KEY (job_id, domain_id)
);
