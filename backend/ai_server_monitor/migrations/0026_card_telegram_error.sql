-- Why a prompt card could not be sent to Telegram (Telegram's own error text), shown on the web
-- card; empty while the card went out or Telegram is not in use. A later successful send clears it.
ALTER TABLE chat_cards ADD COLUMN telegram_error TEXT;
