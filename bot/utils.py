from __future__ import annotations

import html
import logging
import re

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LEN = 4096

# Telegram HTML subset we actually emit.
_TAG_RE = re.compile(r"</?([a-zA-Z0-9]+)(?:\s[^>]*)?>")


def esc(value: object) -> str:
    """HTML-escape any cell value so it never breaks Telegram HTML parsing."""
    return html.escape(str(value)) if value is not None else ""


def clamp(text: str, limit: int = TELEGRAM_MAX_LEN) -> str:
    """Truncate text without leaving broken Telegram HTML tags.

    A naive ``text[:4095] + "…"`` often cuts inside ``<code>…</code>`` and
    Telegram then rejects the whole message.
    """
    if len(text) <= limit:
        return text

    # Reserve space for ellipsis and a few closing tags.
    budget = max(16, limit - 24)
    cut = text[:budget]

    # Drop a trailing incomplete tag: "...<co" / "...<code"
    last_lt = cut.rfind("<")
    last_gt = cut.rfind(">")
    if last_lt > last_gt:
        cut = cut[:last_lt]

    stack: list[str] = []
    for match in _TAG_RE.finditer(cut):
        name = match.group(1).lower()
        raw = match.group(0)
        if raw.startswith("</"):
            if stack and stack[-1] == name:
                stack.pop()
            continue
        if raw.endswith("/>"):
            continue
        stack.append(name)

    cut = cut.rstrip() + "…"
    while stack:
        cut += f"</{stack.pop()}>"

    if len(cut) <= limit:
        return cut

    # Closing tags overflowed the limit — fall back to plain text.
    plain = re.sub(r"<[^>]+>", "", text)
    if len(plain) <= limit:
        return plain
    return plain[: limit - 1] + "…"


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
    body = clamp(text)
    try:
        await message.edit_text(
            body,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        logger.debug("edit_text failed (%s), sending new message", exc)
        try:
            await message.answer(
                body,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except TelegramBadRequest:
            # Last resort: strip tags so a bad HTML payload still reaches the user.
            plain = re.sub(r"<[^>]+>", "", body)
            try:
                await message.answer(
                    clamp(plain),
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                    parse_mode=None,
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

    body = clamp(text)
    try:
        await bot.send_message(
            chat_id,
            body,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        try:
            await bot.send_message(
                chat_id,
                body,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
            return True
        except Exception:
            logger.exception("Retry send failed for chat %s", chat_id)
            return False
    except TelegramBadRequest as exc:
        if "parse entities" in str(exc).lower():
            plain = re.sub(r"<[^>]+>", "", body)
            try:
                await bot.send_message(
                    chat_id,
                    clamp(plain),
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview,
                    parse_mode=None,
                )
                return True
            except Exception:
                logger.exception("Plain-text fallback failed for chat %s", chat_id)
                return False
        logger.warning("Cannot deliver to chat %s: %s", chat_id, exc)
        return False
    except Exception:
        logger.exception("Unexpected send error for chat %s", chat_id)
        return False
