"""The Telegram bot, driven through python-telegram-bot's own request layer: a fake Bot API
(an httpx transport) answers getMe, getUpdates (scripted updates), sendMessage and the rest.
Nothing here ever reaches the real Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_server_monitor.actions import REGISTRY
from ai_server_monitor.config import Bootstrap, Settings
from ai_server_monitor.main import create_app
from ai_server_monitor.outputs import telegram as telegram_module
from ai_server_monitor.outputs.telegram import (
    ID_COOLDOWN,
    markdown_messages,
    split_message,
)

from .actionfakes import NAME, FakeAction
from .conftest import LLM_URL
from .jobfakes import ScriptedModels, finish, tool
from .test_chat import FakeLLM, say

TOKEN = "123456:TESTTOKEN-abcdefghijklmnopqrstuvwxyz012"
CHAT = 111
OTHER = 222
STRANGER = 999
pytestmark = pytest.mark.usefixtures("skip_evidence")

FINDING = {"severity": "warn", "title": "/ is 88% full", "detail": "12 GB free"}
JSON_PARAMS = {
    "entities",
    "reply_markup",
    "commands",
    "allowed_updates",
    "chat_id",
    "message_id",
    "timeout",
    "offset",
}


@pytest.fixture(autouse=True)
def no_debounce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approvals are announced at once unless a test is about the debounce."""
    monkeypatch.setattr(telegram_module, "APPROVAL_DEBOUNCE", 0.0)


class FakeTelegram:
    """The Bot API, in memory."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bad_paths: list[str] = []
        self.fail_get_updates = 0
        self.refuse_entities = False
        """sendMessage with entities answers 400, as Telegram does for entities it cannot use."""
        self._updates: list[dict[str, Any]] = []
        self._update_id = 100
        self._message_id = 1000

    def feed(self, update: dict[str, Any]) -> None:
        self._update_id += 1
        self._updates.append({"update_id": self._update_id, **update})

    async def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith(f"{self.prefix}/bot{TOKEN}/"):
            self.bad_paths.append(path)
        method = path.rsplit("/", 1)[1]
        body: dict[str, Any] = {}
        for key, value in parse_qsl(request.content.decode()):
            body[key] = json.loads(value) if key in JSON_PARAMS else value
        if method == "getUpdates":
            if self.fail_get_updates:
                self.fail_get_updates -= 1
                return httpx.Response(502, text="bad gateway")
            offset = int(body.get("offset") or 0)
            self._updates = [u for u in self._updates if u["update_id"] >= offset]
            if not self._updates:
                await asyncio.sleep(0.05)  # a long poll that found nothing
            return httpx.Response(200, json={"ok": True, "result": list(self._updates)})
        self.calls.append((method, body))
        if method == "getMe":
            user = {"id": 1, "is_bot": True, "first_name": "ASM", "username": "my_asm_bot"}
            return httpx.Response(200, json={"ok": True, "result": user})
        if method == "sendMessage" and self.refuse_entities and "entities" in body:
            refused = {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: can't parse entities",
            }
            return httpx.Response(400, json=refused)
        if method == "sendMessage":
            self._message_id += 1
            result = {
                "message_id": self._message_id,
                "date": int(time.time()),
                "chat": {"id": body["chat_id"], "type": "private"},
                "text": body.get("text", ""),
            }
            return httpx.Response(200, json={"ok": True, "result": result})
        return httpx.Response(200, json={"ok": True, "result": True})

    def of(self, method: str) -> list[dict[str, Any]]:
        return [b for m, b in list(self.calls) if m == method]

    def texts(self, chat: int | None = None) -> list[str]:
        return [b["text"] for b in self.of("sendMessage") if chat in (None, b["chat_id"])]


def marked(body: dict[str, Any], kind: str) -> list[str]:
    """The pieces of a sent message's text its `kind` entities (bold, code, …) cover."""
    raw = body["text"].encode("utf-16-le")  # entity offsets count UTF-16 units
    return [
        raw[e["offset"] * 2 : (e["offset"] + e["length"]) * 2].decode("utf-16-le")
        for e in body.get("entities") or []
        if e["type"] == kind
    ]


def wait_for(condition: Callable[[], Any], timeout: float = 8.0) -> Any:
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
) -> Any:
    settings = Settings.model_validate(
        {
            "llm": ({"base_url": LLM_URL, "model": "fake", "max_retries": 0} if llm else {}),
            "outputs": {
                "telegram": {
                    "enabled": True,
                    "token": TOKEN,
                    "allowed_chat_ids": [CHAT],
                    **(telegram or {}),
                }
            },
        }
    )
    return create_app(
        bootstrap,
        llm_transport=httpx.MockTransport(llm.handler) if llm else None,
        seed_settings=settings,
        telegram_transport=httpx.MockTransport(tg.handler),
    )


class Bot:
    """A running app with its bot; `say` feeds an update and `settle` lets the bot catch up."""

    def __init__(self, app: Any, tg: FakeTelegram) -> None:
        self.app, self.tg = app, tg
        self._client: TestClient | None = None

    def __enter__(self) -> TestClient:
        self._client = TestClient(self.app)
        client = self._client.__enter__()
        wait_for(lambda: self.app.state.ctx.telegram.ready.is_set())
        return client

    def __exit__(self, *exc: object) -> None:
        assert self._client is not None
        self._client.__exit__(*exc)
        assert self.tg.bad_paths == []


def running(app: Any, tg: FakeTelegram) -> Bot:
    return Bot(app, tg)


def message(text: str, chat: int = CHAT, chat_type: str = "private") -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": 5,
        "date": int(time.time()),
        "chat": {"id": chat, "type": chat_type},
        "from": {"id": chat, "is_bot": False, "first_name": "A"},
        "text": text,
    }
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"message": msg}


def press(data: str, message_id: int, chat: int = CHAT, user: str = "alice") -> dict[str, Any]:
    return {
        "callback_query": {
            "id": "cb1",
            "chat_instance": "x",
            "from": {"id": 5, "is_bot": False, "first_name": "A", "username": user},
            "message": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": chat, "type": "private"},
                "text": "Approval",
            },
            "data": data,
        }
    }


def settle() -> None:
    time.sleep(0.4)


def ask(client: TestClient, tg: FakeTelegram, update: dict[str, Any], texts: int) -> list[str]:
    """Feed `update` and wait until the bot has sent `texts` messages in all."""
    tg.feed(update)
    wait_for(lambda: len(tg.texts()) >= texts)
    return tg.texts()


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


def utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def entities(markdown: str) -> tuple[str, list[dict[str, Any]]]:
    """The one message `markdown` becomes, its entities as dicts."""
    [(text, found)] = markdown_messages(markdown)
    return text, [e.to_dict() for e in found]


def test_markdown_becomes_text_and_entities() -> None:
    text, found = entities("```bash\ndf -h /\n```")
    assert text == "df -h /"
    assert found == [{"type": "pre", "offset": 0, "length": 7, "language": "bash"}]

    text, found = entities("Run `df -h` and **look**.")
    assert text == "Run df -h and look."
    assert found == [
        {"type": "code", "offset": 4, "length": 5},
        {"type": "bold", "offset": 14, "length": 4},
    ]

    text, found = entities("# Disks\n- one\n- two")
    assert text == "Disks\n⦁ one\n⦁ two\n"  # no symbol (the package's default 📌) before a heading
    assert found == [
        {"type": "underline", "offset": 0, "length": 5},
        {"type": "bold", "offset": 0, "length": 5},
    ]


def test_offsets_count_utf16_units_after_cyrillic_and_emoji() -> None:
    markdown = "Диск почти полон 💾:\n\n```bash\ndf -h /\n```"
    text, found = entities(markdown)
    [pre] = found
    assert pre["type"] == "pre" and pre["language"] == "bash"
    start = text.index("df -h")  # Python counts code points, Telegram UTF-16 units
    assert pre["offset"] == utf16(text[:start])
    assert pre["length"] == utf16("df -h /")
    assert pre["offset"] != start  # the emoji is two units


def test_a_long_message_is_split_with_every_entity_inside_its_piece() -> None:
    lead = "Проверка диска. " * 252  # Cyrillic up to just before the 4096 boundary
    block = "```bash\n" + "\n".join(f"echo line {i}" for i in range(20)) + "\n```"
    pieces = markdown_messages(f"{lead}\n\n{block}\n\nDone **now**.")
    assert len(pieces) >= 2
    for text, found in pieces:
        assert utf16(text) <= 4096
        for entity in found:
            assert entity.offset >= 0 and entity.offset + entity.length <= utf16(text)
    pres = [e for _, found in pieces for e in found if e.type == "pre"]
    assert pres and all(e.language == "bash" for e in pres)
    joined = "".join(text for text, _ in pieces)
    assert all(f"echo line {i}" in joined for i in range(20))


# --------------------------------------------------------------------------- the transport


def test_polling_serves_updates_survives_errors_and_registers_the_menu(
    bootstrap: Bootstrap, caplog: pytest.LogCaptureFixture
) -> None:
    tg = FakeTelegram()
    tg.fail_get_updates = 1  # one failed poll first: PTB backs off and goes on
    app = make_app(bootstrap, tg)
    with caplog.at_level(logging.INFO), running(app, tg):
        tg.feed(message("/help"))
        wait_for(lambda: tg.texts(), timeout=15)
        menu = wait_for(lambda: tg.of("setMyCommands"))[0]["commands"]
    commands = [c["command"] for c in menu]
    assert commands[:4] == ["status", "jobs", "run", "summary"]
    assert {"approvals", "new", "id", "help"} <= set(commands)
    text = tg.texts()[0]
    for word in ("/status", "/jobs", "/run", "/summary", "/approvals", "/new", "/id"):
        assert word in text
    assert tg.of("deleteWebhook") is not None
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(tg.calls)


def test_a_custom_bot_api_server_is_used(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram(prefix="/botapi")
    app = make_app(bootstrap, tg, telegram={"api_url": "http://botapi.test:8081/botapi/"})
    with running(app, tg):
        ask(None, tg, message("/help"), 1)  # type: ignore[arg-type]
    assert tg.bad_paths == []  # every call went to <api_url>/bot<token>/<method>


def test_bot_is_stopped_and_started_with_the_settings(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        first = app.state.ctx.telegram
        body = client.get("/api/settings").json()["values"]
        body["outputs"]["telegram"]["allowed_chat_ids"] = [CHAT, OTHER]
        body["outputs"]["telegram"]["token"] = TOKEN
        assert client.put("/api/settings", json=body).status_code == 200
        wait_for(lambda: app.state.ctx.telegram is not first)
        wait_for(lambda: app.state.ctx.telegram.ready.is_set())
        assert app.state.ctx.telegram.allowed == {CHAT, OTHER}
        ask(client, tg, message("/help", chat=OTHER), 1)  # the new bot serves the new chat


# --------------------------------------------------------------------------- allowlist


def test_chats_outside_the_allowlist_are_ignored_without_a_reply(
    bootstrap: Bootstrap, caplog: pytest.LogCaptureFixture
) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("should never be asked"))
    app = make_app(bootstrap, tg, llm=llm)
    with caplog.at_level(logging.WARNING), running(app, tg):
        for update in (
            message("hello", STRANGER),
            message("/status", STRANGER),
            press("ba:1:abc", 5, chat=STRANGER),
        ):
            tg.feed(update)
        settle()
    assert tg.of("sendMessage") == [] and tg.of("answerCallbackQuery") == []
    assert llm.requests == []  # the model was never bothered
    ignored = [r for r in caplog.records if "ignoring chat 999" in r.getMessage()]
    assert len(ignored) == 1  # logged once, not per message
    assert TOKEN not in caplog.text


def test_id_command_answers_everybody_but_nothing_else_does(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("should never be asked"))
    app = make_app(bootstrap, tg, llm=llm)
    with running(app, tg) as client:
        update = message("/id", STRANGER)
        update["message"]["from"] = {"id": 4242, "is_bot": False, "first_name": "S"}
        (text,) = ask(client, tg, update, 1)
        assert f"<code>{STRANGER}</code>" in text  # chat id
        assert "<code>4242</code>" in text  # sender id
        assert "private" in text  # chat type
        assert "Add this chat id under Settings → Outputs → Telegram → Allowed chat IDs" in text
        assert tg.of("sendMessage")[0]["chat_id"] == STRANGER
        # The same stranger gets nothing else: no commands, no chat, no buttons.
        for other in ("/status", "/help", "/report", "hello", "/idx", "/id2", "/id@otherbot"):
            tg.feed(message(other, STRANGER))
        tg.feed(press("ba:1:abc", 5, chat=STRANGER))
        settle()
    assert len(tg.texts()) == 1 and tg.of("answerCallbackQuery") == []
    assert llm.requests == []


def test_id_command_forms_group_chat_and_allowed_chat(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        group = message("/id@my_asm_bot", -100123, "supergroup")
        group_text = ask(client, tg, group, 1)[0]
        allowed_text = ask(client, tg, message("/ID"), 2)[1]  # an allowed chat: also fine
        settle()
        assert len(tg.texts()) == 2  # and no "unknown command" on top
    assert "<code>-100123</code>" in group_text and "supergroup" in group_text
    assert f"<code>{CHAT}</code>" in allowed_text and "already allowed" in allowed_text


def test_id_command_is_rate_limited_per_chat(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        bot = app.state.ctx.telegram
        for _ in range(5):
            tg.feed(message("/id", chat=STRANGER))
        wait_for(lambda: tg.texts())
        settle()
        assert len(tg.texts()) == 1  # a burst gets one answer
        ask(client, tg, message("/id", chat=STRANGER + 1), 2)  # other chats have their own limit
        bot._id_replied[STRANGER] -= ID_COOLDOWN + 1  # the cooldown has passed
        ask(client, tg, message("/id", chat=STRANGER), 3)


# --------------------------------------------------------------------------- approvals


def stored_messages(client: TestClient, app: Any) -> int:
    row = client.portal.call(  # type: ignore[union-attr]
        app.state.ctx.db.fetchone, "SELECT COUNT(*) FROM telegram_batch_messages"
    )
    return int(row[0])


def _request_approval(client: TestClient, app: Any, tg: FakeTelegram) -> dict[str, Any]:
    record = client.portal.call(  # type: ignore[union-attr]
        app.state.ctx.actions.request, NAME, {"target": "alpha"}, "scheduler"
    )
    wait_for(lambda: stored_messages(client, app) == 1)  # sent and remembered
    return record


def file_run(client: TestClient, app: Any, n: int) -> list[dict[str, Any]]:
    """What an automation run does: n items of one action from one source, in one batch."""
    service = app.state.ctx.actions
    return client.portal.call(  # type: ignore[union-attr,no-any-return]
        lambda: service.request_many(
            NAME,
            [({"target": f"t{i}"}, None) for i in range(1, n + 1)],
            "scheduler",
            run_key="run:1",
            label="Nightly run",
        )
    )


def test_approval_card_and_approve_button_execute_and_edit_the_message(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        ctx = app.state.ctx
        record = client.portal.call(  # type: ignore[union-attr]
            ctx.actions.request, NAME, {"target": "alpha", "title": "Alpha"}, "scheduler"
        )
        assert record["status"] == "pending"
        sent = wait_for(lambda: tg.of("sendMessage"))[0]
        assert sent["chat_id"] == CHAT and "Approval needed" in sent["text"]
        assert "fake: alpha" in sent["text"]
        buttons = sent["reply_markup"]["inline_keyboard"][0]
        assert [b["text"] for b in buttons] == ["✅ Approve", "❌ Reject"]
        assert all(len(b["callback_data"]) <= 64 for b in buttons)
        assert fake_action.calls == []  # nothing happened yet

        tg.feed(press(buttons[0]["callback_data"], 1001))
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        done = client.get("/api/actions").json()["items"][0]
        assert done["status"] == "executed" and done["decided_by"] == "telegram:alice"
        assert fake_action.calls == ["alpha"]  # the real work went through the action layer
        assert edit["message_id"] == 1001 and "Approved by @alice: 1 executed" in edit["text"]
        assert "reply_markup" not in edit  # buttons are gone
        assert tg.of("answerCallbackQuery")[0]["text"] == "Approved by @alice: 1 executed"

        # Pressing again says what happened instead of doing anything.
        tg.feed(press(buttons[0]["callback_data"], 1001))
        wait_for(lambda: len(tg.of("answerCallbackQuery")) == 2)
        assert "Already decided" in tg.of("answerCallbackQuery")[-1]["text"]


def test_deny_button_rejects(bootstrap: Bootstrap, fake_action: FakeAction) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        file_run(client, app, 4)
        sent = wait_for(lambda: tg.of("sendMessage"))[0]
        assert "Review" not in json.dumps(sent["reply_markup"])  # no web address configured
        reject = sent["reply_markup"]["inline_keyboard"][0][1]
        tg.feed(press(reject["callback_data"], 1001))
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert fake_action.calls == []
        assert "Rejected by @alice: 4 rejected" in edit["text"]
        rows = client.get("/api/actions").json()["items"]
        assert {r["status"] for r in rows} == {"rejected"}
        assert {r["decided_by"] for r in rows} == {"telegram:alice"}


def test_a_button_on_an_expired_approval_answers_expired(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        record = _request_approval(client, app, tg)
        card = tg.of("sendMessage")[0]["reply_markup"]["inline_keyboard"][0]
        conn = sqlite3.connect(bootstrap.db_path)
        conn.execute("UPDATE actions SET status = 'expired' WHERE id = ?", (record["id"],))
        conn.commit()
        conn.close()
        tg.feed(press(card[0]["callback_data"], 1001))
        wait_for(lambda: tg.of("answerCallbackQuery"))
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
    assert "expired" in tg.of("answerCallbackQuery")[0]["text"]
    assert "expired" in edit["text"] and "reply_markup" not in edit
    assert fake_action.calls == []


def test_a_button_on_an_approval_of_a_removed_action_answers_expired(
    bootstrap: Bootstrap, fake_action: FakeAction, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approvals of actions that no longer exist (the *arr ones, closed as expired by an
    upgrade) still have cards in the chat."""
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        record = _request_approval(client, app, tg)
        card = tg.of("sendMessage")[0]["reply_markup"]["inline_keyboard"][0]
        conn = sqlite3.connect(bootstrap.db_path)
        conn.execute(
            "UPDATE actions SET status = 'expired', decided_by = 'upgrade' WHERE id = ?",
            (record["id"],),
        )
        conn.commit()
        conn.close()
        monkeypatch.delitem(REGISTRY, NAME)  # the action is not in the registry any more
        for data in (card[0]["callback_data"], card[1]["callback_data"]):
            tg.feed(press(data, 1001))
        wait_for(lambda: len(tg.of("answerCallbackQuery")) == 2)
        wait_for(lambda: len(tg.of("editMessageText")) == 2)
    assert all("expired" in a["text"] for a in tg.of("answerCallbackQuery"))
    assert all("expired" in e["text"] for e in tg.of("editMessageText"))


def test_forged_and_malformed_callbacks_are_rejected(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        ctx = app.state.ctx
        record = client.portal.call(  # type: ignore[union-attr]
            ctx.actions.request, NAME, {"target": "alpha"}, "scheduler"
        )
        wait_for(lambda: tg.of("sendMessage"))
        batch = record["batch_id"]
        good = ctx.telegram.sign("ba", batch)
        for forged in (
            f"ba:{batch}:deadbeef0000",  # made-up signature
            f"br:{batch}:{good}",  # signature of the other decision
            f"ba:{batch + 1}:{good}",  # signature of another approval
            "ba:oops",
            "garbage",
            f"ba:{batch}:",
        ):
            tg.feed(press(forged, 1001))
        wait_for(lambda: len(tg.of("answerCallbackQuery")) == 6)
        assert [b["text"] for b in tg.of("answerCallbackQuery")] == ["Invalid button."] * 6
        assert client.get("/api/approvals").json()["items"][0]["status"] == "pending"
        assert fake_action.calls == [] and tg.of("editMessageText") == []
        # A press with a valid signature from a chat that is not allowed is ignored too.
        tg.feed(press(f"ba:{batch}:{good}", 1, chat=STRANGER))
        settle()
        assert len(tg.of("answerCallbackQuery")) == 6


def test_decisions_made_elsewhere_are_reflected_in_the_message(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        record = client.portal.call(  # type: ignore[union-attr]
            app.state.ctx.actions.request, NAME, {"target": "alpha"}, "scheduler"
        )
        wait_for(lambda: tg.of("sendMessage"))
        client.post(f"/api/approvals/{record['batch_id']}", json={"decision": "reject"})  # web UI
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert "Rejected by ui: 1 rejected" in edit["text"] and edit["message_id"] == 1001
        assert fake_action.calls == []


def test_one_message_per_run_with_approve_all_reject_all_and_review(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    fake_action.cap = 10  # the hourly cap applies when the items are executed
    app = make_app(bootstrap, tg, telegram={"web_url": "https://asm.example.test/"})
    with running(app, tg) as client:
        ctx = app.state.ctx
        assert len(file_run(client, app, 40)) == 40
        sent = wait_for(lambda: tg.of("sendMessage"))
        time.sleep(0.3)
        assert len(tg.of("sendMessage")) == 1  # 40 items, ONE message

        text = sent[0]["text"]
        assert text.startswith("Approval needed #1\nNightly run: fake_write × 40")
        assert marked(sent[0], "bold") == ["Approval needed #1"]
        assert text.count("• ") == 10 and "+30 more" in text and "asked by: scheduler" in text
        rows = sent[0]["reply_markup"]["inline_keyboard"]
        approve, reject = rows[0]
        assert (approve["text"], reject["text"]) == ("✅ Approve all", "❌ Reject all")
        assert rows[1] == [{"text": "🔍 Review", "url": "https://asm.example.test/#/approvals/1"}]
        assert all(len(b["callback_data"]) <= 64 for b in rows[0])
        assert fake_action.calls == []

        tg.feed(press(approve["callback_data"], 1001))
        # The default hourly cap of 10 applies when the items are executed: the rest of the
        # approved items waits in the queue (nobody is asked again) and the message says so.
        outcome = "Approved by @alice: 10 executed, 30 queued, next in ~60 min"
        wait_for(lambda: tg.of("editMessageText"))
        settle()
        edit = tg.of("editMessageText")[-1]
        assert len(fake_action.calls) == 10
        assert (
            edit["message_id"] == 1001
            and "so far: 10 executed, 30 queued, next in ~60 min" in edit["text"]
        )
        assert "reply_markup" not in edit  # nothing left to decide
        assert tg.of("answerCallbackQuery")[0]["text"] == outcome
        assert stored_messages(client, app) == 1  # still tracked: it is edited as the queue runs
        assert len(tg.of("sendMessage")) == 1  # and never a second message

        # As the hourly cap frees up the queue runs and the same message follows along.
        clock = {"shift": 0.0}
        ctx.actions._clock = lambda: time.time() + clock["shift"]
        for hour in (1, 2, 3):
            clock["shift"] = hour * 3700.0
            client.portal.call(ctx.actions.drain_queue)  # type: ignore[union-attr]
        wait_for(lambda: "Approved by @alice: 40 executed" in tg.of("editMessageText")[-1]["text"])
        assert len(fake_action.calls) == 40
        wait_for(lambda: stored_messages(client, app) == 0)
        assert len(tg.of("sendMessage")) == 1
        assert client.get("/api/approvals").json()["batches"] == []


def test_a_partial_decision_in_the_web_ui_updates_the_message_and_keeps_the_buttons(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        file_run(client, app, 5)
        wait_for(lambda: tg.of("sendMessage"))
        [batch] = client.get("/api/approvals").json()["batches"]
        ids = [i["id"] for i in batch["items"]]
        client.post(f"/api/approvals/{batch['id']}", json={"decision": "approve", "items": ids[:2]})
        edit = wait_for(lambda: tg.of("editMessageText"))[-1]
        assert "so far: 2 executed" in edit["text"] and edit["text"].count("• ") == 3
        keyboard = edit["reply_markup"]["inline_keyboard"][0]
        assert keyboard[0]["text"] == "✅ Approve all"  # applies to what is still pending
        assert stored_messages(client, app) == 1  # still tracked
        # Pressing it decides only the three that are left.
        tg.feed(press(keyboard[0]["callback_data"], 1001))
        wait_for(lambda: "Approved by @alice: 5 executed" in tg.of("editMessageText")[-1]["text"])
        assert len(fake_action.calls) == 5
        wait_for(lambda: stored_messages(client, app) == 0)


def test_requests_close_together_from_one_run_are_merged_into_one_message(
    bootstrap: Bootstrap, monkeypatch: pytest.MonkeyPatch, fake_action: FakeAction
) -> None:
    monkeypatch.setattr(telegram_module, "APPROVAL_DEBOUNCE", 0.6)
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        service = app.state.ctx.actions

        def request(n: int, run: str = "turn:1") -> dict[str, Any]:
            return client.portal.call(  # type: ignore[union-attr,no-any-return]
                lambda: service.request(
                    NAME,
                    {"target": f"t{n}", "title": f"Item {n}"},
                    f"telegram:{CHAT}",
                    run_key=run,
                    label="Chat",
                )
            )

        first, second, third = request(1), request(2), request(3)
        assert first["batch_id"] == second["batch_id"] == third["batch_id"]
        time.sleep(0.2)
        assert tg.of("sendMessage") == []  # still inside the window
        sent = wait_for(lambda: tg.of("sendMessage"), timeout=3)
        time.sleep(0.3)
        assert len(tg.of("sendMessage")) == 1 and "fake_write × 3" in sent[0]["text"]
        assert sent[0]["text"].count("• ") == 3

        # A request of the same turn after the message went out edits it, no second message.
        request(4)
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert "fake_write × 4" in edit["text"] and edit["message_id"] == 1001
        assert len(tg.of("sendMessage")) == 1

        # Decided inside the window: nothing is sent at all.
        other = request(5, run="turn:2")
        client.post(f"/api/approvals/{other['batch_id']}", json={"decision": "reject"})
        time.sleep(1.0)
        assert len(tg.of("sendMessage")) == 1


def test_buttons_of_messages_sent_before_batching_still_decide_their_item(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        ctx = app.state.ctx
        record = _request_approval(client, app, tg)
        other = client.portal.call(  # type: ignore[union-attr]
            lambda: ctx.actions.request(
                NAME, {"target": "beta"}, "scheduler", run_key="r", label="x"
            )
        )
        old_style = f"a:{record['id']}:{ctx.telegram.sign('a', record['id'])}"  # per-item button
        tg.feed(press(old_style, 1001))
        wait_for(lambda: client.get("/api/actions?status=executed").json()["items"])
        assert client.get("/api/actions?status=executed").json()["items"][0]["id"] == record["id"]
        assert (client.portal.call(ctx.actions.get, other["id"]))["status"] == "pending"


def test_approvals_command_shows_one_message_per_batch(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        file_run(client, app, 6)
        wait_for(lambda: tg.of("sendMessage"))
        texts = ask(client, tg, message("/approvals"), 2)
        assert len(texts) == 2 and "fake_write × 6" in texts[1]


def test_approval_messages_are_edited_after_a_restart(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    first = make_app(bootstrap, tg)
    with running(first, tg) as client:
        record = _request_approval(client, first, tg)
    del first  # the restart: a new app, a new bot, the same database
    second = make_app(bootstrap, tg)
    with running(second, tg) as client:
        assert not tg.of("editMessageText")  # still pending: nothing to catch up
        client.post(f"/api/approvals/{record['batch_id']}", json={"decision": "reject"})
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert "Rejected by ui" in edit["text"] and edit["message_id"] == 1001
        wait_for(lambda: stored_messages(client, second) == 0)


def test_approvals_decided_while_the_bot_was_down_are_edited_on_start(
    bootstrap: Bootstrap, fake_action: FakeAction
) -> None:
    tg = FakeTelegram()
    first = make_app(bootstrap, tg)
    with running(first, tg) as client:
        record = _request_approval(client, first, tg)
    conn = sqlite3.connect(bootstrap.db_path)
    conn.execute("UPDATE actions SET status = 'expired' WHERE id = ?", (record["id"],))
    conn.commit()
    conn.close()
    second = make_app(bootstrap, tg)
    with running(second, tg) as client:
        edit = wait_for(lambda: tg.of("editMessageText"))[0]
        assert "expired" in edit["text"] and edit["message_id"] == 1001
        assert "reply_markup" not in edit
        wait_for(lambda: stored_messages(client, second) == 0)


# --------------------------------------------------------------------------- commands


def test_commands_help_approvals_and_unknown(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        texts = ask(client, tg, message("/help"), 1)
        texts = ask(client, tg, message("/approvals"), 2)
        texts = ask(client, tg, message("/nope"), 3)
        texts = ask(client, tg, message("/report"), 4)
        settle()
    assert "/approvals" in texts[0] and "/id" in texts[0] and "/report" not in texts[0]
    assert "Nothing is waiting" in texts[1]
    assert all("Unknown command" in t for t in texts[2:]) and len(texts) == 4


def test_new_command_starts_a_fresh_session_and_keeps_the_old_one(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("one"), say("two"))
    app = make_app(bootstrap, tg, llm=llm)
    with running(app, tg) as client:
        ask(client, tg, message("first topic"), 1)
        ask(client, tg, message("/new"), 2)
        assert "Fresh chat" in tg.texts()[-1]
        ask(client, tg, message("second topic"), 3)
        assert client.get("/api/chat/sessions").json()["total"] == 2  # the old one stays listed
        replay = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] != "system"]
        assert replay == ["second topic"]  # the model does not see the old chat
        tg.feed(message("/new", chat=STRANGER))
        settle()
        assert len(tg.texts()) == 3  # a stranger cannot reset anybody's chat


# --------------------------------------------------------------------------- chat


def test_free_text_goes_to_the_chat_engine_one_session_per_telegram_chat(
    bootstrap: Bootstrap,
) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("The server is **fine**."), say("Still fine."))
    app = make_app(bootstrap, tg, llm=llm)
    with running(app, tg) as client:
        ask(client, tg, message("how is the server?"), 1)
        ask(client, tg, message("and now?"), 2)
        assert tg.texts() == ["The server is fine.", "Still fine."]
        sessions = client.get("/api/chat/sessions").json()
        assert sessions["total"] == 1 and sessions["items"][0]["channel"] == "telegram"
        # The second question saw the first exchange.
        replay = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] != "system"]
        assert replay[:2] == ["how is the server?", "The server is **fine**."]
        first = tg.of("sendMessage")[0]
        assert marked(first, "bold") == ["fine"] and "parse_mode" not in first


def test_a_message_telegram_refuses_is_sent_again_as_plain_text_and_logged(
    bootstrap: Bootstrap, caplog: pytest.LogCaptureFixture
) -> None:
    tg = FakeTelegram()
    tg.refuse_entities = True
    app = make_app(bootstrap, tg, llm=FakeLLM(say("The server is **fine**.")))
    log_name = "ai_server_monitor.outputs.telegram"
    with caplog.at_level(logging.WARNING, logger=log_name), running(app, tg) as client:
        ask(client, tg, message("how is the server?"), 2)
        settle()
    refused, plain = tg.of("sendMessage")
    assert refused["text"] == plain["text"] == "The server is fine."
    assert refused["entities"] and "entities" not in plain and "parse_mode" not in plain
    [record] = [r for r in caplog.records if "refused a message" in r.getMessage()]
    assert "can't parse entities" in record.getMessage().lower()
    assert (
        "'The server is fine.'" in record.getMessage() and '"type": "bold"' in record.getMessage()
    )


def _age_sessions(client: Any, ctx: Any, seconds: float) -> None:
    async def run() -> None:
        await ctx.db.execute("UPDATE chat_sessions SET updated = updated - ?", (seconds,))

    client.portal.call(run)


def test_chat_starts_a_new_session_after_a_quiet_gap(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("a"), say("b"), say("c"))
    app = make_app(bootstrap, tg, llm=llm, telegram={"new_session_after": "6h"})
    with running(app, tg) as client:
        ctx = app.state.ctx
        ask(client, tg, message("morning question"), 1)
        _age_sessions(client, ctx, 3600)  # one hour: still the same chat
        ask(client, tg, message("follow up"), 2)
        assert client.get("/api/chat/sessions").json()["total"] == 1
        _age_sessions(client, ctx, 7 * 3600)  # now quiet for longer than 6 hours
        ask(client, tg, message("evening question"), 3)
        assert client.get("/api/chat/sessions").json()["total"] == 2
        replay = [m["content"] for m in llm.requests[-1]["messages"] if m["role"] != "system"]
        assert replay == ["evening question"]


def test_new_session_after_zero_never_starts_a_new_session(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    llm = FakeLLM(say("a"), say("b"))
    app = make_app(bootstrap, tg, llm=llm, telegram={"new_session_after": "0"})
    with running(app, tg) as client:
        ctx = app.state.ctx
        ask(client, tg, message("one"), 1)
        _age_sessions(client, ctx, 30 * 86400)
        ask(client, tg, message("two"), 2)
        assert client.get("/api/chat/sessions").json()["total"] == 1


def test_chat_failure_is_reported_in_the_chat(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)  # no LLM configured
    with running(app, tg) as client:
        texts = ask(client, tg, message("hello?"), 1)
    assert "not configured" in texts[0] and not texts[0].startswith("⚠")  # plain words


# --------------------------------------------------------------------------- jobs


def run_job(client: TestClient, slug: str) -> int:
    """Run a job through the API and wait for its end (and for the bot to react to it)."""
    run_id = client.post(f"/api/jobs/{slug}/run").json()["run_id"]
    wait_for(lambda: client.get(f"/api/jobs/runs/{run_id}").json()["status"] != "running")
    settle()
    return int(run_id)


def test_summary_changed_sends_first_then_only_when_the_picture_changes(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [
        finish("warn", "Disk is filling up.\nMore text.", [FINDING]),
        finish("warn", "Disk is filling up (still).", [FINDING]),  # same status and findings
        finish("warn", "Disk is filling up.", [FINDING, {"severity": "info", "title": "note"}]),
        finish("crit", "Disk is full.", [{**FINDING, "severity": "crit"}]),  # severity changed
        finish("crit", "Disk is full.", [{**FINDING, "severity": "crit", "title": "<b>x</b>"}]),
    ]
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)  # send_summary defaults to "changed"
    with running(app, tg) as client:
        run_job(client, "summary")
        [first] = tg.texts()
        assert first.startswith("⚠️ Warning — Disk is filling up.")
        assert marked(tg.of("sendMessage")[0], "bold") == ["Warning"]
        assert "/ is 88% full" in first and "12 GB free" in first and "More text" not in first
        run_job(client, "summary")
        assert len(tg.texts()) == 1  # identical picture: nothing new
        run_job(client, "summary")
        assert len(tg.texts()) == 1  # only an info finding was added: not worth a message
        run_job(client, "summary")
        assert len(tg.texts()) == 2 and tg.texts()[1].startswith("🔴 Critical")
        run_job(client, "summary")
        last = tg.texts()[2]
        # HTML in the model's text is dropped by telegramify-markdown (its text stays)
        assert "• 🔴 x — 12 GB free" in last and "<b>" not in last
        assert marked(tg.of("sendMessage")[-1], "bold") == ["Critical"]


def test_summary_changed_does_not_resend_after_a_restart(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [finish("warn", "Disk.", [FINDING]), finish("warn", "Disk.", [FINDING])]
    tg = FakeTelegram()
    first = make_app(bootstrap, tg)
    with running(first, tg) as client:
        run_job(client, "summary")
    assert len(tg.texts()) == 1
    del first
    second = make_app(bootstrap, tg)
    with running(second, tg) as client:
        run_job(client, "summary")
    assert len(tg.texts()) == 1


def test_summary_every_sends_each_run_and_only_summaries(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [
        finish("ok", "Quiet."),
        finish("ok", "Quiet."),
        finish("ok", "Quiet."),
    ]
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, telegram={"send_summary": "every"})
    with running(app, tg) as client:
        run_job(client, "summary")
        run_job(client, "summary")
        assert len(tg.texts()) == 2 and tg.texts()[0].startswith("✅ OK — Quiet.")
        run_job(client, "storage")  # a topic job is not a summary
        assert len(tg.texts()) == 2


def test_summary_never_sends_nothing_but_the_command_still_answers(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [finish("ok", "Quiet."), finish("ok", "Asked for.")]
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, telegram={"send_summary": "never"})
    with running(app, tg) as client:
        run_job(client, "summary")
        assert tg.texts() == []
        texts = ask(client, tg, message("/summary"), 2)
    assert "Running" in texts[0] and "Asked for." in texts[1]


def test_summary_command_runs_it_and_replies_here_others_get_the_change(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [finish("warn", "Disk is filling up.", [FINDING])]
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, telegram={"allowed_chat_ids": [CHAT, OTHER]})
    with running(app, tg) as client:
        tg.feed(message("/summary"))
        wait_for(lambda: len(tg.texts(CHAT)) >= 2 and tg.texts(OTHER))
        settle()
        asker = tg.texts(CHAT)
        assert "Running" in asker[0] and len(asker) == 2  # no second copy for the asker
        assert "Disk is filling up." in asker[1] and "/ is 88% full" in asker[1]
        assert tg.texts(OTHER)[0].startswith("⚠️ Warning")
        assert len(tg.texts(OTHER)) == 1
        # the asker's run counts as seen: the same picture later is not news
        models.script = [finish("warn", "Disk is filling up.", [FINDING])]
        run_job(client, "summary")
    assert len(tg.texts(OTHER)) == 1


def test_run_command_runs_a_job_and_replies_with_its_result(
    bootstrap: Bootstrap, models: ScriptedModels, shell: list[str]
) -> None:
    models.script = [
        tool("bash", command="df -h /"),
        finish("warn", "Root is 88% full.", [FINDING]),
    ]
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        tg.feed(message("/run storage"))
        wait_for(lambda: len(tg.texts()) >= 2)
        running_text, result = tg.texts()[:2]
        assert running_text.startswith("Running <b>")
        assert result.startswith("⚠️ Storage — Warning")
        assert marked(tg.of("sendMessage")[1], "bold") == ["Storage"]
        assert "Root is 88% full." in result and "/ is 88% full" in result
        runs = client.get("/api/jobs/storage/runs").json()["items"]
        assert len(runs) == 1 and runs[0]["trigger"] == "manual"
        settle()
        assert len(tg.texts()) == 2  # a topic result is not broadcast on top


def test_run_with_an_unknown_job_lists_the_valid_ones(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        texts = ask(client, tg, message("/run nope"), 1)
        texts = ask(client, tg, message("/run"), 2)
        assert not client.get("/api/jobs/storage/runs").json()["items"]  # nothing was started
    for text in texts:
        for slug in ("storage", "journal", "summary", "updates"):
            assert slug in text


def test_status_command_shows_the_headline_and_one_line_per_job(
    bootstrap: Bootstrap, models: ScriptedModels
) -> None:
    models.script = [
        tool("bash", command="df -h /"),
        finish("warn", "Root is 88% full.", [FINDING]),
        finish("crit", "Server in trouble."),
    ]
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, telegram={"send_summary": "never"})
    with running(app, tg) as client:
        names = {j["slug"]: j["name"] for j in client.get("/api/jobs").json()["items"]}
        run_job(client, "storage")
        run_job(client, "summary")
        text = ask(client, tg, message("/status"), 1)[0]
    lines = text.splitlines()
    assert lines[0].startswith("🔴 Critical — Server in trouble.")
    job_lines = [ln for ln in lines if ln.startswith("• ")]
    assert len(job_lines) == len(names)
    storage = next(ln for ln in job_lines if names["storage"] in ln)
    assert "Warning" in storage and "just now" in storage
    assert "never ran" in next(ln for ln in job_lines if names["journal"] in ln)


def test_status_command_before_any_summary(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        text = ask(client, tg, message("/status"), 1)[0]
    assert text.startswith("No summary yet.") and text.count("never ran") == 7


def test_jobs_command_lists_origin_state_and_next_run(bootstrap: Bootstrap) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg)
    with running(app, tg) as client:
        client.put("/api/jobs/updates", json={"enabled": False})
        made = client.post(
            "/api/jobs",
            json={
                "slug": "mine",
                "name": "My check",
                "prompt": "Look at the backup folder.",
                "interval_s": 1800,
            },
        )
        assert made.status_code == 201
        text = ask(client, tg, message("/jobs"), 1)[0]
    lines = text.splitlines()
    assert len(lines) == 8
    storage = next(ln for ln in lines if "<code>storage</code>" in ln)
    assert "Builtin" in storage and "next run in" in storage
    assert "Builtin, off" in next(ln for ln in lines if "<code>updates</code>" in ln)
    mine = next(ln for ln in lines if "<code>mine</code>" in ln)
    assert "<b>My check</b>" in mine and "Custom, by user" in mine and "in 30 min" in mine


def test_a_command_approved_later_continues_in_the_telegram_chat(
    bootstrap: Bootstrap, models: ScriptedModels, root_shell: list[str]
) -> None:
    tg = FakeTelegram()
    app = make_app(bootstrap, tg, llm=FakeLLM())
    with running(app, tg) as client:
        models.script = [tool("bash_write", command="systemctl restart nginx", reason="hangs")]
        tg.feed(message("restart nginx please"))
        wait_for(lambda: any("Waiting for approval" in t for t in tg.texts()))
        assert root_shell == []
        [batch] = client.get("/api/approvals").json()["items"]
        models.script = ["Restarted, nginx is up."]
        client.post(f"/api/approvals/{batch['id']}", json={"decision": "approve"})
        wait_for(lambda: "Restarted, nginx is up." in tg.texts())
        assert root_shell == ["systemctl restart nginx"]
