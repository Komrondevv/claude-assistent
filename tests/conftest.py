"""Shared test helpers. Nothing here touches the network."""
from __future__ import annotations

import inspect
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetBusinessConnection, SendMessage
from aiogram.types import BusinessConnection, Chat, Message, Update

from assistant.config import load_settings
from assistant.handlers import build_router
from assistant.service import Outcome, build_assistant

OWNER_ID = 1001
OTHER_ID = 2002
CONNECTION_ID = "conn-1"
FAKE_DATE = 1_700_000_000


def assert_private_file(path: Path, expected: int = 0o600) -> None:
    """POSIX modes do not exist on Windows, so the check only applies elsewhere."""
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == expected


def connection_payload(
    connection_id: str = CONNECTION_ID,
    owner_id: int = OWNER_ID,
    enabled: bool = True,
    can_reply: bool = True,
) -> Dict[str, Any]:
    return {
        "id": connection_id,
        "user": {"id": owner_id, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": owner_id,
        "date": FAKE_DATE,
        "is_enabled": enabled,
        "can_reply": can_reply,
        "rights": {"can_reply": can_reply},
    }


class RecordingSession(BaseSession):
    """Answers Bot API calls from memory and records them."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[Any] = []
        self.connections: Dict[str, BusinessConnection] = {}
        self._next_message_id = 5000

    async def close(self) -> None:
        return None

    async def make_request(self, bot: Bot, method: Any, timeout: Optional[int] = None) -> Any:
        self.requests.append(method)
        if isinstance(method, GetBusinessConnection):
            connection = self.connections.get(method.business_connection_id)
            if connection is None:
                raise TelegramBadRequest(method=method, message="Bad Request: connection not found")
            return connection
        if isinstance(method, SendMessage):
            self._next_message_id += 1
            return Message(
                message_id=self._next_message_id,
                date=datetime.fromtimestamp(FAKE_DATE, timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        return True

    async def stream_content(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("stream_content must not be used in tests")
        yield b""  # pragma: no cover


class FakeGenerator:
    """Plays back replies or exceptions and records every call."""

    def __init__(self) -> None:
        self.replies: List[Any] = []
        self.calls: List[Any] = []
        self.hook: Optional[Callable[[], Any]] = None
        self.default = "Salom, hozir bandman"

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self.hook is not None:
            result = self.hook()
            if inspect.isawaitable(result):
                await result
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, BaseException):
            raise reply
        return reply


class Clock:
    def __init__(self, now: float = 1_760_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Harness:
    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.tmp_path = tmp_path
        env_file = tmp_path / ".env"
        env_file.write_text("", encoding="utf-8")
        profile = tmp_path / "profile.md"
        profile.write_text("<!-- hidden note -->\n# Men haqimda\n- Ism: Test Egasi\n", encoding="utf-8")
        values = {
            "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
            "OWNER_ID": str(OWNER_ID),
            "DATA_DIR": str(tmp_path / "data"),
            "PROFILE_PATH": str(profile),
            "CLAUDE_BIN": "claude-not-used",
        }
        values.update({key: str(value) for key, value in overrides.items()})
        self.settings = load_settings(env_file, overrides=values)
        self.session = RecordingSession()
        self.bot = Bot(token="123456:TEST-TOKEN", session=self.session)
        self.generator = FakeGenerator()
        self.clock = Clock()
        self.previews: List[Any] = []
        self._update_id = 0
        self._message_id = 0
        self._build()

    def _build(self) -> None:
        self.assistant = build_assistant(
            self.settings,
            generator=self.generator,
            clock=self.clock,
            preview_sink=self.previews.append,
        )
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(build_router(self.assistant))

    def restart(self) -> None:
        self._build()

    @property
    def sent_messages(self) -> List[SendMessage]:
        return [request for request in self.session.requests if isinstance(request, SendMessage)]

    @property
    def lookups(self) -> List[GetBusinessConnection]:
        return [r for r in self.session.requests if isinstance(r, GetBusinessConnection)]

    def set_connection(
        self,
        connection_id: str = CONNECTION_ID,
        owner_id: int = OWNER_ID,
        enabled: bool = True,
        can_reply: bool = True,
    ) -> None:
        """Change what Telegram would answer, without delivering an update."""
        self.session.connections[connection_id] = BusinessConnection.model_validate(
            connection_payload(connection_id, owner_id, enabled, can_reply),
            context={"bot": self.bot},
        )

    async def connect(
        self,
        connection_id: str = CONNECTION_ID,
        owner_id: int = OWNER_ID,
        enabled: bool = True,
        can_reply: bool = True,
    ) -> None:
        self.set_connection(connection_id, owner_id, enabled, can_reply)
        await self._feed(
            {"business_connection": connection_payload(connection_id, owner_id, enabled, can_reply)}
        )

    async def _feed(self, payload: Dict[str, Any]) -> None:
        self._update_id += 1
        update = Update.model_validate(
            {"update_id": self._update_id, **payload}, context={"bot": self.bot}
        )
        await self.dispatcher.feed_update(self.bot, update)

    async def business_message(
        self,
        text: Optional[str] = "Salom",
        *,
        from_id: int = OTHER_ID,
        chat_id: int = OTHER_ID,
        message_id: Optional[int] = None,
        connection_id: Optional[str] = CONNECTION_ID,
        is_bot: bool = False,
        via_business_bot: bool = False,
        first_name: str = "Ali",
        **extra: Any,
    ) -> Outcome:
        if message_id is None:
            self._message_id += 1
            message_id = self._message_id
        message: Dict[str, Any] = {
            "message_id": message_id,
            "date": FAKE_DATE,
            "chat": {"id": chat_id, "type": "private", "first_name": first_name},
            "from": {"id": from_id, "is_bot": is_bot, "first_name": first_name},
        }
        if connection_id is not None:
            message["business_connection_id"] = connection_id
        if text is not None:
            message["text"] = text
        if via_business_bot:
            message["sender_business_bot"] = {"id": 9999, "is_bot": True, "first_name": "Helper"}
        message.update(extra)
        await self._feed({"business_message": message})
        return self.assistant.outcomes[-1]

    async def direct_message(self, text: str, *, from_id: int = OWNER_ID) -> None:
        self._message_id += 1
        message: Dict[str, Any] = {
            "message_id": self._message_id,
            "date": FAKE_DATE,
            "chat": {"id": from_id, "type": "private", "first_name": "User"},
            "from": {"id": from_id, "is_bot": False, "first_name": "User"},
            "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        await self._feed({"message": message})


@pytest.fixture
def make_harness(tmp_path: Path) -> Callable[..., Harness]:
    def factory(**overrides: Any) -> Harness:
        return Harness(tmp_path, **overrides)

    return factory
