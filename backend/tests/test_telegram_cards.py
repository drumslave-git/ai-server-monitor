"""Prompt cards in Telegram: the chat model's questions arrive as a message with buttons, free
text comes as a reply, and answering here or in the web chat closes the card in both."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap

from .jobfakes import ScriptedModels, tool
from .test_chat import FakeLLM
from .test_telegram import (
    CHAT,
    STRANGER,
    FakeTelegram,
    make_app,
    message,
    press,
    running,
    settle,
    wait_for,
)

ONE = [{"text": "Restart nginx?", "kind": "single", "options": ["yes", "no"]}]
MANY = [
    {"text": "Which disk?", "kind": "single", "options": ["sda", "sdb"]},
    {"text": "Which checks?", "kind": "multi", "options": ["smart", "fsck", "scrub"]},
    {"text": "Anything else?", "kind": "text"},
]


def card_messages(tg: FakeTelegram) -> list[dict[str, Any]]:
    return [b for b in tg.of("sendMessage") if "Question #" in b["text"]]


def message_id(tg: FakeTelegram, body: dict[str, Any]) -> int:
    return 1001 + tg.of("sendMessage").index(body)  # the fake numbers messages from 1001


def keys(body: dict[str, Any]) -> list[dict[str, str]]:
    return [b for row in body["reply_markup"]["inline_keyboard"] for b in row]


def key(body: dict[str, Any], label: str) -> str:
    """The callback data of the button whose text contains `label`."""
    return next(b["callback_data"] for b in keys(body) if label in b["text"])


def latest(tg: FakeTelegram, mid: int) -> dict[str, Any]:
    """The last version of message `mid`: its last edit, else the text first sent."""
    edits = [e for e in tg.of("editMessageText") if e["message_id"] == mid]
    return edits[-1] if edits else next(b for b in tg.of("sendMessage") if "Question" in b["text"])


def reply(text: str, to: int) -> dict[str, Any]:
    update = message(text)
    update["message"]["reply_to_message"] = {
        "message_id": to,
        "date": 1,
        "chat": {"id": CHAT, "type": "private"},
        "text": "Question",
    }
    return update


def toasts(tg: FakeTelegram) -> list[str]:
    return [b.get("text", "") for b in tg.of("answerCallbackQuery")]


def open_card(
    client: TestClient, tg: FakeTelegram, models: ScriptedModels, questions: list[dict[str, Any]]
) -> tuple[dict[str, Any], int]:
    models.script = [tool("ask_owner", questions=questions)]
    tg.feed(message("check my server"))
    body = wait_for(lambda: card_messages(tg))[0]
    return body, message_id(tg, body)


def test_a_card_arrives_with_buttons_and_a_tap_answers_it_and_the_run_continues(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, llm=FakeLLM())
    with running(app, tg) as client:
        body, mid = open_card(client, tg, models, ONE)
        assert body["chat_id"] == CHAT
        assert "Restart nginx?" in body["text"] and "one option" in body["text"]
        assert [b["text"] for b in keys(body)] == ["yes", "no", "✏️ Own answer"]  # no Send button
        assert all(len(b["callback_data"]) <= 64 for b in keys(body))
        assert not any("Waiting for your answer" in t for t in tg.texts())  # only the card

        models.script = ["Restarted nginx."]
        tg.feed(press(key(body, "yes"), mid))
        wait_for(lambda: "Restarted nginx." in tg.texts())
        assert (
            "The owner answered:" in models.requests[-1]["text"]
            and "yes" in (models.requests[-1]["text"])
        )
        shown = latest(tg, mid)
        assert "→ yes" in shown["text"] and "Answered here in Telegram." in shown["text"]
        assert "reply_markup" not in shown
        session = client.get("/api/chat/sessions").json()["items"][0]["id"]
        [kept] = client.get(f"/api/chat/sessions/{session}").json()["cards"]
        assert kept["status"] == "answered" and kept["answered_via"] == "telegram"
        assert kept["answers"] == [{"chosen": ["yes"], "text": ""}]
        # a second tap says it was answered already and changes nothing
        tg.feed(press(key(body, "no"), mid))
        wait_for(lambda: "This question was answered already." in toasts(tg))


def test_several_questions_toggle_then_send_with_a_reply_for_the_free_text(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, llm=FakeLLM())
    with running(app, tg) as client:
        body, mid = open_card(client, tg, models, MANY)
        assert "Tap Send" in body["text"] and "Reply to this message" in body["text"]
        assert [b["text"] for b in keys(body)][:2] == ["1. sda", "1. sdb"]
        assert "📨 Send" in [b["text"] for b in keys(body)]
        sent = len(tg.of("editMessageText"))

        def tap(label: str) -> dict[str, Any]:
            nonlocal sent
            data = key(latest(tg, mid), label)
            tg.feed(press(data, mid))
            wait_for(lambda: len(tg.of("editMessageText")) == sent + 1)
            sent += 1
            return latest(tg, mid)

        tap("sda")
        shown = tap("sdb")  # a single choice replaces the earlier one
        assert "✅ 1. sdb" in [b["text"] for b in keys(shown)]
        assert "✅ 1. sda" not in [b["text"] for b in keys(shown)]
        tap("2. scrub")
        tap("2. smart")
        shown = tap("2. scrub")  # toggled off again
        assert "→ sdb" in shown["text"] and "→ smart" in shown["text"]
        assert "scrub" not in shown["text"].split("Which checks?")[1].split("Anything")[0]
        assert not client.get("/api/approvals").json()["items"]

        # Send before the free-text question is answered: refused, the card stays open
        tg.feed(press(key(shown, "Send"), mid))
        wait_for(lambda: any("not answered" in t for t in toasts(tg)))
        assert len(models.requests) == 1

        # a reply to the message answers the first free-text question not answered yet
        tg.feed(reply("only the cables", mid))
        wait_for(lambda: len(tg.of("editMessageText")) == sent + 1)
        shown = latest(tg, mid)
        assert "→ only the cables" in shown["text"] and "Reply to this message" not in shown["text"]

        models.script = ["Checked sdb."]
        tg.feed(press(key(shown, "Send"), mid))
        wait_for(lambda: "Checked sdb." in tg.texts())
        text = models.requests[-1]["text"]
        assert "sdb" in text and "smart" in text and "only the cables" in text
        assert "Answered here in Telegram." in latest(tg, mid)["text"]


def test_own_answer_button_then_a_reply_sends_the_text_instead_of_the_choice(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, llm=FakeLLM())
    with running(app, tg) as client:
        body, mid = open_card(client, tg, models, ONE)
        tg.feed(press(key(body, "Own answer"), mid))
        wait_for(lambda: "Reply to this message with your answer." in toasts(tg))
        wait_for(lambda: tg.of("editMessageText"))
        assert "Reply to this message with your answer to question 1" in latest(tg, mid)["text"]
        models.script = ["Waited, then restarted."]
        tg.feed(reply("yes, but after the backup", mid))
        wait_for(lambda: "Waited, then restarted." in tg.texts())
        assert "yes, but after the backup (own answer)" in models.requests[-1]["text"]
        session = client.get("/api/chat/sessions").json()["items"][0]["id"]
        [kept] = client.get(f"/api/chat/sessions/{session}").json()["cards"]
        assert kept["answers"] == [{"chosen": [], "text": "yes, but after the backup"}]


def test_a_reply_to_a_card_with_nothing_to_answer_gets_a_hint_and_other_text_is_chat(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, llm=FakeLLM())
    with running(app, tg) as client:
        _, mid = open_card(client, tg, models, ONE)
        # a reply to the card whose only question is a choice (not awaiting): a hint, no answer
        count = len(tg.texts())
        tg.feed(reply("maybe", mid))
        wait_for(lambda: len(tg.texts()) == count + 1)
        assert "Own answer" in tg.texts()[-1]
        assert len(models.requests) == 1
        # a reply to any other message is a chat message as usual
        models.script = ["Hello there."]
        tg.feed(reply("hello?", 1))
        wait_for(lambda: "Hello there." in tg.texts())


def test_answering_in_the_web_chat_edits_the_telegram_message_and_removes_the_buttons(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, llm=FakeLLM())
    with running(app, tg) as client:
        body, mid = open_card(client, tg, models, ONE)
        session = client.get("/api/chat/sessions").json()["items"][0]["id"]
        [card] = client.get(f"/api/chat/sessions/{session}").json()["cards"]
        models.script = ["Done in the web."]
        done = client.post(
            f"/api/chat/cards/{card['id']}/answer", json={"answers": [{"chosen": ["no"]}]}
        )
        assert done.status_code == 200
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert edit["message_id"] == mid and "→ no" in edit["text"]
        assert "Answered in the web chat." in edit["text"] and "reply_markup" not in edit
        wait_for(lambda: "Done in the web." in tg.texts())
        # the stale button now says so
        tg.feed(press(key(body, "yes"), mid))
        wait_for(lambda: "This question was answered already." in toasts(tg))
        assert "yes" not in models.requests[-1]["text"].split("The owner answered:")[-1]


def test_forged_card_buttons_and_chats_outside_the_allowlist_do_nothing(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, llm=FakeLLM())
    with running(app, tg) as client:
        body, mid = open_card(client, tg, models, ONE)
        good = key(body, "yes")
        head, _, _ = good.rpartition(":")
        for forged in (f"{head}:deadbeef0000", "co:1:0:9:abcdef012345", "cs:1:abc", "co:x", "co"):
            tg.feed(press(forged, mid))
        wait_for(lambda: len(toasts(tg)) == 5)
        assert toasts(tg) == ["Invalid button."] * 5
        tg.feed(press(good, 1, chat=STRANGER))
        settle()
        assert len(toasts(tg)) == 5
        session = client.get("/api/chat/sessions").json()["items"][0]["id"]
        assert client.get(f"/api/chat/sessions/{session}").json()["cards"][0]["status"] == "open"
        assert tg.of("editMessageText") == []


def test_the_choices_and_the_card_survive_a_restart(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    tg = FakeTelegram()
    first = make_app(bootstrap, tg, llm=FakeLLM())
    with running(first, tg) as client:
        body, mid = open_card(client, tg, models, MANY)
        tg.feed(press(key(body, "2. fsck"), mid))
        wait_for(lambda: tg.of("editMessageText"))
    del first  # the restart: a new app, a new bot, the same database
    models.script = ["Went on."]
    second = make_app(bootstrap, tg, llm=FakeLLM())
    with running(second, tg) as client:
        settle()
        assert len(card_messages(tg)) == 1  # not announced a second time
        shown = latest(tg, mid)
        tg.feed(press(key(shown, "1. sda"), mid))
        wait_for(lambda: len(tg.of("editMessageText")) == 2)
        shown = latest(tg, mid)
        assert "→ sda" in shown["text"] and "→ fsck" in shown["text"]  # the old choice is kept
        tg.feed(reply("nothing", mid))
        wait_for(lambda: len(tg.of("editMessageText")) == 3)
        tg.feed(press(key(latest(tg, mid), "Send"), mid))
        wait_for(lambda: "Went on." in tg.texts())
        assert "sda" in models.requests[-1]["text"] and "fsck" in models.requests[-1]["text"]


def test_cards_answered_while_the_bot_was_down_are_edited_and_unsent_ones_are_sent_on_start(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    tg = FakeTelegram()
    first = make_app(bootstrap, tg, llm=FakeLLM())
    with running(first, tg) as client:
        _, mid = open_card(client, tg, models, ONE)
    conn = sqlite3.connect(bootstrap.db_path)
    conn.execute(
        "UPDATE chat_cards SET status = 'answered', answered = 1, answered_via = 'web', "
        'answers = \'[{"chosen": ["yes"], "text": ""}]\''
    )
    # a second card whose message was never sent (the bot was down when it was asked)
    conn.execute(
        "INSERT INTO chat_cards (session_id, run_id, call_id, created, source, questions) "
        "SELECT session_id, run_id, 'other', 2, source, questions FROM chat_cards"
    )
    conn.commit()
    conn.close()
    second = make_app(bootstrap, tg, llm=FakeLLM())
    with running(second, tg):
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert edit["message_id"] == mid and "Answered in the web chat." in edit["text"]
        assert "reply_markup" not in edit
        fresh = wait_for(lambda: card_messages(tg)[1:])[0]
        assert "Question #2" in fresh["text"] and keys(fresh)
