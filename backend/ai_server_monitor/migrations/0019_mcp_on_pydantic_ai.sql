-- 0.8: MCP tools are given to the model directly, always. The token budget above which the chat
-- got a generic list/call pair (`mcp.max_inline_tool_tokens`) is gone; remove it from the stored
-- settings and their backups. (A policy `off` saved before is mapped when the settings load:
-- see McpServerSettings._policy_off_is_gone.)

UPDATE kv
SET value = json_remove(value, '$.mcp.max_inline_tool_tokens')
WHERE (key = 'settings' OR key LIKE 'settings.backup.%')
  AND json_valid(value)
  AND json_type(value, '$.mcp.max_inline_tool_tokens') IS NOT NULL;
