-- 0.8: MCP tools are sorted into domains (an area a person asks about: an application, a device,
-- a part of the host). The chat loads the tools of the domains a request needs; a job is given
-- the tools of its domains. Six built-in domains, one per built-in topic job, exist from the
-- start; `key` is how built-in jobs and later releases refer to them (the owner may rename them,
-- not delete them).

CREATE TABLE mcp_domains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE,                  -- stable key of a built-in domain, NULL for the others
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    set_by TEXT NOT NULL DEFAULT 'model' CHECK (set_by IN ('builtin', 'model', 'owner')),
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

INSERT INTO mcp_domains (key, name, description, set_by, updated_ts) VALUES
    ('storage', 'storage', 'Disks, filesystems, mounts, SMART health, RAID and free space', 'builtin', 0),
    ('journal', 'journal', 'System logs: the journal, kernel messages and log errors', 'builtin', 0),
    ('resources', 'resources', 'CPU, memory, load, temperatures and GPU usage', 'builtin', 0),
    ('docker', 'docker', 'Docker containers, images, volumes, networks and their health', 'builtin', 0),
    ('systemd', 'systemd', 'Systemd services, timers and failed units', 'builtin', 0),
    ('updates', 'updates', 'Package updates, security updates and pending reboots', 'builtin', 0);
