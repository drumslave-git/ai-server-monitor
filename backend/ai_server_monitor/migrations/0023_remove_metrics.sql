-- The app no longer exposes /metrics (the owner reaches Prometheus through an MCP server):
-- its two settings, outputs.metrics.enabled and outputs.metrics.bearer_token, are removed from
-- the stored settings (live and the backups `settings.backup.<ts>`).

UPDATE kv
SET value = json_remove(value, '$.outputs.metrics')
WHERE (key = 'settings' OR key LIKE 'settings.backup.%')
  AND json_valid(value)
  AND json_type(value) = 'object';
