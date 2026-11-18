-- Three features are gone: the *arr (Radarr, Sonarr, Whisparr, Prowlarr) and qBittorrent
-- integration, the model benchmarks, and the generic HTTP API access of the chat (api_call).
-- What is left of them is removed or closed here:
--
-- 1. Stored settings: the sections `arr`, `collectors.arr` and `bench` are dropped (from the
--    live settings and from the backups `settings.backup.<ts>`, which would keep the API keys).
--    The approval timeout moves from `arr.actions.approval_ttl` to `approvals.approval_ttl`.
-- 2. The *arr collector's samples are deleted; the benchmark tables are dropped.
-- 3. Their actions stay in the permanent audit log as plain records; the ones still waiting
--    (suggested, pending, approved, queued) can never run now and are closed as expired.
-- 4. A pause of the automation switch held by a benchmark run is lifted.

UPDATE kv
SET value = json_set(
        value,
        '$.approvals',
        json_object('approval_ttl', json_extract(value, '$.arr.actions.approval_ttl'))
    )
WHERE key = 'settings'
  AND json_valid(value)
  AND json_type(value, '$.arr.actions.approval_ttl') = 'text'
  AND json_type(value, '$.approvals') IS NULL;

UPDATE kv
SET value = json_remove(value, '$.arr', '$.collectors.arr', '$.bench')
WHERE (key = 'settings' OR key LIKE 'settings.backup.%')
  AND json_valid(value)
  AND json_type(value) = 'object';

DELETE FROM kv
WHERE key = 'automation.pause'
  AND json_valid(value)
  AND json_extract(value, '$.by') LIKE 'bench #%';

DELETE FROM collector_samples WHERE collector = 'arr';

DROP TABLE IF EXISTS bench_hw_samples;
DROP TABLE IF EXISTS bench_attempts;
DROP TABLE IF EXISTS bench_snapshots;
DROP TABLE IF EXISTS bench_runs;

UPDATE actions
SET status = 'expired',
    updated = (julianday('now') - 2440587.5) * 86400.0,
    decided_by = COALESCE(decided_by, 'upgrade'),
    result = json_object('detail', 'this kind of action was removed from the app')
WHERE status IN ('suggested', 'pending', 'approved', 'queued')
  AND action IN (
      'qbit_delete_torrent', 'arr_remove_queue_item', 'arr_search', 'arr_add_media',
      'prowlarr_test_indexer', 'api_call'
  );
