from __future__ import annotations

import html
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096


def esc(value: object) -> str:
    """HTML-escape any cell value so it never breaks Telegram HTML parsing."""
    return html.escape(str(value)) if value is not None else ""


def clamp(text: str, limit: int = TELEGRAM_MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def safe_edit(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool = True,
) -> None:
    """Edit a message, tolerating the "message is not modified" error.

    Also falls back to sending a new message if the original can no longer be
    edited (e.g. too old).
    """
    try:
        await message.edit_text(
            clamp(text),
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        logger.debug("edit_text failed (%s), sending new message", exc)
        try:
            await message.answer(
                clamp(text),
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except TelegramBadRequest:
            logger.exception("Failed to send fallback message")


async def safe_send(
    bot,
    chat_id: int,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_web_page_preview: bool = True,
) -> bool:
    """Send a message, handling flood-control and blocked-chat errors."""
    import asyncio

    try:
        await bot.send_message(
            chat_id,
            clamp(text),
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        try:
            await bot.send_message(
                chat_id,
                clamp(text),
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
            return True
        except Exception:
            logger.exception("Retry send failed for chat %s", chat_id)
            return False
    except TelegramBadRequest as exc:
        logger.warning("Cannot deliver to chat %s: %s", chat_id, exc)
        return False
    except Exception:
        logger.exception("Unexpected send error for chat %s", chat_id)
        return False
