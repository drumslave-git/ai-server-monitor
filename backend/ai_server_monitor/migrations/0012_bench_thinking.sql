-- Benchmarks at several thinking levels: each attempt records the reasoning_effort it ran with,
-- how much the model reasoned and whether the answer came back empty or hit the limit.
-- The trace keeps the length of every reply's reasoning (also when bodies are not stored).

ALTER TABLE bench_attempts ADD COLUMN effort TEXT;            -- auto | none | low | medium | high; NULL = not applicable
ALTER TABLE bench_attempts ADD COLUMN reasoning_chars INTEGER; -- characters of reasoning over the attempt's LLM calls
ALTER TABLE bench_attempts ADD COLUMN answer_issue TEXT;      -- empty (no answer: limit or thought only) | limit (answer cut off)

ALTER TABLE llm_calls ADD COLUMN reasoning_chars INTEGER;
