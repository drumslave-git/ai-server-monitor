-- Benchmarks: the hardware load while a run was going. One row every second or so (Settings →
-- Benchmarks), tied to the run; each attempt also keeps its own peak/average in `hw`.
-- Rows go away with their run.

CREATE TABLE bench_hw_samples (
    run_id     INTEGER NOT NULL REFERENCES bench_runs(id) ON DELETE CASCADE,
    ts         REAL NOT NULL,
    gpu_util   REAL,               -- % busy, highest of the GPUs
    vram_used  INTEGER,            -- bytes, all GPUs together
    vram_total INTEGER,
    llm_vram   INTEGER,            -- bytes held by the LLM server's own process, when visible
    power_w    REAL,
    temp_c     REAL,
    cpu        REAL,               -- host CPU %
    ram_used   INTEGER,            -- host RAM bytes (total - available)
    ram_total  INTEGER
);
CREATE INDEX idx_bench_hw_run ON bench_hw_samples(run_id, ts);

ALTER TABLE bench_attempts ADD COLUMN hw TEXT;   -- JSON {n, gpu_util: {avg, peak}, ...}
