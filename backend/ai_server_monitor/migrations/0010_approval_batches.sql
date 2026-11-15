-- Approval batches: one approval (one web card, one Telegram message) groups the item
-- requests of one action type from one run (an automation run, or one chat turn). Every
-- item stays its own row in the permanent `actions` audit table, linked by `batch_id`;
-- its own status says what happened to it.

CREATE TABLE approval_batches (
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    updated    REAL NOT NULL,
    action     TEXT NOT NULL,
    label      TEXT,               -- "Stalled cleanup", "Chat", ...
    source     TEXT NOT NULL,
    run_key    TEXT,               -- the run / chat turn the items came from
    expires_at REAL
);
CREATE INDEX ix_approval_batches_key ON approval_batches (action, source, run_key);

ALTER TABLE actions ADD COLUMN batch_id INTEGER;
CREATE INDEX ix_actions_batch ON actions (batch_id);

-- Existing approvals that are still pending (or that have a Telegram message) are wrapped
-- into batches, so nothing is lost on upgrade. The run is not known for old rows: items of
-- the same action and source requested within the same minute are taken as one run.
INSERT INTO approval_batches (ts, updated, action, label, source, run_key, expires_at)
SELECT MIN(ts), MAX(updated), action,
       CASE
           WHEN source LIKE 'chat:%' OR source LIKE 'telegram:%' THEN 'Chat'
           WHEN MIN(policy_key) = 'stalled_cleanup' THEN 'Stalled cleanup'
           WHEN MIN(policy_key) = 'missing_search' THEN 'Missing search'
           WHEN MIN(policy_key) = 'cutoff_search' THEN 'Cutoff search'
           WHEN MIN(policy_key) = 'indexer_triage' THEN 'Indexer triage'
           ELSE NULL
       END,
       source,
       'migrated:' || CAST(ts / 60 AS INTEGER),
       MIN(expires_at)
FROM actions
WHERE status = 'pending' OR id IN (SELECT approval_id FROM telegram_approval_messages)
GROUP BY action, source, CAST(ts / 60 AS INTEGER);

UPDATE actions
SET batch_id = (
    SELECT b.id FROM approval_batches b
    WHERE b.action = actions.action AND b.source = actions.source
      AND b.run_key = 'migrated:' || CAST(actions.ts / 60 AS INTEGER)
)
WHERE status = 'pending' OR id IN (SELECT approval_id FROM telegram_approval_messages);

-- The Telegram message of an approval now belongs to the batch.
CREATE TABLE telegram_batch_messages (
    batch_id   INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_ts REAL NOT NULL,
    PRIMARY KEY (batch_id, chat_id)
);
INSERT OR IGNORE INTO telegram_batch_messages (batch_id, chat_id, message_id, created_ts)
SELECT a.batch_id, m.chat_id, m.message_id, m.created_ts
FROM telegram_approval_messages m JOIN actions a ON a.id = m.approval_id
WHERE a.batch_id IS NOT NULL;
DROP TABLE telegram_approval_messages;
