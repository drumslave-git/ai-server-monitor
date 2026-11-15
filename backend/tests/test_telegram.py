from __future__ import annotations

import functools
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app
from ai_server_monitor.outputs.telegram import (
    ID_COOLDOWN,
    BotAPI,
    TelegramError,
    html_messages,
    md_to_html,
    split_message,
)

from .arrfakes import HASH_A, FakeArrWorld, arr_settings, torrent
from .conftest import LLM_URL
from .test_chat import FakeLLM, say

TOKEN = "123456:TESTTOKEN-abcdefghijklmnopqrstuvwxyz012"
CHAT = 111
STRANGER = 999


class FakeTelegram:
    """The Bot API, in memory. Nothing here ever reaches the real Telegram."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self._message_id = 1000

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith(f"/bot{TOKEN}/"), "wrong token in the URL"
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content) if request.content else {}
        if method != "getUpdates":
            self.calls.append((method, body))
        if method == "getUpdates":
            updates, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": updates})
        if method == "sendMessage":
            self._message_id += 1
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {"message_id": self._message_id, "chat": {"id": body["chat_id"]}},
                },
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    def of(self, method: str) -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == method]

    def texts(self) -> list[str]:
        return [b["text"] for b in self.of("sendMessage")]


def wait_for(condition: Callable[[], Any], timeout: float = 5.0) -> Any:
    end = time.time() + timeout
    while time.time() < end:
        value = condition()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("timed out waiting for the bot")


def make_app(
    bootstrap: Bootstrap,
    tg: FakeTelegram,
    *,
    telegram: dict[str, Any] | None = None,
    llm: FakeLLM | None = None,
    world: FakeArrWorld | None = None,
    actions: dict[str, Any] | None = None,
) -> Any:
    arr = arr_settings() if world is not None else {}
    if world is not None:
        arr["actions"] = actions or {"stalled_cleanup": {"policy": "approve"}}
    settings = Settings.model_validate(
        {
            "llm": (
                {"base_url": LLM_URL, "model": "fake", "tool_mode": "native", "max_retries": 0}
                if llm
                else {}
            ),
            "outputs": {
                "telegram": {
                    "enabled": True,
                    "token": TOKEN,
                    "allowed_chat_ids": [CHAT],
                    "report_every": 0,
                    **(telegram or {}),
                }
            },
            "arr": arr,
        }
    )
    return create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(llm.handler) if llm else None,
        collector_registry={},
        seed_settings=settings,
        arr_transport=world.transport() if world else None,
        telegram_transport=httpx.MockTransport(tg.handler),
    )


def message(text: str, chat: int = CHAT) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text, "from": {"id": chat}}}


def press(data: str, message_id: int, chat: int = CHAT, user: str = "alice") -> dict[str, Any]:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 5, "username": user},
            "message": {"chat": {"id": chat}, "message_id": message_id, "text": "Approval"},
            "data": data,
        },
    }


# --------------------------------------------------------------------------- formatting


def test_split_keeps_every_piece_within_4096_and_cuts_at_paragraphs() -> None:
    text = "A" * 3000 + "\n\n" + "B" * 3000 + "\n\n" + "C" * 100
    pieces = split_message(text, 4096)
    assert [p[0] for p in pieces] == ["A", "B"]  # paragraphs stay whole: A | B + C
    assert pieces[1].endswith("C" * 100) and all(len(p) <= 4096 for p in pieces)
    assert "\n\n".join(pieces) == text
    giant = "x" * 10_000  # no paragraph, no line break: cut anywhere
    assert [len(p) for p in split_message(giant, 4096)] == [4096, 4096, 1808]
    lines = "\n".join(f"line {i}" for i in range(2000))
    assert all(len(p) <= 4096 for p in split_message(lines, 4096))


def test_markdown_becomes_safe_telegram_html_and_splits_without_cutting_tags() -> None:
    assert md_to_html("## Findings\n- **Disk** is `/data` & <full>") == (
        "<b>Findings</b>\n• <b>Disk</b> is <code>/data</code> &amp; &lt;full&gt;"
    )
    report = "\n\n".join(f"## Section {i}\n- **item** & <x> " + "word " * 300 for i in range(12))
    pieces = html_messages(report)
    assert len(pieces) > 1 and all(len(p) <= 4096 for p in pieces)
    for piece in pieces:  # every tag that opens also closes inside the same message
        assert piece.count("<b>") == piece.count("</b>")
        assert "<x>" not in piece and "&lt;x&gt;" in piece


async def test_api_errors_never_contain_the_token() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    api = BotAPI(TOKEN, transport=httpx.MockTransport(refuse))
    with pytest.raises(TelegramError) as info:
        await api.call("getMe")
    assert TOKEN not in str(info.value) and "ConnectError" in str(info.value)
    await api.close()

    def flood(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "slow down",
                "parameters": {"retry_after": 7},
            },
        )

    api = BotAPI(TOKEN, transport=httpx.MockTransport(flood))
    with pytest.raises(TelegramError) as limited:
        await api.call("sendMessage", chat_id=1, text="x")
    assert limited.value.retry_after == 7 and limited.value.code == 429
    await api.close()


# --------------------------------------------------------------------------- allowlist


def test_chats_outside_the_allowlist_are_ignored_without_a_reply(
    bootstrap: Bootstrap, caplog: pytest.LogCaptureFixture
) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("should never be asked"))
    app = make_app(bootstrap, tg, llm=llm)
    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        bot = app.state.ctx.telegram
        for update in (message("hello"), message("/status"), press("a:1:abc", 5)):
            for chat in (STRANGER,):
                if "message" in update:
                    update = {**update, "message": {**update["message"], "chat": {"id": chat}}}
                else:
                    update["callback_query"]["message"]["chat"]["id"] = chat
                client.portal.call(bot.handle_update, update)
    assert tg.of("sendMessage") == [] and tg.of("answerCallbackQuery") == []
    assert llm.requests == []  # the model was never bothered
    ignored = [r for r in caplog.records if "ignoring chat 999" in r.getMessage()]
    assert len(ignored) == 1  # logged once, not per message
    assert TOKEN not in caplog.text


def test_id_command_answers_everybody_but_nothing_else_does(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("should never be asked"))
    app = make_app(bootstrap, tg, llm=llm)
    with TestClient(app) as client:
        bot = app.state.ctx.telegram
        stranger = {"id": STRANGER, "type": "private"}
        update = {
            "update_id": 1,
            "message": {"chat": stranger, "text": "/id", "from": {"id": 4242}},
        }
        client.portal.call(bot.handle_update, update)
        (text,) = tg.texts()
        assert f"<code>{STRANGER}</code>" in text  # chat id
        assert "<code>4242</code>" in text  # sender id
        assert "private" in text  # chat type
        assert "Add this chat id under Settings → Outputs → Telegram → Allowed chat IDs" in text
        assert tg.of("sendMessage")[0]["chat_id"] == STRANGER
        # The same stranger gets nothing else: no commands, no chat, no buttons.
        for other in ("/status", "/help", "/report", "hello", "/idx", "/id2"):
            client.portal.call(
                bot.handle_update, {"update_id": 2, "message": {**update["message"], "text": other}}
            )
        client.portal.call(bot.handle_update, press("a:1:abc", 5, chat=STRANGER))
    assert len(tg.texts()) == 1 and tg.of("answerCallbackQuery") == []
    assert llm.requests == []


def test_id_command_forms_group_chat_and_allowed_chat(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with TestClient(app) as client:
        bot = app.state.ctx.telegram
        group = {"update_id": 1, "message": {"chat": {"id": -100123, "type": "supergroup"}}}
        group["message"]["text"] = "/id@my_asm_bot"
        group["message"]["from"] = {"id": 7}
        client.portal.call(bot.handle_update, group)
        client.portal.call(bot.handle_update, message("/ID"))  # an allowed chat: also fine
        group_text, allowed_text = tg.texts()
    assert "<code>-100123</code>" in group_text and "supergroup" in group_text
    assert f"<code>{CHAT}</code>" in allowed_text and "already allowed" in allowed_text


def test_id_command_is_rate_limited_per_chat(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with TestClient(app) as client:
        bot = app.state.ctx.telegram
        for _ in range(5):
            client.portal.call(bot.handle_update, message("/id", chat=STRANGER))
        assert len(tg.texts()) == 1  # a burst gets one answer
        client.portal.call(bot.handle_update, message("/id", chat=STRANGER + 1))
        assert len(tg.texts()) == 2  # other chats have their own limit
        bot._id_replied[STRANGER] -= ID_COOLDOWN + 1  # the cooldown has passed
        client.portal.call(bot.handle_update, message("/id", chat=STRANGER))
        assert len(tg.texts()) == 3


# --------------------------------------------------------------------------- approvals


def test_approval_button_executes_through_the_action_layer_and_edits_the_message(
    bootstrap: Bootstrap,
) -> None:
    tg, world = FakeTelegram(), FakeArrWorld()
    world.torrents = {HASH_A: torrent(HASH_A, "Stuck.Movie", "stalledDL")}
    app = make_app(bootstrap, tg, world=world)
    with TestClient(app) as client:
        ctx = app.state.ctx
        record = client.portal.call(  # type: ignore[union-attr]
            ctx.actions.request,
            "qbit_delete_torrent",
            {"hash": HASH_A, "title": "Stuck.Movie"},
            "scheduler",
        )
        assert record["status"] == "pending"
        sent = wait_for(lambda: tg.of("sendMessage"))[0]
        assert sent["chat_id"] == CHAT and "Approval needed" in sent["text"]
        assert "qbit_delete_torrent" in sent["text"] and "Stuck.Movie" in sent["text"]
        buttons = sent["reply_markup"]["inline_keyboard"][0]
        assert [b["text"] for b in buttons] == ["✅ Approve", "❌ Reject"]
        assert all(len(b["callback_data"]) <= 64 for b in buttons)
        assert HASH_A in world.torrents  # nothing happened yet

        message_id = 1001
        client.portal.call(
            ctx.telegram.handle_update, press(buttons[0]["callback_data"], message_id)
        )
        done = client.get("/api/actions").json()["items"][0]
        assert done["status"] == "executed" and done["decided_by"] == "telegram:alice"
        assert HASH_A not in world.torrents  # the real work went through the action layer
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert edit["message_id"] == message_id and "approved → executed" in edit["text"]
        assert edit["reply_markup"] == {"inline_keyboard": []}  # buttons are gone
        assert tg.of("answerCallbackQuery")[0]["text"] == "approved → executed"

        # Pressing again says what happened instead of doing anything.
        client.portal.call(
            ctx.telegram.handle_update, press(buttons[0]["callback_data"], message_id)
        )
        assert "not pending" in tg.of("answerCallbackQuery")[-1]["text"]


def test_forged_and_malformed_callbacks_are_rejected(bootstrap: Bootstrap) -> None:
    tg, world = FakeTelegram(), FakeArrWorld()
    world.torrents = {HASH_A: torrent(HASH_A, "x", "stalledDL")}
    app = make_app(bootstrap, tg, world=world)
    with TestClient(app) as client:
        ctx = app.state.ctx
        record = client.portal.call(  # type: ignore[union-attr]
            ctx.actions.request, "qbit_delete_torrent", {"hash": HASH_A}, "scheduler"
        )
        wait_for(lambda: tg.of("sendMessage"))
        good = ctx.telegram.sign("a", record["id"])
        for forged in (
            f"a:{record['id']}:deadbeef0000",  # made-up signature
            f"r:{record['id']}:{good}",  # signature of the other decision
            f"a:{record['id'] + 1}:{good}",  # signature of another action
            "a:oops",
            "garbage",
            f"a:{record['id']}:",
        ):
            client.portal.call(ctx.telegram.handle_update, press(forged, 1001))
        answers = [b["text"] for b in tg.of("answerCallbackQuery")]
        assert answers == ["Invalid button."] * 6
        assert client.get("/api/approvals").json()["items"][0]["status"] == "pending"
        assert HASH_A in world.torrents and tg.of("editMessageText") == []
        # A press with a valid signature from a chat that is not allowed is ignored too.
        client.portal.call(
            ctx.telegram.handle_update, press(f"a:{record['id']}:{good}", 1, chat=STRANGER)
        )
        assert len(tg.of("answerCallbackQuery")) == 6


def test_decisions_made_elsewhere_are_reflected_in_the_message(bootstrap: Bootstrap) -> None:
    tg, world = FakeTelegram(), FakeArrWorld()
    world.torrents = {HASH_A: torrent(HASH_A, "x", "stalledDL")}
    app = make_app(bootstrap, tg, world=world)
    with TestClient(app) as client:
        ctx = app.state.ctx
        record = client.portal.call(  # type: ignore[union-attr]
            ctx.actions.request, "qbit_delete_torrent", {"hash": HASH_A}, "scheduler"
        )
        wait_for(lambda: tg.of("sendMessage"))
        client.post(f"/api/approvals/{record['id']}", json={"decision": "reject"})  # the web UI
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert "rejected by ui" in edit["text"] and edit["message_id"] == 1001
        assert HASH_A in world.torrents


def stored_messages(client: TestClient, app: Any) -> int:
    row = client.portal.call(  # type: ignore[union-attr]
        app.state.ctx.db.fetchone, "SELECT COUNT(*) FROM telegram_approval_messages"
    )
    return int(row[0])


def _request_approval(client: TestClient, app: Any, tg: FakeTelegram) -> dict[str, Any]:
    record = client.portal.call(  # type: ignore[union-attr]
        app.state.ctx.actions.request, "qbit_delete_torrent", {"hash": HASH_A}, "scheduler"
    )
    wait_for(lambda: stored_messages(client, app) == 1)  # sent and remembered
    return record


def test_approval_messages_are_edited_after_a_restart(bootstrap: Bootstrap) -> None:
    tg, world = FakeTelegram(), FakeArrWorld()
    world.torrents = {HASH_A: torrent(HASH_A, "x", "stalledDL")}
    first = make_app(bootstrap, tg, world=world)
    with TestClient(first) as client:
        record = _request_approval(client, first, tg)
    del first  # the restart: a new app, a new bot, the same database
    second = make_app(bootstrap, tg, world=world)
    with TestClient(second) as client:
        assert not tg.of("editMessageText")  # still pending: nothing to catch up
        client.post(f"/api/approvals/{record['id']}", json={"decision": "reject"})
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert "rejected by ui" in edit["text"] and edit["message_id"] == 1001
        wait_for(lambda: stored_messages(client, second) == 0)


def test_approvals_decided_while_the_bot_was_down_are_edited_on_start(
    bootstrap: Bootstrap,
) -> None:
    import sqlite3

    tg, world = FakeTelegram(), FakeArrWorld()
    world.torrents = {HASH_A: torrent(HASH_A, "x", "stalledDL")}
    first = make_app(bootstrap, tg, world=world)
    with TestClient(first) as client:
        record = _request_approval(client, first, tg)
    conn = sqlite3.connect(bootstrap.db_path)
    conn.execute("UPDATE actions SET status = 'expired' WHERE id = ?", (record["id"],))
    conn.commit()
    conn.close()
    second = make_app(bootstrap, tg, world=world)
    with TestClient(second) as client:
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert "expired" in edit["text"] and edit["message_id"] == 1001
        assert edit["reply_markup"] == {"inline_keyboard": []}
        wait_for(lambda: stored_messages(client, second) == 0)
    assert TOKEN not in json.dumps(tg.calls[1:])  # the token is only ever in the URL


# --------------------------------------------------------------------------- reports


def report_event(i: int, health: str) -> dict[str, Any]:
    return {
        "id": i,
        "health": health,
        "score": 90,
        "summary": f"summary {i}",
        "findings": [{"severity": "warn", "title": "Disk", "detail": "91% full"}],
    }


def test_alerts_only_mode_sends_a_report_only_when_the_health_changes(
    bootstrap: Bootstrap,
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, telegram={"report_every": 0})
    with TestClient(app) as client:
        bot = app.state.ctx.telegram
        for i, health in enumerate(["ok", "ok", "warn", "warn", "ok"], start=1):
            client.portal.call(bot.on_event, "report.done", report_event(i, health))
    texts = tg.texts()
    assert len(texts) == 2  # ok→warn and warn→ok; the first report is only the baseline
    assert "Health changed: ok → warn" in texts[0] and "summary 3" in texts[0]
    assert "Disk</b> — 91% full" in texts[0]
    assert "Health changed: warn → ok" in texts[1]


def test_every_nth_report_is_sent_in_full_and_alerts_can_be_switched_off(
    bootstrap: Bootstrap,
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, telegram={"report_every": 2, "alerts": False})
    with TestClient(app) as client:
        ctx = app.state.ctx
        for i in (1, 2, 3, 4):
            add = functools.partial(
                ctx.reports.store.add, ts=float(i), status="ok", markdown=f"## Report {i}\n- fine"
            )
            client.portal.call(add)
        for i, health in enumerate(["ok", "warn", "ok", "ok"], start=1):
            client.portal.call(ctx.telegram.on_event, "report.done", report_event(i, health))
    texts = tg.texts()
    assert len(texts) == 2 and "<b>Report 2</b>" in texts[0] and "<b>Report 4</b>" in texts[1]
    assert "Health changed" not in texts[0]  # alerts are off: no extra messages for changes


def test_the_every_nth_counter_survives_a_restart(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    for i in (1, 2, 3, 4):
        app = make_app(bootstrap, tg, telegram={"report_every": 2, "alerts": False})
        with TestClient(app) as client:
            ctx = app.state.ctx
            client.portal.call(
                functools.partial(
                    ctx.reports.store.add, ts=float(i), status="ok", markdown=f"## Report {i}"
                )
            )
            client.portal.call(ctx.telegram.on_event, "report.done", report_event(i, "ok"))
        # a fresh bot every time: only the database carries the count
    texts = tg.texts()
    assert len(texts) == 2 and "<b>Report 2</b>" in texts[0] and "<b>Report 4</b>" in texts[1]


def test_commands_status_help_approvals_and_report(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with TestClient(app) as client:
        ctx = app.state.ctx
        add = functools.partial(
            ctx.reports.store.add,
            ts=time.time(),
            status="ok",
            health="warn",
            score=71,
            summary="Disk is filling up",
            markdown="## Overview\nfull report body",
        )
        client.portal.call(add)
        for text in ("/help", "/status", "/approvals", "/nope"):
            client.portal.call(ctx.telegram.handle_update, message(text))
        help_text, status, approvals, unknown = tg.texts()
        assert "/status" in help_text and "/report" in help_text and "/id" in help_text
        assert "warn (score 71)" in status and "Disk is filling up" in status
        assert "Nothing is waiting" in approvals and "Unknown command" in unknown

        client.portal.call(ctx.telegram.handle_update, message("/report"))
        assert "Writing a report" in tg.texts()[-1]
        # The requester gets the next report in full, although the mode is "alerts only".
        client.portal.call(
            ctx.telegram.on_event, "report.done", {**report_event(1, "warn"), "id": 1}
        )
        assert "full report body" in tg.texts()[-1]


# --------------------------------------------------------------------------- chat


def test_chat_round_trip_uses_one_session_per_telegram_chat(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("The server is **fine**."), say("Still fine."))
    app = make_app(bootstrap, tg, llm=llm)
    with TestClient(app) as client:
        ctx = app.state.ctx
        client.portal.call(ctx.telegram.handle_update, message("how is the server?"))
        client.portal.call(ctx.telegram.handle_update, message("and now?"))
        assert tg.texts() == ["The server is <b>fine</b>.", "Still fine."]
        assert tg.of("sendChatAction")[0]["action"] == "typing"
        sessions = client.get("/api/chat/sessions").json()
        assert sessions["total"] == 1 and sessions["items"][0]["channel"] == "telegram"
        # The second question saw the first exchange.
        replay = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] != "system"]
        assert replay[:2] == ["how is the server?", "The server is **fine**."]
        assert tg.of("sendMessage")[0]["parse_mode"] == "HTML"


def test_new_command_starts_a_fresh_session_and_keeps_the_old_one(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("one"), say("two"))
    app = make_app(bootstrap, tg, llm=llm)
    with TestClient(app) as client:
        ctx = app.state.ctx
        client.portal.call(ctx.telegram.handle_update, message("first topic"))
        client.portal.call(ctx.telegram.handle_update, message("/new"))
        assert "Fresh chat" in tg.texts()[-1]
        client.portal.call(ctx.telegram.handle_update, message("second topic"))
        assert client.get("/api/chat/sessions").json()["total"] == 2  # the old one stays listed
        replay = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] != "system"]
        assert replay == ["second topic"]  # the model does not see the old chat
        sent = len(tg.texts())
        client.portal.call(ctx.telegram.handle_update, message("/new", chat=STRANGER))
        assert len(tg.texts()) == sent  # a stranger cannot reset anybody's chat


def test_new_is_listed_in_help_and_the_bot_menu(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with TestClient(app) as client:
        client.portal.call(app.state.ctx.telegram.handle_update, message("/help"))
        menu = wait_for(lambda: tg.of("setMyCommands"))
        assert "/new" in tg.texts()[0]
        assert "new" in [c["command"] for c in menu[0]["commands"]]


def _age_sessions(client: Any, ctx: Any, seconds: float) -> None:
    async def run() -> None:
        await ctx.db.execute("UPDATE chat_sessions SET updated = updated - ?", (seconds,))

    client.portal.call(run)


def test_chat_starts_a_new_session_after_a_quiet_gap(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("a"), say("b"), say("c"))
    app = make_app(bootstrap, tg, llm=llm, telegram={"new_session_after": "6h"})
    with TestClient(app) as client:
        ctx = app.state.ctx
        client.portal.call(ctx.telegram.handle_update, message("morning question"))
        _age_sessions(client, ctx, 3600)  # one hour: still the same chat
        client.portal.call(ctx.telegram.handle_update, message("follow up"))
        assert client.get("/api/chat/sessions").json()["total"] == 1
        _age_sessions(client, ctx, 7 * 3600)  # now quiet for longer than 6 hours
        client.portal.call(ctx.telegram.handle_update, message("evening question"))
        assert client.get("/api/chat/sessions").json()["total"] == 2
        replay = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] != "system"]
        assert replay == ["evening question"]


def test_new_session_after_zero_never_starts_a_new_session(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("a"), say("b"))
    app = make_app(bootstrap, tg, llm=llm, telegram={"new_session_after": "0"})
    with TestClient(app) as client:
        ctx = app.state.ctx
        client.portal.call(ctx.telegram.handle_update, message("one"))
        _age_sessions(client, ctx, 30 * 86400)
        client.portal.call(ctx.telegram.handle_update, message("two"))
        assert client.get("/api/chat/sessions").json()["total"] == 1


def test_chat_failure_is_reported_in_the_chat(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)  # no LLM configured
    with TestClient(app) as client:
        client.portal.call(app.state.ctx.telegram.handle_update, message("hello?"))
    assert "not configured" in tg.texts()[0] and tg.texts()[0].startswith("⚠️")


def test_chat_write_requests_carry_the_telegram_source(bootstrap: Bootstrap) -> None:
    tg, world = FakeTelegram(), FakeArrWorld()
    world.torrents = {HASH_A: torrent(HASH_A, "x", "stalledDL")}
    llm = FakeLLM(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "w",
                    "type": "function",
                    "function": {
                        "name": "qbit_delete_torrent",
                        "arguments": json.dumps({"hash": HASH_A}),
                    },
                }
            ],
        },
        say("I asked for approval."),
    )
    app = make_app(bootstrap, tg, llm=llm, world=world)
    with TestClient(app) as client:
        client.portal.call(app.state.ctx.telegram.handle_update, message("delete that torrent"))
        pending = client.get("/api/approvals").json()["items"]
        assert pending[0]["source"] == f"telegram:{CHAT}" and HASH_A in world.torrents
        wait_for(lambda: any("Approval needed" in t for t in tg.texts()))


# --------------------------------------------------------------------------- the loop


def test_polling_loop_serves_updates_and_survives_errors(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    failures = {"left": 1}
    original = tg.handler

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates") and failures["left"]:
            failures["left"] -= 1
            return httpx.Response(502, text="bad gateway")
        return original(request)

    app = create_app(
        bootstrap,
        collector_registry={},
        seed_settings=Settings.model_validate(
            {"outputs": {"telegram": {"enabled": True, "token": TOKEN, "allowed_chat_ids": [CHAT]}}}
        ),
        telegram_transport=httpx.MockTransport(flaky),
    )
    with TestClient(app):
        tg.updates.append(message("/help"))
        wait_for(lambda: tg.texts(), timeout=8)  # after one failed poll and a backoff
    assert "/status" in tg.texts()[0]
    assert tg.of("setMyCommands")[0]["commands"][0]["command"] == "status"
