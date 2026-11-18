-- Nothing is collected by code any more: the collectors, the digest and the reports built on
-- it, the Prometheus query planner, live metrics and the hand-written LLM client (with its
-- JSON-in-text and no-tools modes) are gone. What is left of them is removed here:
--
-- 1. Tables: collector samples, reports, the planner's Prometheus queries.
-- 2. Stored settings (live and the backups `settings.backup.<ts>`): the `report` and
--    `collectors` sections, the LLM knobs only the old client used (tool_mode,
--    max_context_tokens, stream_usage; main and worker model) and storage.sample_interval
--    (the Telegram report switches are mapped by 0016). Two values move first: report.language becomes
--    general.language, collectors.docker.url becomes general.docker_url (the shell's Docker).
-- 3. kv state of the removed parts: the tool-mode probe, the planner, Telegram report counters.

DROP TABLE IF EXISTS collector_samples;
DROP TABLE IF EXISTS reports;
DROP TABLE IF EXISTS prom_queries;

UPDATE kv
SET value = json_set(value, '$.general.language', json_extract(value, '$.report.language'))
WHERE key = 'settings'
  AND json_valid(value)
  AND json_type(value, '$.report.language') = 'text'
  AND json_type(value, '$.general') = 'object'
  AND json_type(value, '$.general.language') IS NULL;

UPDATE kv
SET value = json_set(
        value, '$.general.docker_url', json_extract(value, '$.collectors.docker.url')
    )
WHERE key = 'settings'
  AND json_valid(value)
  AND json_type(value, '$.collectors.docker.url') = 'text'
  AND json_extract(value, '$.collectors.docker.url') != ''
  AND json_type(value, '$.general') = 'object'
  AND json_type(value, '$.general.docker_url') IS NULL;

UPDATE kv
SET value = json_remove(
        value,
        '$.report',
        '$.collectors',
        '$.llm.tool_mode',
        '$.llm.max_context_tokens',
        '$.llm.stream_usage',
        '$.worker.tool_mode',
        '$.worker.max_context_tokens',
        '$.worker.stream_usage',
        '$.storage.sample_interval'
    )
WHERE (key = 'settings' OR key LIKE 'settings.backup.%')
  AND json_valid(value)
  AND json_type(value) = 'object';

DELETE FROM kv
WHERE key IN (
    'llm.tool_mode',
    'prom_plan',
    'prom_dismissed',
    'telegram.report_count',
    'telegram.last_health',
    'telegram.report_requests'
);
