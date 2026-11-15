-- Chat messages are pruned by age (also inside live sessions): index the timestamp.

CREATE INDEX ix_chat_messages_ts ON chat_messages (ts);
