"""aiogram router: business updates plus the owner's private commands."""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BusinessConnection, Message

from .service import Assistant

ALLOWED_UPDATES = ["message", "business_connection", "business_message"]

STRANGER_START_TEXT = (
    "Bu shaxsiy yordamchi bot, u faqat egasi uchun ishlaydi. "
    "Bu yerda suhbatlashib bo'lmaydi. Iltimos, egasiga to'g'ridan-to'g'ri yozing."
)


def build_router(assistant: Assistant) -> Router:
    router = Router(name="assistant")
    owner_id = assistant.settings.owner_id
    private_chat = F.chat.type == "private"
    from_owner = F.from_user.id == owner_id

    @router.business_connection()
    async def on_connection(connection: BusinessConnection) -> None:
        await assistant.on_business_connection(connection)

    @router.business_message()
    async def on_message(message: Message, bot: Bot) -> None:
        await assistant.on_business_message(bot, message)

    @router.message(CommandStart(), private_chat)
    async def on_start(message: Message) -> None:
        user = message.from_user
        if user is not None and user.id == owner_id:
            await message.answer(assistant.start_text())
        else:
            await message.answer(STRANGER_START_TEXT)

    @router.message(Command("pause"), private_chat, from_owner)
    async def on_pause(message: Message) -> None:
        assistant.pause()
        await message.answer("Paused. No replies until /resume.")

    @router.message(Command("resume"), private_chat, from_owner)
    async def on_resume(message: Message) -> None:
        assistant.resume()
        await message.answer("Resumed. Pause, limit and takeover timers are cleared.")

    @router.message(Command("status"), private_chat, from_owner)
    async def on_status(message: Message) -> None:
        await message.answer(assistant.status_text())

    return router
