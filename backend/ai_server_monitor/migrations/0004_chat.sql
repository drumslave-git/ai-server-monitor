-- Chat: the tables exist since 0001. `meta` carries per-message details as JSON
-- (tool mode, token usage, step number, whether a tool result was trimmed, errors).

ALTER TABLE chat_messages ADD COLUMN meta TEXT;
