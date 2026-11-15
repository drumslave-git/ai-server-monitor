-- Telegram bot state that has to survive a restart: which Telegram message(s) show which
-- approval, so they can be edited once it is decided or expires. Small scalars (report
-- counter, last health, pending /report requests) live in `kv`.

CREATE TABLE telegram_approval_messages (
    approval_id INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    created_ts  REAL NOT NULL,
    PRIMARY KEY (approval_id, chat_id)
);
