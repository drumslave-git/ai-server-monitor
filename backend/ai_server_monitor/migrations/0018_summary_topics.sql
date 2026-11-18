-- The summary's topic list: one entry per enabled topic job (slug, status, clause), JSON.

ALTER TABLE job_runs ADD COLUMN topics TEXT;
