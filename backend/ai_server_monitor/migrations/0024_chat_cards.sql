-- Prompt cards: questions the chat model asks the owner (web chat or Telegram). The run that
-- asked is stopped in chat_pending_runs; when every card of it is answered (and every action it
-- waits for is decided) it is resumed with the answers. The card stays as the record of what
-- was asked and answered.

CREATE TABLE chat_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    run_id INTEGER,                   -- the stopped run while it waits (chat_pending_runs.id)
    call_id TEXT NOT NULL,            -- the model's tool call this card answers
    created REAL NOT NULL,
    source TEXT,                      -- who asked: chat:<session> or telegram:<chat id>
    questions TEXT NOT NULL,          -- JSON: the questions (see chat/cards.py)
    status TEXT NOT NULL DEFAULT 'open',      -- open | answered
    answers TEXT,                     -- JSON: one answer per question, once answered
    answered REAL,
    answered_via TEXT                 -- web | telegram
);
CREATE INDEX idx_chat_cards_session ON chat_cards(session_id);
CREATE INDEX idx_chat_cards_run ON chat_cards(run_id);

-- The Telegram message of a card (one per chat) and the choices toggled in it before it is sent.
CREATE TABLE telegram_card_messages (
    card_id INTEGER NOT NULL REFERENCES chat_cards(id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    awaiting INTEGER,                 -- index of the question the next reply to the message answers
    PRIMARY KEY (card_id, chat_id)
);

CREATE TABLE telegram_card_drafts (
    card_id INTEGER NOT NULL REFERENCES chat_cards(id) ON DELETE CASCADE,
    question INTEGER NOT NULL,        -- index of the question
    answer TEXT NOT NULL,             -- JSON: the draft answer (chosen options, or the text)
    PRIMARY KEY (card_id, question)
);
