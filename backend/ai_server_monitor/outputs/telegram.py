"""Telegram bot: reports and alerts, chat, approval buttons, a few commands.

Plain long polling against the Bot API with httpx (no webhook: the server sits behind a
reverse proxy, and nothing needs to be reachable from outside). Only chats on the allowlist
are served; everything else is ignored without a reply. The bot token is only ever used to
build request URLs: it is not logged, and errors never contain request URLs.

Messages are sent as HTML (only `&`, `<`, `>` need escaping, unlike MarkdownV2) and split
below Telegram's 4096-character limit on paragraph boundaries.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from ..actions import ActionError, Conflict, NotFound
from ..config import TelegramSettings
from ..digest import collectors_snapshot

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
MESSAGE_LIMIT = 4096
POLL_TIMEOUT = 25
BACKOFF_MAX = 60.0
KV_REPORT_COUNT = "telegram.report_count"
KV_LAST_HEALTH = "telegram.last_health"
KV_REPORT_REQUESTS = "telegram.report_requests"
TYPING_EVERY = 4.0
HELP = (
    "<b>AI Server Monitor</b>\n"
    "Just write a question to chat about the server.\n\n"
    "/status — health and open flags\n"
    "/report — write a report now\n"
    "/approvals — actions waiting for your decision\n"
    "/help — this text"
)
_HEALTH_ICON = {"ok": "🟢", "warn": "🟡", "crit": "🔴"}


class TelegramError(Exception):
    def __init__(self, message: str, code: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


# --------------------------------------------------------------------------- formatting


def md_to_html(text: str) -> str:
    """The small Markdown subset reports and answers use → Telegram HTML."""
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        heading = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if heading:
            out.append(f"<b>{_inline(heading.group(1))}</b>")
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            out.append(f"• {_inline(bullet.group(1))}")
            continue
        out.append(_inline(line))
    return "\n".join(out)


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    return re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", escaped)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Pieces of at most `limit` characters, cut at paragraph boundaries where possible
    (then at line ends, then anywhere)."""
    pieces: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        for part in _hard_split(block, limit):
            candidate = f"{current}\n\n{part}" if current else part
            if len(candidate) <= limit:
                current = candidate
            else:
                pieces.append(current)
                current = part
    if current:
        pieces.append(current)
    return pieces or [""]


def _hard_split(block: str, limit: int) -> list[str]:
    if len(block) <= limit:
        return [block]
    parts: list[str] = []
    current = ""
    for line in block.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            parts.append(current)
            current = line
    if current:
        parts.append(current)
    return parts


def html_messages(markdown: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Markdown → HTML messages within the limit. Blocks are converted one by one, so a
    split never lands inside a tag; an oversized block is split first, then converted."""
    blocks: list[str] = []
    for block in markdown.split("\n\n"):
        converted = md_to_html(block)
        if len(converted) <= limit:
            blocks.append(converted)
        else:
            blocks.extend(md_to_html(part) for part in _hard_split(block, limit // 2))
    return split_message("\n\n".join(blocks), limit) if blocks else [""]


# --------------------------------------------------------------------------- Bot API


class BotAPI:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "",
        poll_timeout: int = POLL_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base = f"{(base_url or API_URL).rstrip('/')}/bot{token}/"
        self.poll_timeout = poll_timeout
        self._http = httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(poll_timeout + 15.0, connect=10.0)
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        try:
            response = await self._http.post(self._base + method, json=params)
        except httpx.HTTPError as exc:
            # No str(exc): it can contain the URL, which contains the token.
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not body.get("ok"):
            retry = (body.get("parameters") or {}).get("retry_after")
            raise TelegramError(
                f"{method}: {body.get('description') or f'HTTP {response.status_code}'}",
                code=body.get("error_code") or response.status_code,
                retry_after=float(retry) if retry else None,
            )
        return body.get("result")


# --------------------------------------------------------------------------- the bot


class TelegramBot:
    def __init__(
        self,
        app: AppContext,
        cfg: TelegramSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_timeout: int = POLL_TIMEOUT,
    ):
        self.app = app
        self.cfg = cfg
        token = cfg.token.get_secret_value()
        self.api = BotAPI(
            token, base_url=cfg.api_url, poll_timeout=poll_timeout, transport=transport
        )
        self._key = hashlib.sha256(f"asm-telegram|{token}".encode()).digest()
        self.allowed = set(cfg.allowed_chat_ids)
        self._ignored: set[int] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._runners: list[asyncio.Task[None]] = []
        self._offset = 0
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if not self.allowed:
            log.warning("telegram: no allowed chat IDs are set, so the bot ignores everyone")
        self._runners = [
            asyncio.create_task(self._poll_loop(), name="telegram-poll"),
            asyncio.create_task(self._event_loop(), name="telegram-events"),
        ]
        self._spawn(self._catch_up())
        log.info("telegram: bot started (%d allowed chat(s))", len(self.allowed))

    async def stop(self) -> None:
        self._stopping = True
        for task in [*self._runners, *self._tasks]:
            task.cancel()
        await asyncio.gather(*self._runners, *self._tasks, return_exceptions=True)
        await self.api.close()

    # ------------------------------------------------------------------ polling

    async def _poll_loop(self) -> None:
        delay = 1.0
        try:
            await self.api.call(
                "setMyCommands",
                commands=[
                    {"command": "status", "description": "Health and open flags"},
                    {"command": "report", "description": "Write a report now"},
                    {"command": "approvals", "description": "Actions waiting for approval"},
                    {"command": "help", "description": "What I can do"},
                ],
            )
        except TelegramError as exc:
            log.info("telegram: could not register commands: %s", exc)
        while not self._stopping:
            started = time.monotonic()
            try:
                updates = await self.api.call(
                    "getUpdates",
                    offset=self._offset,
                    timeout=self.api.poll_timeout,
                    allowed_updates=["message", "callback_query"],
                )
                delay = 1.0
            except TelegramError as exc:
                wait = exc.retry_after or delay
                log.warning("telegram: polling failed (%s); retrying in %.0fs", exc, wait)
                await asyncio.sleep(wait)
                delay = min(delay * 2, BACKOFF_MAX)
                continue
            for update in updates or []:
                self._offset = max(self._offset, int(update["update_id"]) + 1)
                self._spawn(self.handle_update(update))
            if not updates and time.monotonic() - started < 0.5:
                await asyncio.sleep(0.2)  # a server that answers instantly must not spin us

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(self._guard(coro))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _guard(self, coro: Any) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram: handling an update failed")

    # ------------------------------------------------------------------ incoming

    def _allowed(self, chat_id: int) -> bool:
        if chat_id in self.allowed:
            return True
        if chat_id not in self._ignored:
            self._ignored.add(chat_id)
            log.warning("telegram: ignoring chat %s (not in the allowlist)", chat_id)
        return False

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None or not text or not self._allowed(chat_id):
            return
        if text.startswith("/"):
            command = text.split()[0][1:].split("@")[0].lower()
            await self._command(chat_id, command)
        else:
            await self._chat(chat_id, text)

    async def _command(self, chat_id: int, command: str) -> None:
        if command in ("start", "help"):
            await self.send(chat_id, HELP, markdown=False)
        elif command == "status":
            await self.send(chat_id, await self._status_text(), markdown=False)
        elif command == "report":
            if self.app.run_report_now():
                await self._add_report_request(chat_id)
                await self.send(chat_id, "Writing a report, it will arrive here.", markdown=False)
            else:
                await self.send(chat_id, "A report is already being written.", markdown=False)
        elif command == "approvals":
            pending = await self.app.actions.pending()
            if not pending:
                await self.send(chat_id, "Nothing is waiting for approval.", markdown=False)
            for record in pending:
                await self._send_approval(chat_id, record)
        else:
            await self.send(chat_id, "Unknown command. /help lists them.", markdown=False)

    async def _status_text(self) -> str:
        app = self.app
        latest = await app.reports.store.latest(status="ok")
        digest = await app.reports.digest()
        raw = collectors_snapshot(app.collectors)
        broken = [n for n, c in raw.items() if c["state"] == "active" and c["ok"] is False]
        lines = []
        if latest:
            icon = _HEALTH_ICON.get(latest["health"], "⚪")
            age = max(int(time.time() - latest["ts"]) // 60, 0)
            lines.append(
                f"{icon} {latest['health']} (score {latest['score']}), report {age} min ago"
            )
            if latest.get("summary"):
                lines.append(html.escape(latest["summary"]))
        else:
            lines.append("No report yet.")
        flags = [f for f in digest.flags if f.severity in ("crit", "warn")]
        lines += [f"[{f.severity.upper()}] {html.escape(f.title)}" for f in flags[:6]]
        if broken:
            lines.append("Collectors with errors: " + ", ".join(broken))
        pending = await app.pending_approvals_count()
        if pending:
            lines.append(f"{pending} action(s) waiting for approval (/approvals)")
        return "\n".join(lines)

    # ------------------------------------------------------------------ chat

    async def _session_for(self, chat_id: int) -> str | None:
        return await self.app.db.kv_get(f"telegram.session.{chat_id}")

    async def _chat(self, chat_id: int, text: str) -> None:
        engine = self.app.chat
        session_id = await self._session_for(chat_id)
        if session_id is not None and not await engine.store.session_exists(session_id):
            session_id = None
        typing = asyncio.create_task(self._typing(chat_id))
        answer, error = "", None
        try:
            async for event in engine.run(
                text, session_id, channel="telegram", source=f"telegram:{chat_id}"
            ):
                if event["type"] == "session":
                    await self.app.db.kv_set(f"telegram.session.{chat_id}", event["session_id"])
                elif event["type"] == "done":
                    answer = event["text"]
                elif event["type"] == "error":
                    error = event["message"]
        finally:
            typing.cancel()
        await self.send(chat_id, answer or f"⚠️ {error or 'No answer.'}")

    async def _typing(self, chat_id: int) -> None:
        while True:
            with contextlib.suppress(TelegramError):
                await self.api.call("sendChatAction", chat_id=chat_id, action="typing")
            await asyncio.sleep(TYPING_EVERY)

    # ------------------------------------------------------------------ sending

    async def send(
        self, chat_id: int, text: str, *, markdown: bool = True, **extra: Any
    ) -> list[dict[str, Any]]:
        """Send `text` (Markdown, or ready HTML with markdown=False), split to fit."""
        pieces = html_messages(text) if markdown else split_message(text)
        sent = []
        for i, piece in enumerate(pieces):
            params = {"chat_id": chat_id, "text": piece, "parse_mode": "HTML", **extra}
            if i < len(pieces) - 1:
                params.pop("reply_markup", None)  # buttons go on the last piece
            try:
                sent.append(await self.api.call("sendMessage", **params))
            except TelegramError as exc:
                if exc.code != 400:
                    raise
                params.pop("parse_mode")  # unparsable markup: send it as plain text
                params["text"] = html.unescape(re.sub(r"<[^>]+>", "", piece))
                sent.append(await self.api.call("sendMessage", **params))
        return sent

    async def broadcast(self, text: str, *, markdown: bool = True, **extra: Any) -> None:
        for chat_id in sorted(self.allowed):
            try:
                await self.send(chat_id, text, markdown=markdown, **extra)
            except TelegramError as exc:
                log.warning("telegram: could not send to chat %s: %s", chat_id, exc)

    # ------------------------------------------------------------------ bus events

    async def _event_loop(self) -> None:
        sub = self.app.events.subscribe(
            "report.done", "approval.pending", "approval.decided", "approval.expired"
        )
        async for event in sub:
            self._spawn(self.on_event(event.type, event.data))

    async def on_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "report.done":
            await self._on_report(data)
        elif kind == "approval.pending":
            for chat_id in sorted(self.allowed):
                await self._send_approval(chat_id, data)
        elif kind in ("approval.decided", "approval.expired"):
            await self._reflect_decision(data)

    async def _last_health(self) -> str | None:
        stored = await self.app.db.kv_get(KV_LAST_HEALTH)
        if stored is not None:
            return str(stored)
        latest = await self.app.reports.store.latest(status="ok")
        return latest["health"] if latest else None

    async def _take_report_requests(self) -> set[int]:
        """The chats that asked for /report and are still waiting (older asks are dropped)."""
        stored = await self.app.db.kv_get(KV_REPORT_REQUESTS)
        if stored is None:
            return set()
        await self.app.db.kv_set(KV_REPORT_REQUESTS, None)
        max_age = self.app.settings.report.interval.total_seconds()
        if time.time() - float(stored.get("ts", 0)) > max_age:
            return set()
        return {int(c) for c in stored.get("chats", [])}

    async def _add_report_request(self, chat_id: int) -> None:
        stored = await self.app.db.kv_get(KV_REPORT_REQUESTS) or {}
        chats = {int(c) for c in stored.get("chats", [])} | {chat_id}
        await self.app.db.kv_set(KV_REPORT_REQUESTS, {"ts": time.time(), "chats": sorted(chats)})

    async def _on_report(self, data: dict[str, Any]) -> None:
        count = int(await self.app.db.kv_get(KV_REPORT_COUNT, 0)) + 1
        await self.app.db.kv_set(KV_REPORT_COUNT, count)
        health = data.get("health")
        previous = await self._last_health()
        await self.app.db.kv_set(KV_LAST_HEALTH, health)
        changed = previous is not None and health != previous and self.cfg.alerts
        every = self.cfg.report_every
        full_for_all = every > 0 and count % every == 0
        requesters = await self._take_report_requests()
        report = await self.app.reports.store.get(int(data["id"])) if data.get("id") else None
        markdown = (report or {}).get("markdown") or data.get("summary") or ""
        header = ""
        if changed:
            header = (
                f"{_HEALTH_ICON.get(previous or '', '⚪')} → {_HEALTH_ICON.get(health, '⚪')} "
                f"Health changed: {previous} → {health}\n\n"
            )
        for chat_id in sorted(self.allowed):
            if full_for_all or chat_id in requesters:
                await self._guarded_send(chat_id, header + markdown)
            elif changed:
                await self._guarded_send(chat_id, header + _alert_body(data))

    async def _guarded_send(self, chat_id: int, text: str) -> None:
        try:
            await self.send(chat_id, text)
        except TelegramError as exc:
            log.warning("telegram: could not send to chat %s: %s", chat_id, exc)

    # ------------------------------------------------------------------ approvals

    def sign(self, decision: str, action_id: int) -> str:
        return hmac.new(self._key, f"{decision}:{action_id}".encode(), hashlib.sha256).hexdigest()[
            :12
        ]

    def _keyboard(self, action_id: int) -> dict[str, Any]:
        def button(label: str, decision: str) -> dict[str, str]:
            return {
                "text": label,
                "callback_data": f"{decision}:{action_id}:{self.sign(decision, action_id)}",
            }

        return {"inline_keyboard": [[button("✅ Approve", "a"), button("❌ Reject", "r")]]}

    @staticmethod
    def _approval_text(record: dict[str, Any], *, expiry: bool = True) -> str:
        lines = [
            f"<b>Approval needed #{record['id']}</b>",
            f"<code>{html.escape(str(record.get('action')))}</code> → "
            f"{html.escape(str(record.get('target') or ''))}",
            f"asked by: {html.escape(str(record.get('source') or ''))}",
        ]
        if record.get("reason"):
            lines.append(f"why: {html.escape(str(record['reason']))[:300]}")
        if expiry and record.get("expires_at"):
            minutes = max(int((record["expires_at"] - time.time()) // 60), 0)
            lines.append(f"expires in about {minutes} min")
        return "\n".join(lines)

    async def _send_approval(self, chat_id: int, record: dict[str, Any]) -> None:
        sent = await self.send(
            chat_id,
            self._approval_text(record),
            markdown=False,
            reply_markup=self._keyboard(int(record["id"])),
        )
        message_id = sent[-1]["message_id"]
        await self.app.db.execute(
            "INSERT OR REPLACE INTO telegram_approval_messages "
            "(approval_id, chat_id, message_id, created_ts) VALUES (?, ?, ?, ?)",
            (int(record["id"]), chat_id, int(message_id), time.time()),
        )

    async def _on_callback(self, query: dict[str, Any]) -> None:
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id is None or not self._allowed(chat_id):
            return
        decision, action_id = self._parse_callback(query.get("data") or "")
        if decision is None or action_id is None:
            log.warning("telegram: rejected a callback with a bad signature from chat %s", chat_id)
            await self._answer(query, "Invalid button.")
            return
        user = query.get("from") or {}
        who = user.get("username") or str(user.get("id"))
        try:
            record = await self.app.actions.decide(
                action_id, "approve" if decision == "a" else "reject", decided_by=f"telegram:{who}"
            )
        except (NotFound, Conflict, ActionError) as exc:
            await self._answer(query, str(exc)[:150])
            await self._finish_message(chat_id, message.get("message_id"), message, str(exc))
            return
        outcome = (
            "rejected" if decision == "r" else f"approved → {record['status']}"
        )  # executed | failed | dry_run | rejected (limit)
        await self._answer(query, outcome)
        await self._reflect(
            action_id, f"{outcome} by @{who}", only=(chat_id, message.get("message_id"))
        )

    def _parse_callback(self, data: str) -> tuple[str | None, int | None]:
        parts = data.split(":")
        if len(parts) != 3 or parts[0] not in ("a", "r") or not parts[1].isdigit():
            return None, None
        action_id = int(parts[1])
        if not hmac.compare_digest(parts[2], self.sign(parts[0], action_id)):
            return None, None
        return parts[0], action_id

    async def _answer(self, query: dict[str, Any], text: str) -> None:
        try:
            await self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=text)
        except TelegramError as exc:
            log.info("telegram: answerCallbackQuery failed: %s", exc)

    async def _finish_message(
        self, chat_id: int, message_id: int | None, message: dict[str, Any], note: str
    ) -> None:
        if message_id is None:
            return
        original = html.escape(message.get("text") or "")
        await self._edit(chat_id, message_id, f"{original}\n\n— {html.escape(note)}")

    async def _edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.api.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                reply_markup={"inline_keyboard": []},
            )
        except TelegramError as exc:
            log.info("telegram: could not edit a message: %s", exc)  # e.g. "not modified"

    async def _reflect(
        self, action_id: int, note: str, only: tuple[int, int | None] | None = None
    ) -> None:
        """Replace the buttons of every message for this approval with the outcome."""
        record = await self.app.actions.get(action_id)
        text = self._approval_text(record, expiry=False) + "\n\n<b>" + html.escape(note) + "</b>"
        known = await self._stored_messages(action_id)
        for chat_id, message_id in known:
            await self._edit(chat_id, message_id, text)
        if only and only[1] and (only[0], only[1]) not in known:
            await self._edit(only[0], only[1], text)
        await self.app.db.execute(
            "DELETE FROM telegram_approval_messages WHERE approval_id = ?", (action_id,)
        )

    async def _stored_messages(self, action_id: int) -> list[tuple[int, int]]:
        rows = await self.app.db.fetchall(
            "SELECT chat_id, message_id FROM telegram_approval_messages WHERE approval_id = ?",
            (action_id,),
        )
        return [(int(r["chat_id"]), int(r["message_id"])) for r in rows]

    async def _reflect_decision(self, data: dict[str, Any]) -> None:
        action_id = int(data["id"])
        if not await self._stored_messages(action_id):
            return
        decided_by = str(data.get("decided_by") or "")
        decision = data.get("decision") or data.get("status")
        await self._reflect(action_id, f"{decision} by {decided_by} ({data.get('status')})")

    async def _catch_up(self) -> None:
        """Edit messages of approvals that were decided or expired while the bot was down."""
        try:
            rows = await self.app.db.fetchall(
                "SELECT DISTINCT approval_id FROM telegram_approval_messages"
            )
            for row in rows:
                action_id = int(row["approval_id"])
                try:
                    record = await self.app.actions.get(action_id)
                except NotFound:
                    await self.app.db.execute(
                        "DELETE FROM telegram_approval_messages WHERE approval_id = ?",
                        (action_id,),
                    )
                    continue
                if record["status"] == "pending":
                    continue
                by = f" by {record['decided_by']}" if record.get("decided_by") else ""
                await self._reflect(action_id, f"{record['status']}{by}")
        except Exception:
            log.exception("telegram: could not catch up on approval messages")


def _alert_body(data: dict[str, Any]) -> str:
    lines = [str(data.get("summary") or "")]
    for finding in (data.get("findings") or [])[:6]:
        detail = f" — {finding.get('detail')}" if finding.get("detail") else ""
        lines.append(f"- [{finding.get('severity')}] **{finding.get('title')}**{detail}")
    return "\n".join(line for line in lines if line)
