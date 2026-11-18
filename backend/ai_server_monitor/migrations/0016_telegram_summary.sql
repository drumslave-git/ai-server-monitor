-- The Telegram report switches (`report_every`, `alerts`) give way to one setting for the
-- summary job's result, `outputs.telegram.send_summary` (changed | every | never). Mapped from
-- the old values in the stored settings and in the backups (`settings.backup.<ts>`):
--
--   report_every = 1                     -> every
--   report_every > 1                     -> changed
--   report_every = 0, alerts = false     -> never
--   report_every = 0, alerts otherwise   -> changed
--   no report_every, alerts = false      -> never
--   neither key                          -> nothing (the default, changed, applies)
--
-- Then both old keys are removed.

UPDATE kv
SET value = json_set(
        value,
        '$.outputs.telegram.send_summary',
        CASE
            WHEN json_type(value, '$.outputs.telegram.report_every') IN ('integer', 'real')
                 AND json_extract(value, '$.outputs.telegram.report_every') = 1 THEN 'every'
            WHEN json_type(value, '$.outputs.telegram.report_every') IN ('integer', 'real')
                 AND json_extract(value, '$.outputs.telegram.report_every') > 1 THEN 'changed'
            WHEN json_type(value, '$.outputs.telegram.alerts') = 'false' THEN 'never'
            ELSE 'changed'
        END
    )
WHERE (key = 'settings' OR key LIKE 'settings.backup.%')
  AND json_valid(value)
  AND json_type(value, '$.outputs.telegram') = 'object'
  AND json_type(value, '$.outputs.telegram.send_summary') IS NULL
  AND (
      json_type(value, '$.outputs.telegram.report_every') IS NOT NULL
      OR json_type(value, '$.outputs.telegram.alerts') IS NOT NULL
  );

UPDATE kv
SET value = json_remove(
        value, '$.outputs.telegram.report_every', '$.outputs.telegram.alerts'
    )
WHERE (key = 'settings' OR key LIKE 'settings.backup.%')
  AND json_valid(value)
  AND json_type(value, '$.outputs.telegram') = 'object';
