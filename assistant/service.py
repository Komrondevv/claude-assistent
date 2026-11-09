"""Decides what to do with each business message and drafts or sends the reply."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Union

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message

from .claude_cli import ClaudeCLI, ClaudeError, ClaudeLimitError
from .config import Settings
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import (
    HistoryStore,
    RuntimeState,
    SeenMessages,
    append_private_line,
    ensure_private_dir,
)

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 4000
ERROR_ALERT_INTERVAL_SECONDS = 600

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class Outcome(str, Enum):
    PREVIEWED = "previewed"
    SENT = "sent"
    DUPLICATE = "duplicate"
    IGNORED_BOT = "ignored_bot"
    NO_CONNECTION = "no_connection"
    FOREIGN_CONNECTION = "foreign_connection"
    CONNECTION_DISABLED = "connection_disabled"
    UNSUPPORTED = "unsupported"
    OWNER_MESSAGE = "owner_message"
    OWNER_ACTIVE = "owner_active"
    PAUSED = "paused"
    LIMITED = "limited"
    SKIPPED = "skipped"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"


@dataclass
class Preview:
    connection_id: str
    chat_id: int
    chat_name: str
    language: Optional[str]
    incoming: str
    reply: str
    note: Optional[str] = None


def print_preview(preview: Preview) -> None:
    lines = [
        "PREVIEW, not sent",
        f"  chat:     {preview.chat_name} ({preview.chat_id})",
        f"  language: {preview.language or 'unknown'}",
        f"  incoming: {preview.incoming}",
        f"  reply:    {preview.reply}",
    ]
    if preview.note:
        lines.append(f"  note:     {preview.note}")
    logger.info("\n".join(lines))


def describe(message: Message) -> Optional[str]:
    """Return a text form of the message, or ``None`` for service messages."""
    if message.text:
        return message.text

    caption = message.caption or ""
    if message.sticker is not None:
        return f"[sticker] {message.sticker.emoji or ''}".strip()
    if message.poll is not None:
        return f"[poll] {message.poll.question}"
    if message.dice is not None:
        return f"[dice] {message.dice.emoji}"

    # Animations also carry a document, so they must be checked first.
    labels = (
        (message.voice, "[voice message]"),
        (message.video_note, "[video message]"),
        (message.photo, "[photo]"),
        (message.video, "[video]"),
        (message.animation, "[GIF]"),
        (message.audio, "[audio]"),
        (message.document, "[file]"),
        (message.contact, "[contact]"),
        (message.venue, "[venue]"),
        (message.location, "[location]"),
    )
    for content, label in labels:
        if content:
            return f"{label} {caption}".strip()
    return None


def can_reply(connection: Any) -> bool:
    rights = getattr(connection, "rights", None)
    if isinstance(rights, dict):
        return bool(rights.get("can_reply"))
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


def read_profile(path: Union[str, Path]) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    return _HTML_COMMENT_RE.sub("", text).strip()


def profile_is_filled(profile: str) -> bool:
    """True when the profile has content beyond the template's headings and empty labels."""
    for raw_line in profile.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-") and line.endswith(":"):
            continue
        return True
    return False


def _sender_name(message: Message) -> str:
    if message.from_user is not None:
        return message.from_user.full_name
    return message.chat.title or message.chat.first_name or "Contact"


def _chat_name(message: Message) -> str:
    chat = message.chat
    if chat.title:
        return chat.title
    name = " ".join(part for part in (chat.first_name, chat.last_name) if part)
    return name or str(chat.id)


class Assistant:
    def __init__(
        self,
        settings: Settings,
        generator: Any,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], None] = print_preview,
    ) -> None:
        self.settings = settings
        self.generator = generator
        self.bot_username: Optional[str] = None
        self._clock = clock
        self._preview_sink = preview_sink

        data_dir = ensure_private_dir(settings.data_dir)
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState(data_dir / "state.json")
        self.previews_path = data_dir / "previews.jsonl"

        self.outcomes: Deque[Outcome] = deque(maxlen=100)
        self._connections: Dict[str, BusinessConnection] = {}
        self._takeover_until: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_error_alert: Optional[float] = None

    async def on_business_connection(self, connection: BusinessConnection) -> None:
        self._connections[connection.id] = connection
        if connection.user.id != self.settings.owner_id:
            logger.warning(
                "Ignoring business connection %s from a foreign account: user=%s",
                connection.id,
                connection.user.id,
            )
            return
        logger.info(
            "Business connection %s: user=%s enabled=%s can_reply=%s",
            connection.id,
            connection.user.id,
            connection.is_enabled,
            can_reply(connection),
        )

    async def on_business_message(self, bot: Bot, message: Message) -> Outcome:
        outcome = await self._process(bot, message)
        self.outcomes.append(outcome)
        logger.info("Business message in chat %s: %s", message.chat.id, outcome.value)
        return outcome

    async def _process(self, bot: Bot, message: Message) -> Outcome:
        connection_id = message.business_connection_id
        if not connection_id:
            return Outcome.NO_CONNECTION

        chat_id = message.chat.id
        # Marked before the first await so that parallel deliveries cannot both pass.
        if not self.seen.first_time(f"{connection_id}:{chat_id}:{message.message_id}"):
            return Outcome.DUPLICATE

        if message.sender_business_bot is not None or (
            message.from_user is not None and message.from_user.is_bot
        ):
            return Outcome.IGNORED_BOT

        connection_outcome = await self._check_connection(bot, connection_id)
        if connection_outcome is not None:
            return connection_outcome

        text = describe(message)
        if text is None:
            return Outcome.UNSUPPORTED

        chat_key = f"{connection_id}:{chat_id}"
        now = self._clock()
        if message.from_user is not None and message.from_user.id == self.settings.owner_id:
            self.history.add(
                connection_id, chat_id,
                role="owner", name=_sender_name(message), text=text, lang=None, ts=now,
            )
            minutes = self.settings.owner_takeover_minutes
            if minutes > 0:
                self._takeover_until[chat_key] = now + minutes * 60
            return Outcome.OWNER_MESSAGE

        language = detect_language(text) or self.history.last_language(connection_id, chat_id)
        self.history.add(
            connection_id, chat_id,
            role="other", name=_sender_name(message), text=text, lang=language, ts=now,
        )

        async with self._locks.setdefault(chat_key, asyncio.Lock()):
            return await self._respond(bot, message, connection_id, chat_key, text, language)

    async def _check_connection(self, bot: Bot, connection_id: str) -> Optional[Outcome]:
        connection = self._connections.get(connection_id)
        if connection is None:
            try:
                connection = await bot.get_business_connection(
                    business_connection_id=connection_id
                )
            except TelegramAPIError as exc:
                logger.warning("Could not look up business connection: %s", exc)
                return Outcome.NO_CONNECTION
            self._connections[connection_id] = connection
        if connection.user.id != self.settings.owner_id:
            return Outcome.FOREIGN_CONNECTION
        if not connection.is_enabled:
            return Outcome.CONNECTION_DISABLED
        return None

    def _blocked(self, chat_key: str) -> Optional[Outcome]:
        now = self._clock()
        if self.state.paused:
            return Outcome.PAUSED
        limit_until = self.state.limit_until
        if limit_until is not None and limit_until > now:
            return Outcome.LIMITED
        if self._takeover_until.get(chat_key, 0) > now:
            return Outcome.OWNER_ACTIVE
        return None

    async def _respond(
        self,
        bot: Bot,
        message: Message,
        connection_id: str,
        chat_key: str,
        text: str,
        language: Optional[str],
    ) -> Outcome:
        blocked = self._blocked(chat_key)
        if blocked is not None:
            return blocked

        chat_id = message.chat.id
        current_time = datetime.fromtimestamp(self._clock(), timezone.utc).astimezone()
        system_prompt = build_system_prompt(read_profile(self.settings.profile_path), current_time)
        user_prompt = build_user_prompt(self.history.get(connection_id, chat_id), language)

        try:
            reply = await self.generator.generate(system_prompt, user_prompt)
        except ClaudeLimitError as exc:
            return await self._handle_limit(bot, exc)
        except ClaudeError as exc:
            return await self._handle_error(bot, exc)

        if not reply or not reply.strip() or SKIP_TOKEN in reply:
            return Outcome.SKIPPED
        reply = reply.strip()

        preview = Preview(
            connection_id=connection_id,
            chat_id=chat_id,
            chat_name=_chat_name(message),
            language=language,
            incoming=text,
            reply=reply,
        )
        if not self.settings.auto_send:
            self._emit_preview(preview)
            return Outcome.PREVIEWED

        return await self._send(bot, preview, chat_key)

    async def _handle_limit(self, bot: Bot, exc: ClaudeLimitError) -> Outcome:
        now = self._clock()
        if exc.resets_at and exc.resets_at > now:
            until = exc.resets_at
        else:
            until = now + self.settings.limit_cooldown_minutes * 60
        self.state.limit_until = until
        resume_at = datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        logger.warning("%s. No replies until %s.", exc, resume_at)
        if self.settings.auto_send:
            await self._alert_owner(bot, f"{exc}. Replies are on hold until {resume_at}.")
        return Outcome.LIMITED

    async def _handle_error(self, bot: Bot, exc: ClaudeError) -> Outcome:
        logger.error("Claude failed: %s", exc)
        now = self._clock()
        last_alert = self._last_error_alert
        if self.settings.auto_send and (
            last_alert is None or now - last_alert >= ERROR_ALERT_INTERVAL_SECONDS
        ):
            self._last_error_alert = now
            await self._alert_owner(bot, f"Assistant could not reply: {exc}")
        return Outcome.ERROR

    async def _alert_owner(self, bot: Bot, text: str) -> None:
        try:
            await bot.send_message(chat_id=self.settings.owner_id, text=text[:MAX_MESSAGE_LENGTH])
        except TelegramAPIError as exc:
            logger.warning("Could not alert the owner: %s", exc)

    def _emit_preview(self, preview: Preview) -> None:
        self._preview_sink(preview)
        record = {**asdict(preview), "ts": self._clock()}
        append_private_line(self.previews_path, json.dumps(record, ensure_ascii=False))

    def _hold_back(self, preview: Preview, note: str) -> None:
        preview.note = note
        self._emit_preview(preview)

    async def _send(self, bot: Bot, preview: Preview, chat_key: str) -> Outcome:
        # The owner may have written, or /pause may have arrived, during generation.
        blocked = self._blocked(chat_key)
        if blocked is not None:
            self._hold_back(preview, f"Not sent: {blocked.value} changed during generation")
            return blocked

        problem = await self._permission_problem(bot, preview.connection_id)
        if problem is not None:
            logger.warning("Reply not sent: %s", problem)
            self._hold_back(preview, f"Not sent: {problem}")
            return Outcome.PERMISSION_DENIED

        try:
            for chunk in _split_message(preview.reply):
                await bot.send_message(
                    chat_id=preview.chat_id,
                    text=chunk,
                    business_connection_id=preview.connection_id,
                )
        except TelegramAPIError as exc:
            logger.error("Could not send the reply: %s", exc)
            return Outcome.ERROR

        self.history.add(
            preview.connection_id, preview.chat_id,
            role="owner", name="You", text=preview.reply,
            lang=preview.language, ts=self._clock(),
        )
        return Outcome.SENT

    async def _permission_problem(self, bot: Bot, connection_id: str) -> Optional[str]:
        """Re-read the connection from Telegram; the cache may be out of date."""
        try:
            connection = await bot.get_business_connection(business_connection_id=connection_id)
        except TelegramAPIError as exc:
            return f"could not re-check the business connection ({exc})"
        self._connections[connection_id] = connection
        if connection.user.id != self.settings.owner_id:
            return "the business connection belongs to another account"
        if not connection.is_enabled:
            return "the business connection is disabled"
        if not can_reply(connection):
            return "the bot has no permission to reply"
        return None

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False
        self.state.limit_until = None
        self._takeover_until.clear()

    def _active_limit(self) -> Optional[str]:
        limit_until = self.state.limit_until
        if limit_until is None or limit_until <= self._clock():
            return None
        return datetime.fromtimestamp(limit_until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def status_text(self) -> str:
        mode = "AUTO-SEND" if self.settings.auto_send else "PREVIEW: replies are only shown in the logs"
        state = "paused" if self.state.paused else "running"
        limit = self._active_limit()
        lines = [
            f"Mode: {mode}",
            f"State: {state}",
            f"Claude limit: {'until ' + limit if limit else 'none'}",
            f"Chats in history: {self.history.chat_count()}",
        ]
        return "\n".join(lines)

    def start_text(self) -> str:
        username = f"@{self.bot_username}" if self.bot_username else "bot username'i"
        if self.settings.auto_send:
            mode = "AUTO-SEND: javoblar suhbatdoshga avtomatik yuboriladi"
        else:
            mode = "PREVIEW: javoblar faqat logda ko'rinadi, hech kimga yuborilmaydi"
        state = "pauzada" if self.state.paused else "ishlayapti"
        minutes = self.settings.owner_takeover_minutes
        if minutes > 0:
            takeover = f"O'zingiz chatga yozsangiz, bot o'sha chatda {minutes} daqiqa jim turadi."
        else:
            takeover = "O'zingiz chatga yozganda jim turish o'chirilgan."

        lines: List[str] = [
            "Salom! Men sizning shaxsiy Telegram yordamchingizman.",
            "",
            "Sizga kimdir yozsa, profile.md dagi ma'lumotlarga tayanib, sizning nomingizdan "
            "va uslubingizda javob tayyorlayman.",
            "",
            "Hozirgi holat:",
            f"• Rejim: {mode}",
            f"• Holat: {state}",
            "",
            "Qanday ulanadi (Telegram Premium kerak):",
            "1. Telegram → Settings → Telegram Business → Chatbots",
            f"2. {username} ni kiriting",
            "3. Bot qaysi chatlarni ko'rishini tanlang",
            "4. Xabarlarga javob berish ruxsatini yoqing",
            "",
            "Buyruqlar:",
            "/pause — javob berishni to'xtatish",
            "/resume — qayta yoqish",
            "/status — holatni ko'rish",
            "",
            "Bilib qo'ying:",
            f"• {takeover}",
            "• Pul, qarz va uchrashuvlarga rozilik bermayman, va'da ham qilmayman.",
            "• profile.md ni to'ldirish kerak, javoblar shunga qarab yoziladi.",
        ]
        if not self.settings.auto_send:
            lines.append("• Javoblar yuborilishi uchun AUTO_SEND=true qilish kerak.")
        return "\n".join(lines)


def _split_message(text: str) -> List[str]:
    return [text[i : i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]


def make_claude_cli(settings: Settings) -> ClaudeCLI:
    return ClaudeCLI(
        settings.claude_bin,
        workdir=settings.data_dir / "claude-run",
        model=settings.claude_model,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout_seconds,
        oauth_token=settings.claude_oauth_token or None,
        auth_label=settings.claude_auth_mode,
    )


def build_assistant(settings: Settings, **kwargs: Any) -> Assistant:
    generator = kwargs.pop("generator", None)
    if generator is None:
        generator = make_claude_cli(settings)
    return Assistant(settings, generator, **kwargs)
