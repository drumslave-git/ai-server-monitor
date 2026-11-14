-- Action layer: the audit table itself exists since 0001. This adds which policy
-- group an action counts against (for per-hour caps) and indexes for the caps and
-- the approval expiry sweep.

ALTER TABLE actions ADD COLUMN policy_key TEXT;

CREATE INDEX ix_actions_policy_key_ts ON actions (policy_key, ts);
CREATE INDEX ix_actions_status_expires ON actions (status, expires_at);
