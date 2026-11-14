-- Report engine: one row per report attempt.
--   status:       ok | failed | skipped   (failed/skipped rows carry the reason in `error`;
--                 identical consecutive failures update the last row's ts instead of adding rows)
--   summary:      one sentence from the model header (or derived)
--   digest_hash:  fingerprint of the digest (volatile numbers rounded), for skip_if_unchanged
--   parsed:       1 if the model's JSON header was understood, 0 if the rule-based fallback was used

ALTER TABLE reports ADD COLUMN summary TEXT;
ALTER TABLE reports ADD COLUMN status TEXT NOT NULL DEFAULT 'ok';
ALTER TABLE reports ADD COLUMN digest_hash TEXT;
ALTER TABLE reports ADD COLUMN parsed INTEGER NOT NULL DEFAULT 1;
