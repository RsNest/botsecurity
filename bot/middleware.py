"""Middleware that records every user interaction into the activity log."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.keyboards import REPLY_BUTTONS
from bot.storage import Storage

logger = logging.getLogger(__name__)

# Human labels for callback prefixes so /users reads nicely
_CALLBACK_LABELS = {
    "act": "меню",
    "pg": "листание",
    "pgm": "листание",
    "pgd": "листание",
    "row": "карточка образа",
    "dev": "разработчик",
    "rel": "релиз",
    "devs": "список разработчиков",
    "rels": "список релизов",
    "date": "выборка по датам",
    "rep": "отчёт ИБ",
    "menu": "меню",
}


def _classify_message(message: Message) -> tuple[str, str]:
    if message.document:
        return "архив", message.document.file_name or ""
    if message.photo:
        return "фото", ""
    text = (message.text or "").strip()
    if not text:
        return "другое", ""
    if text.startswith("/"):
        return text.split()[0].split("@")[0], text
    if text in REPLY_BUTTONS:
        return text, ""
    return "поиск", text


def _classify_callback(callback: CallbackQuery) -> tuple[str, str]:
    data = callback.data or ""
    prefix = data.split(":", 1)[0]
    label = _CALLBACK_LABELS.get(prefix, prefix)
    return f"кнопка: {label}", data


class ActivityMiddleware(BaseMiddleware):
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            self._log(event)
        except Exception:
            logger.exception("Failed to log user activity")
        return await handler(event, data)

    def _log(self, event: TelegramObject) -> None:
        if isinstance(event, Message):
            user = event.from_user
            if not user or user.is_bot:
                return
            action, detail = _classify_message(event)
        elif isinstance(event, CallbackQuery):
            user = event.from_user
            if not user or user.is_bot:
                return
            action, detail = _classify_callback(event)
        else:
            return
        self.storage.log_activity(
            user_id=user.id,
            username=user.username or "",
            full_name=user.full_name or "",
            kind="message" if isinstance(event, Message) else "callback",
            action=action,
            detail=detail,
        )
