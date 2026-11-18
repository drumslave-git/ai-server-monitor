-- 0.8: what a job run had of MCP: its domains at run time, servers that could not be reached,
-- enabled tools that had no domain (JSON object; NULL: the job has no domains).
ALTER TABLE job_runs ADD COLUMN mcp TEXT;
