"""Telegram bot: chat, approval buttons, job commands, the summary.

Built on python-telegram-bot, run inside the app's own event loop (no `run_polling()`): long
polling, no webhook, because the server sits behind a reverse proxy and nothing needs to be
reachable from outside. Only chats on the allowlist are served; everything else is ignored
without a reply. The one exception is `/id`: it answers in any chat (so the id to put on the
allowlist can be found), at most once per `ID_COOLDOWN` seconds per chat. The bot token is only
ever used to build request URLs: it is not logged, and errors never contain request URLs.

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
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, InvalidToken, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import HTTPXRequest

from ..actions import ActionError, Conflict, NotFound
from ..actions.service import outcome_text
from ..config import TelegramSettings
from ..jobs.status import headline, job_status

if TYPE_CHECKING:
    from ..context import AppContext

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and a Telegram URL contains the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

MESSAGE_LIMIT = 4096
POLL_TIMEOUT = 25
BACKOFF_MAX = 60.0
TYPING_EVERY = 4.0
APPROVAL_DEBOUNCE = 30.0
"""Seconds an approval waits before it is announced: requests of the same run that arrive in
this window end up in one message instead of several."""
MESSAGE_ITEMS = 10
"""How many items an approval message lists (the rest is "+N more")."""
SUMMARY_ITEMS = 8
"""How many warnings a summary message lists (the rest is "+N more")."""
SUMMARY_KEY = "telegram.summary_seen"
"""kv key: status and warnings of the last summary seen, so a restart does not resend it."""
STOP_GRACE = 10.0
"""Seconds a stopping bot lets running handlers finish before they are cancelled."""
HELP = (
    "<b>AI Server Monitor</b>\n"
    "Just write a question to chat about the server.\n\n"
    "/status — overall status and the last result of every job\n"
    "/jobs — the jobs and when they run next\n"
    "/run &lt;job&gt; — run one job now and show its result\n"
    "/summary — run the summary now and show it\n"
    "/approvals — actions waiting for your decision\n"
    "/new — start a fresh chat (the old one stays in the web Chat list)\n"
    "/id — this chat's id (works in any chat)\n"
    "/help — this text"
)
COMMANDS = [
    BotCommand("status", "Overall status and last job results"),
    BotCommand("jobs", "List the jobs and their next runs"),
    BotCommand("run", "Run a job now: /run <job>"),
    BotCommand("summary", "Run the summary now"),
    BotCommand("approvals", "Actions waiting for approval"),
    BotCommand("new", "Start a fresh chat"),
    BotCommand("id", "Show this chat's id"),
    BotCommand("help", "What I can do"),
]
ID_COOLDOWN = 10.0
"""Seconds between two /id answers in the same chat: it works for everybody, so it is limited."""
ICONS = {"ok": "✅", "warn": "⚠️", "crit": "🔴", "error": "❗"}
ORIGINS = {"builtin": "Builtin", "user": "Custom, by user", "llm": "Custom, by LLM"}
"""Where a job comes from, in the words of the web UI."""
WORDS = {"ok": "OK", "warn": "Warning", "crit": "Critical", "error": "Error"}


# --------------------------------------------------------------------------- formatting


def md_to_html(text: str) -> str:
    """The small Markdown subset answers use → Telegram HTML."""
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


def _one_line(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _command(text: str) -> tuple[str, str] | None:
    """("id", "") for "/ID", ("id", "mybot") for "/id@mybot args"; None for plain text."""
    if not text.startswith("/"):
        return None
    word = text.split(maxsplit=1)[0][1:]
    name, _, target = word.partition("@")
    return name.lower(), target


def _age(ts: float | None, now: float) -> str:
    if ts is None:
        return "never"
    seconds = max(now - ts, 0)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


def _in(ts: float | None, now: float) -> str:
    seconds = (ts or now) - now
    if seconds < 90:
        return "in under 2 min"
    if seconds < 5400:
        return f"in {round(seconds / 60)} min"
    if seconds < 172800:
        return f"in {round(seconds / 3600)} h"
    return f"in {round(seconds / 86400)} d"


def _outcome_line(view: dict[str, Any]) -> str:
    """ "Approved by @name: 37 executed, 2 failed, 1 deferred". The same text whichever path
    (button callback or bus event) edits the message."""
    decision = str(view.get("decision") or "")
    decided_by = str(view.get("decided_by") or "")
    by = f"@{decided_by[9:]}" if decided_by.startswith("telegram:") else decided_by
    verb = {"approve": "Approved", "reject": "Rejected", "expired": "Expired"}.get(decision, "Done")
    counts = {k: v for k, v in (view.get("counts") or {}).items() if k != "pending"}
    tail = f": {outcome_text(counts)}{view.get('queue_note') or ''}" if counts else ""
    who = f" by {by}" if by and decision != "expired" else ""
    return f"{verb}{who}{tail}"


def _attention(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The findings that need attention (warn, crit); info is left out."""
    return [f for f in run.get("findings") or [] if f.get("severity") in ("warn", "crit")]


def _finding_lines(findings: list[dict[str, Any]], limit: int) -> list[str]:
    lines = []
    for f in findings[:limit]:
        icon = ICONS["crit"] if f.get("severity") == "crit" else ICONS["warn"]
        detail = _one_line(str(f.get("detail") or ""), 120)
        lines.append(
            f"• {icon} {html.escape(_one_line(str(f.get('title') or ''), 120))}"
            + (f" — {html.escape(detail)}" if detail else "")
        )
    if len(findings) > limit:
        lines.append(f"+{len(findings) - limit} more")
    return lines


def summary_message(run: dict[str, Any]) -> str:
    """Status, headline and the warnings of a summary run (HTML)."""
    status = str(run.get("status") or "error")
    first = html.escape(_one_line(headline(run), 300))
    lines = [
        f"{ICONS.get(status, '❔')} <b>{WORDS.get(status, status)}</b>"
        + (f" — {first}" if first else "")
    ]
    attention = _attention(run)
    if attention:
        lines.append("")
        lines.extend(_finding_lines(attention, SUMMARY_ITEMS))
    return "\n".join(lines)


def run_message(name: str, run: dict[str, Any]) -> str:
    """The result of a run a person asked for: its summary text and all findings (HTML)."""
    status = str(run.get("status") or "error")
    lines = [f"{ICONS.get(status, '❔')} <b>{html.escape(name)}</b> — {WORDS.get(status, status)}"]
    text = (run.get("summary") or run.get("error") or "").strip()
    if text:
        lines += ["", md_to_html(text)]
    findings = run.get("findings") or []
    if findings:
        lines.append("")
        lines.extend(_finding_lines(findings, 15))
    return "\n".join(lines)


def summary_signature(run: dict[str, Any]) -> dict[str, Any]:
    """What "changed" compares: the overall status and the (severity, title) of the warnings."""
    return {
        "status": run.get("status"),
        "findings": sorted([f["severity"], f.get("title", "")] for f in _attention(run)),
    }


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
        self.poll_timeout = poll_timeout
        self._key = hashlib.sha256(f"asm-telegram|{token}".encode()).digest()
        self.allowed = set(cfg.allowed_chat_ids)
        self._ignored: set[int] = set()
        self._id_replied: dict[int, float] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._supervisor: asyncio.Task[None] | None = None
        self._events: asyncio.Task[None] | None = None
        self.debounce = APPROVAL_DEBOUNCE
        self._announcing: dict[int, asyncio.Task[None]] = {}
        self._own_runs: dict[int, int] = {}
        """Runs started by a command: run id → the chat that asked and gets the result."""
        self.ready = asyncio.Event()
        """Set once the bot is polling."""
        self._initialized = False
        self.ptb = self._build(token, transport)

    def _build(self, token: str, transport: httpx.AsyncBaseTransport | None) -> Application:  # type: ignore[type-arg]
        extra: dict[str, Any] = {"httpx_kwargs": {"transport": transport}} if transport else {}
        builder = (
            ApplicationBuilder()
            .token(token)
            .request(HTTPXRequest(read_timeout=20.0, write_timeout=20.0, **extra))
            .get_updates_request(
                HTTPXRequest(
                    connection_pool_size=1,
                    read_timeout=self.poll_timeout + 10.0,
                    write_timeout=20.0,
                    **extra,
                )
            )
            .concurrent_updates(True)  # a long chat answer must not hold up a button press
            .job_queue(None)  # the app has its own scheduler
        )
        base = self.cfg.api_url.strip().rstrip("/")
        if base:
            builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
        application = builder.build()
        application.add_handler(CommandHandler("id", self._id_command), group=-2)
        application.add_handler(TypeHandler(Update, self._gate), group=-1)
        for names, callback in (
            (("start", "help"), self._help),
            (("new",), self._new),
            (("approvals",), self._approvals),
            (("status",), self._status),
            (("jobs",), self._jobs),
            (("run",), self._run),
            (("summary",), self._summary),
        ):
            application.add_handler(CommandHandler(names, callback))
        application.add_handler(MessageHandler(filters.COMMAND, self._unknown))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._chat))
        application.add_handler(CallbackQueryHandler(self._on_callback))
        application.add_error_handler(self._on_error)
        return application

    @property
    def bot(self) -> Any:
        return self.ptb.bot

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Start in the background: a Telegram that cannot be reached yet must not hold up
        (or break) the app; the bot retries until it gets through."""
        if not self.allowed:
            log.warning("telegram: no allowed chat IDs are set, so the bot ignores everyone")
        self._supervisor = asyncio.create_task(self._supervise(), name="telegram-start")

    async def _supervise(self) -> None:
        delay = 1.0
        while True:
            try:
                await self.ptb.initialize()  # getMe
                break
            except InvalidToken:
                log.error("telegram: the bot token was rejected; the bot is off")
                return
            except (TelegramError, OSError) as exc:
                log.warning("telegram: cannot reach Telegram (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, BACKOFF_MAX)
        self._initialized = True
        try:
            await self.ptb.start()
            await self.ptb.updater.start_polling(  # type: ignore[union-attr]
                timeout=self.poll_timeout,
                allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
                bootstrap_retries=-1,
            )
            self._events = asyncio.create_task(self._event_loop(), name="telegram-events")
            self._spawn(self._catch_up())
            with contextlib.suppress(TelegramError):
                await self.bot.set_my_commands(COMMANDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram: could not start polling")
            return
        self.ready.set()
        log.info("telegram: bot started (%d allowed chat(s))", len(self.allowed))

    async def stop(self) -> None:
        for task in (self._supervisor, self._events):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(t for t in (self._supervisor, self._events) if t), return_exceptions=True
        )
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        ptb = self.ptb
        try:
            if ptb.updater is not None and ptb.updater.running:
                await ptb.updater.stop()
            if ptb.running:
                try:
                    async with asyncio.timeout(STOP_GRACE):
                        await ptb.stop()  # lets running handlers finish, up to the grace time
                except TimeoutError:
                    log.info("telegram: cancelled handlers that were still running")
            if self._initialized:
                await ptb.shutdown()
        except Exception:
            log.exception("telegram: stopping the bot failed")

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
            log.exception("telegram: handling an event failed")

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        if isinstance(error, TelegramError):  # network trouble, rate limits: no traceback
            log.warning("telegram: %s: %s", type(error).__name__, error)
        else:
            log.error("telegram: handling an update failed", exc_info=error)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ gate and /id

    async def _gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Only allowed chats get past this point (the /id answer comes before it)."""
        chat = update.effective_chat
        if chat is not None and chat.id in self.allowed:
            return
        command = (
            _command(update.effective_message.text or "") if update.effective_message else None
        )
        if (
            chat is not None
            and not (command and command[0] == "id")
            and chat.id not in self._ignored
        ):
            self._ignored.add(chat.id)
            log.warning("telegram: ignoring chat %s (not in the allowlist)", chat.id)
        raise ApplicationHandlerStop

    async def _id_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reply with the chat id, sender id and chat type, in any chat, rate-limited."""
        chat, message = update.effective_chat, update.effective_message
        if chat is None or message is None:
            return
        now = time.monotonic()
        last = self._id_replied.get(chat.id)
        if last is not None and now - last < ID_COOLDOWN:
            return
        if len(self._id_replied) > 1000:  # forget old entries: strangers can create many
            self._id_replied = {c: t for c, t in self._id_replied.items() if now - t < ID_COOLDOWN}
        self._id_replied[chat.id] = now
        sender = message.from_user.id if message.from_user else None
        lines = [
            f"<b>Chat id:</b> <code>{chat.id}</code>",
            f"<b>Your user id:</b> <code>{sender if sender is not None else 'unknown'}</code>",
            f"<b>Chat type:</b> {html.escape(str(chat.type or 'unknown'))}",
            "This chat is already allowed to use the bot."
            if chat.id in self.allowed
            else "Add this chat id under Settings → Outputs → Telegram → Allowed chat IDs.",
        ]
        log.info("telegram: /id answered in chat %s", chat.id)
        await self.send(chat.id, "\n".join(lines), markdown=False)

    # ------------------------------------------------------------------ commands

    async def _help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.send(update.effective_chat.id, HELP, markdown=False)  # type: ignore[union-attr]

    async def _unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        command = _command(
            (update.effective_message.text or "") if update.effective_message else ""
        )
        if command is None or command[0] == "id":
            return
        if command[1] and command[1].lower() != (self.bot.username or "").lower():
            return  # meant for another bot in the group
        await self.send(
            update.effective_chat.id,  # type: ignore[union-attr]
            "Unknown command. /help lists them.",
            markdown=False,
        )

    async def _new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id  # type: ignore[union-attr]
        await self.app.db.execute("DELETE FROM kv WHERE key = ?", (f"telegram.session.{chat_id}",))
        await self.send(
            chat_id,
            "Fresh chat started. The earlier one stays in the Chat list on the web page.",
            markdown=False,
        )

    async def _approvals(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id  # type: ignore[union-attr]
        batches = [b for b in await self.app.actions.pending_batches() if b["pending"]]
        if not batches:
            await self.send(chat_id, "Nothing is waiting for approval.", markdown=False)
        for batch in batches:
            await self._send_approval(chat_id, batch)

    async def _status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = time.time()
        data = await job_status(self.app)
        overall = data["overall"]
        if overall is None:
            lines = ["No summary yet."]
        else:
            lines = [
                f"{ICONS[overall.status]} <b>{WORDS[overall.status]}</b>"
                + (
                    f" — {html.escape(_one_line(overall.headline, 300))}"
                    if overall.headline
                    else ""
                )
                + f" ({_age(overall.finished, now)})"
            ]
        lines.append("")
        for job in data["jobs"]:
            state = (
                f"{ICONS[job.status]} {WORDS[job.status]}, {_age(job.finished, now)}"
                if job.status
                else "never ran"
            )
            off = "" if job.enabled else " (off)"
            lines.append(f"• {html.escape(job.name)}{off} — {state}")
        await self.send(update.effective_chat.id, "\n".join(lines), markdown=False)  # type: ignore[union-attr]

    async def _jobs(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        now = time.time()
        lines = []
        for job in await self.app.jobs.store.list():
            nxt = self.app.job_next_run(job["slug"])
            if not job["enabled"]:
                when = "off"
            elif nxt is None:
                when = "on, not scheduled (paused)"
            else:
                when = f"on, next run {_in(nxt, now)}"
            origin = ORIGINS.get(job.get("created_by"), "Custom")
            lines.append(
                f"• <b>{html.escape(job['name'])}</b> (<code>{html.escape(job['slug'])}</code>)"
                f" — {origin}, {when}"
            )
        await self.send(
            update.effective_chat.id,  # type: ignore[union-attr]
            "\n".join(lines) or "No jobs.",
            markdown=False,
        )

    async def _run(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id  # type: ignore[union-attr]
        jobs = {j["slug"]: j for j in await self.app.jobs.store.list()}
        wanted = (context.args[0] if context.args else "").strip().lower()
        job = jobs.get(wanted) or next(
            (j for j in jobs.values() if j["name"].lower() == wanted), None
        )
        if job is None:
            slugs = ", ".join(f"<code>{html.escape(s)}</code>" for s in jobs)
            await self.send(
                chat_id,
                f"Which job? Usage: /run &lt;job&gt;. Jobs: {slugs}",
                markdown=False,
            )
            return
        await self._run_and_reply(chat_id, job)

    async def _summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id  # type: ignore[union-attr]
        summary = next(
            (j for j in await self.app.jobs.store.list() if j["kind"] == "summary"), None
        )
        if summary is None:
            await self.send(chat_id, "There is no summary job.", markdown=False)
            return
        await self._run_and_reply(chat_id, summary)

    async def _run_and_reply(self, chat_id: int, job: dict[str, Any]) -> None:
        """Run `job` now and answer with its result here. The run is started in the
        background (it goes on if this bot is restarted meanwhile) and waited for through the
        `job.finished` event."""
        await self.send(chat_id, f"Running <b>{html.escape(job['name'])}</b>…", markdown=False)
        runner = self.app.jobs
        limit = float(job.get("timeout_s") or self.app.settings.jobs.timeout.total_seconds())
        sub = self.app.events.subscribe("job.finished")
        try:
            run_id = await runner.start(job["slug"], "manual")
            self._own_runs[run_id] = chat_id
            try:
                async with asyncio.timeout(limit * 2 + 60):
                    async for event in sub:
                        if event.data.get("run_id") == run_id:
                            break
            except TimeoutError:
                await self.send(
                    chat_id, "The run is taking much longer than allowed.", markdown=False
                )
                return
        finally:
            sub.close()
        run = await runner.store.get_run(run_id)
        if run is None:
            await self.send(chat_id, "The run is gone.", markdown=False)
            return
        await self.send(chat_id, run_message(job["name"], run), markdown=False)

    # ------------------------------------------------------------------ chat

    async def _session_for(self, chat_id: int) -> str | None:
        return await self.app.db.kv_get(f"telegram.session.{chat_id}")

    async def _chat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id  # type: ignore[union-attr]
        text = (update.effective_message.text or "").strip()  # type: ignore[union-attr]
        if not text:
            return
        engine = self.app.chat
        session_id = await self._session_for(chat_id)
        if session_id is not None:
            last = await engine.store.session_updated(session_id)
            gap = self.app.settings.outputs.telegram.new_session_after.total_seconds()
            if last is None or (gap > 0 and time.time() - last > gap):
                session_id = None  # gone, or quiet for long enough: the next message starts anew
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
                    break  # the title that may follow is stored by the engine on its own
                elif event["type"] == "error":
                    error = event["message"]
        finally:
            typing.cancel()
        await self.send(chat_id, answer or f"⚠️ {error or 'No answer.'}")

    async def _typing(self, chat_id: int) -> None:
        while True:
            with contextlib.suppress(TelegramError):
                await self.bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(TYPING_EVERY)

    # ------------------------------------------------------------------ sending

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        markdown: bool = True,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> list[Message]:
        """Send `text` (Markdown, or ready HTML with markdown=False), split to fit."""
        pieces = html_messages(text) if markdown else split_message(text)
        sent = []
        for i, piece in enumerate(pieces):
            markup = reply_markup if i == len(pieces) - 1 else None  # buttons on the last piece
            try:
                sent.append(
                    await self.bot.send_message(
                        chat_id, piece, parse_mode=ParseMode.HTML, reply_markup=markup
                    )
                )
            except BadRequest:  # unparsable markup: send it as plain text
                plain = html.unescape(re.sub(r"<[^>]+>", "", piece))
                sent.append(await self.bot.send_message(chat_id, plain, reply_markup=markup))
        return sent

    async def broadcast(self, text: str, *, markdown: bool = True, skip: int | None = None) -> None:
        for chat_id in sorted(self.allowed):
            if chat_id == skip:
                continue
            try:
                await self.send(chat_id, text, markdown=markdown)
            except TelegramError as exc:
                log.warning("telegram: could not send to chat %s: %s", chat_id, exc)

    # ------------------------------------------------------------------ bus events

    async def _event_loop(self) -> None:
        sub = self.app.events.subscribe(
            "approval.pending",
            "approval.updated",
            "approval.decided",
            "approval.expired",
            "approval.progress",
            "job.finished",
            "chat.message",
        )
        async for event in sub:
            self._spawn(self.on_event(event.type, event.data))

    async def on_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "approval.pending":
            await self._announce(int(data["id"]))
        elif kind == "approval.updated":
            if int(data["id"]) not in self._announcing:  # else the message is still to be sent
                await self._refresh(int(data["id"]))
        elif kind in ("approval.decided", "approval.expired", "approval.progress"):
            self._cancel_announcement(int(data["id"]))
            await self._reflect_decision(data)
        elif kind == "job.finished":
            await self._on_job_finished(data)
        elif kind == "chat.message":
            await self._on_chat_message(data)

    async def _on_chat_message(self, data: dict[str, Any]) -> None:
        """A chat that waited for an approval continued by itself: the answer goes to the
        Telegram chat that asked (web sessions see it on the chat page)."""
        source = str(data.get("source") or "")
        if not source.startswith("telegram:"):
            return
        try:
            chat_id = int(source.removeprefix("telegram:"))
        except ValueError:
            return
        if chat_id in self.allowed and data.get("text"):
            await self.send(chat_id, str(data["text"]))

    # ------------------------------------------------------------------ the summary

    async def _on_job_finished(self, data: dict[str, Any]) -> None:
        """A summary run ended: tell the chats, as `send_summary` says."""
        run_id = int(data["run_id"])
        asked_by = self._own_runs.pop(run_id, None)  # it answered there already
        mode = self.cfg.send_summary
        if mode == "never":
            return
        store = self.app.jobs.store
        job = await store.get(str(data.get("slug") or ""))
        run = await store.get_run(run_id)
        if job is None or run is None or job["kind"] != "summary":
            return
        signature = summary_signature(run)
        seen = await self.app.db.kv_get(SUMMARY_KEY)
        await self.app.db.kv_set(SUMMARY_KEY, signature)
        if mode == "changed" and seen == signature:
            return
        await self.broadcast(summary_message(run), markdown=False, skip=asked_by)

    # ------------------------------------------------------------------ approvals
    #
    # One message per approval *batch* (the items one run asked for), with Approve all /
    # Reject all / Review buttons. The message lists the first items and is edited with the
    # outcome; its (chat, message) is stored so that survives a restart.

    def sign(self, decision: str, batch_id: int) -> str:
        return hmac.new(self._key, f"{decision}:{batch_id}".encode(), hashlib.sha256).hexdigest()[
            :12
        ]

    def _keyboard(self, batch: dict[str, Any]) -> InlineKeyboardMarkup:
        batch_id = int(batch["id"])

        def button(label: str, decision: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                label, callback_data=f"{decision}:{batch_id}:{self.sign(decision, batch_id)}"
            )

        many = int(batch.get("pending") or 0) > 1
        rows = [
            [
                button("✅ Approve all" if many else "✅ Approve", "ba"),
                button("❌ Reject all" if many else "❌ Reject", "br"),
            ]
        ]
        web = self.cfg.web_url.strip().rstrip("/")
        if web:
            rows.append([InlineKeyboardButton("🔍 Review", url=f"{web}/#/approvals/{batch_id}")])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _batch_text(batch: dict[str, Any], *, expiry: bool = True) -> str:
        items = batch.get("items") or []
        waiting = [i for i in items if i.get("status") == "pending"]
        queued = [i for i in items if i.get("status") == "queued"]
        lines = [
            f"<b>Approval {'needed ' if waiting else ''}#{batch['id']}</b>",
            html.escape(str(batch.get("title") or "")),
            "",
        ]
        shown = waiting or queued or items
        for item in shown[:MESSAGE_ITEMS]:
            lines.append("• " + html.escape(_one_line(str(item.get("target") or ""), 90)))
        if len(shown) > MESSAGE_ITEMS:
            lines.append(f"+{len(shown) - MESSAGE_ITEMS} more")
        done = {k: v for k, v in (batch.get("counts") or {}).items() if k != "pending"}
        if (waiting or queued) and done:
            lines.append(f"so far: {outcome_text(done)}{batch.get('queue_note') or ''}")
        lines.append("")
        lines.append(f"asked by: {html.escape(str(batch.get('source') or ''))}")
        reasons = {i.get("reason") for i in waiting if i.get("reason")}
        if len(reasons) == 1:
            lines.append(f"why: {html.escape(str(next(iter(reasons))))[:300]}")
        if expiry and waiting and batch.get("expires_at"):
            minutes = max(int((batch["expires_at"] - time.time()) // 60), 0)
            lines.append(f"expires in about {minutes} min")
        return "\n".join(lines)

    async def _announce(self, batch_id: int) -> None:
        """Send the approval message of a new batch, after the debounce window."""
        if self.debounce <= 0:
            await self._announce_now(batch_id)
            return
        self._cancel_announcement(batch_id)
        task = asyncio.create_task(self._announce_later(batch_id))
        self._announcing[batch_id] = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _announce_later(self, batch_id: int) -> None:
        try:
            await asyncio.sleep(self.debounce)
            self._announcing.pop(batch_id, None)
            await self._announce_now(batch_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("telegram: could not announce approval %s", batch_id)
        finally:
            self._announcing.pop(batch_id, None)

    def _cancel_announcement(self, batch_id: int) -> None:
        task = self._announcing.pop(batch_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _announce_now(self, batch_id: int) -> None:
        try:
            batch = await self.app.actions.get_batch(batch_id)
        except NotFound:
            return
        if not batch["pending"]:
            return  # decided or expired during the debounce window
        for chat_id in sorted(self.allowed):
            await self._send_approval(chat_id, batch)

    async def _send_approval(self, chat_id: int, batch: dict[str, Any]) -> None:
        sent = await self.send(
            chat_id, self._batch_text(batch), markdown=False, reply_markup=self._keyboard(batch)
        )
        await self.app.db.execute(
            "INSERT OR REPLACE INTO telegram_batch_messages "
            "(batch_id, chat_id, message_id, created_ts) VALUES (?, ?, ?, ?)",
            (int(batch["id"]), chat_id, sent[-1].message_id, time.time()),
        )

    async def _refresh(self, batch_id: int) -> None:
        """Re-render the messages of a batch that is still open (items were added)."""
        known = await self._stored_messages(batch_id)
        if not known:
            return
        batch = await self.app.actions.get_batch(batch_id)
        if not batch["pending"]:
            return
        for chat_id, message_id in known:
            await self._edit(chat_id, message_id, self._batch_text(batch), self._keyboard(batch))

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or update.effective_chat is None:
            return
        chat_id = update.effective_chat.id
        message = query.message
        message_id = message.message_id if message is not None else None
        decision, batch_id, only_item = await self._parse_callback(query.data or "")
        if decision is None or batch_id is None:
            log.warning("telegram: rejected a callback with a bad signature from chat %s", chat_id)
            await self._answer(query, "Invalid button.")
            return
        who = query.from_user.username or str(query.from_user.id)
        try:
            view = await self.app.actions.decide_batch(
                batch_id,
                decision,
                None if only_item is None else [only_item],
                decided_by=f"telegram:{who}",
            )
        except (NotFound, Conflict, ActionError) as exc:
            note = await self._closed_note(batch_id, exc)
            await self._answer(query, note)
            original = getattr(message, "text", None)
            await self._finish_message(chat_id, message_id, original, note)
            return
        await self._answer(query, _outcome_line(view)[:190])
        await self._reflect(batch_id, view, only=(chat_id, message_id))

    async def _closed_note(self, batch_id: int, exc: Exception) -> str:
        """Why a button no longer works: expired (also approvals closed by an upgrade) or
        already decided."""
        if isinstance(exc, NotFound) or "expired" in str(exc):
            return "This approval has expired."
        try:
            counts = (await self.app.actions.get_batch(batch_id)).get("counts") or {}
        except Exception:
            return "This approval has expired."
        if set(counts) <= {"expired"}:
            return "This approval has expired."
        done = {k: v for k, v in counts.items() if k != "pending"}
        if done:
            return f"Already decided: {outcome_text(done)}"[:190]
        return str(exc)[:190]

    async def _parse_callback(self, data: str) -> tuple[str | None, int | None, int | None]:
        """(approve | reject, batch id, item id when the button is an old per-item one)."""
        parts = data.split(":")
        if len(parts) != 3 or not parts[1].isdigit():
            return None, None, None
        kind, number = parts[0], int(parts[1])
        if kind not in ("ba", "br", "a", "r"):
            return None, None, None
        if not hmac.compare_digest(parts[2], self.sign(kind, number)):
            return None, None, None
        decision = "approve" if kind in ("ba", "a") else "reject"
        if kind in ("ba", "br"):
            return decision, number, None
        # A button from before approvals were batched: `number` is an action id.
        row = await self.app.db.fetchone("SELECT batch_id FROM actions WHERE id = ?", (number,))
        if row is None or row["batch_id"] is None:
            return None, None, None
        return decision, int(row["batch_id"]), number

    async def _answer(self, query: Any, text: str) -> None:
        try:
            await query.answer(text=text)
        except TelegramError as exc:  # e.g. the button is too old to be answered
            log.info("telegram: answering a button failed: %s", exc)

    async def _finish_message(
        self, chat_id: int, message_id: int | None, original: str | None, note: str
    ) -> None:
        """Take the buttons off a card that no longer applies and say why."""
        if message_id is None:
            return
        if original:
            await self._edit(
                chat_id, message_id, f"{html.escape(original)}\n\n— {html.escape(note)}"
            )
            return
        with contextlib.suppress(TelegramError):  # too old to read its text: only the buttons go
            await self.bot.edit_message_reply_markup(chat_id, message_id, reply_markup=None)

    async def _edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            await self.bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,  # none: Telegram drops the buttons
            )
        except TelegramError as exc:
            log.info("telegram: could not edit a message: %s", exc)  # e.g. "not modified"

    async def _reflect(
        self,
        batch_id: int,
        view: dict[str, Any] | None = None,
        only: tuple[int, int | None] | None = None,
    ) -> None:
        """Edit every message of this approval with where it stands now: once nothing is
        pending any more the buttons go and the outcome is shown (and the message forgotten);
        while items still wait, the buttons stay and apply to the rest."""
        batch = await self.app.actions.get_batch(batch_id)
        known = await self._stored_messages(batch_id)
        keyboard: InlineKeyboardMarkup | None
        if batch["pending"]:
            text, keyboard = self._batch_text(batch), self._keyboard(batch)
        elif batch["queued"]:  # approved, running by itself as the hourly cap frees up
            text, keyboard = self._batch_text(batch, expiry=False), None
        else:
            text = (
                self._batch_text(batch, expiry=False)
                + "\n\n<b>"
                + html.escape(_outcome_line(view or batch))
                + "</b>"
            )
            keyboard = None
        for chat_id, message_id in known:
            await self._edit(chat_id, message_id, text, keyboard)
        if only and only[1] and (only[0], only[1]) not in known:
            await self._edit(only[0], only[1], text, keyboard)
        if not batch["pending"] and not batch["queued"]:
            await self.app.db.execute(
                "DELETE FROM telegram_batch_messages WHERE batch_id = ?", (batch_id,)
            )

    async def _stored_messages(self, batch_id: int) -> list[tuple[int, int]]:
        rows = await self.app.db.fetchall(
            "SELECT chat_id, message_id FROM telegram_batch_messages WHERE batch_id = ?",
            (batch_id,),
        )
        return [(int(r["chat_id"]), int(r["message_id"])) for r in rows]

    async def _reflect_decision(self, data: dict[str, Any]) -> None:
        batch_id = int(data["id"])
        if not await self._stored_messages(batch_id):
            return
        await self._reflect(batch_id, data)

    async def _catch_up(self) -> None:
        """Edit messages of approvals that were decided or expired while the bot was down."""
        try:
            rows = await self.app.db.fetchall(
                "SELECT DISTINCT batch_id FROM telegram_batch_messages"
            )
            for row in rows:
                batch_id = int(row["batch_id"])
                try:
                    batch = await self.app.actions.get_batch(batch_id)
                except NotFound:
                    await self.app.db.execute(
                        "DELETE FROM telegram_batch_messages WHERE batch_id = ?", (batch_id,)
                    )
                    continue
                if batch["pending"]:
                    continue
                await self._reflect(batch_id)
        except Exception:
            log.exception("telegram: could not catch up on approval messages")
