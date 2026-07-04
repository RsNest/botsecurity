from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import settings
from bot.formatters import (
    format_change,
    format_help,
    format_pending_list,
    format_reminder,
    format_status_summary,
    format_welcome,
)
from bot.monitor import RegistryMonitor
from bot.storage import Storage

logger = logging.getLogger(__name__)

RATE_LIMIT_SECONDS = 30
_last_command_at: dict[int, datetime] = defaultdict(lambda: datetime.min)


def _rate_limited(user_id: int) -> bool:
    now = datetime.now()
    last = _last_command_at[user_id]
    if (now - last).total_seconds() < RATE_LIMIT_SECONDS:
        return True
    _last_command_at[user_id] = now
    return False


async def _refresh_data(
    message: Message,
    monitor: RegistryMonitor,
    bot: Bot,
    storage: Storage,
) -> bool:
    wait = await message.answer("⏳ Загружаю актуальные данные…")
    try:
        result = await monitor.scan()
        if result.changes:
            await broadcast_changes(bot, storage, result.changes)
        await wait.delete()
        return True
    except Exception as exc:
        await wait.edit_text(
            f"❌ Не удалось загрузить таблицу:\n<code>{exc}</code>",
            parse_mode=ParseMode.HTML,
        )
        return False


async def _run_query_command(
    message: Message,
    monitor: RegistryMonitor,
    bot: Bot,
    storage: Storage,
    title: str,
    rows_fn,
) -> None:
    if message.from_user and _rate_limited(message.from_user.id):
        await message.answer("⏳ Подождите немного перед повторным запросом.")
        return
    if not await _refresh_data(message, monitor, bot, storage):
        return
    rows = rows_fn()
    text = format_pending_list(rows, title)
    await message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def setup_handlers(
    dp: Dispatcher,
    bot: Bot,
    monitor: RegistryMonitor,
    storage: Storage,
) -> None:
    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        storage.add_subscriber(message.chat.id)
        await message.answer(format_welcome(), parse_mode=ParseMode.HTML)

    @dp.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        subscribed = storage.is_subscriber(message.chat.id)
        await message.answer(
            format_help(subscribed),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @dp.message(Command("subscribe"))
    async def cmd_subscribe(message: Message) -> None:
        if storage.add_subscriber(message.chat.id):
            await message.answer("✅ Вы подписаны на уведомления.")
        else:
            await message.answer("Вы уже подписаны.")

    @dp.message(Command("unsubscribe", "stop"))
    async def cmd_unsubscribe(message: Message) -> None:
        if storage.remove_subscriber(message.chat.id):
            await message.answer("🔕 Вы отписались от уведомлений.")
        else:
            await message.answer("Вы не были подписаны.")

    @dp.message(Command("pending"))
    async def cmd_pending(message: Message) -> None:
        await _run_query_command(
            message,
            monitor,
            bot,
            storage,
            "Образы, ожидающие передачи на проверку",
            monitor.pending_rows,
        )

    @dp.message(Command("on_review"))
    async def cmd_on_review(message: Message) -> None:
        await _run_query_command(
            message,
            monitor,
            bot,
            storage,
            "Образы на проверке у ИБ",
            monitor.rows_on_review,
        )

    @dp.message(Command("failed"))
    async def cmd_failed(message: Message) -> None:
        await _run_query_command(
            message,
            monitor,
            bot,
            storage,
            "Образы, не прошедшие проверку ИБ",
            monitor.rows_failed,
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if message.from_user and _rate_limited(message.from_user.id):
            await message.answer("⏳ Подождите немного перед повторным запросом.")
            return
        if not await _refresh_data(message, monitor, bot, storage):
            return
        rows = monitor.last_rows
        summary = monitor.status_summary(rows)
        text = format_status_summary(summary, len(rows))
        await message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    @dp.message(Command("today"))
    async def cmd_today(message: Message) -> None:
        await _run_query_command(
            message,
            monitor,
            bot,
            storage,
            "Образы, добавленные сегодня",
            monitor.rows_for_today,
        )

    @dp.message(Command("by_dev"))
    async def cmd_by_dev(message: Message, command: CommandObject) -> None:
        if message.from_user and _rate_limited(message.from_user.id):
            await message.answer("⏳ Подождите немного перед повторным запросом.")
            return
        query = (command.args or "").strip()
        if not query:
            await message.answer("Использование: /by_dev Зуев")
            return
        if not await _refresh_data(message, monitor, bot, storage):
            return
        rows = monitor.rows_by_developer(query)
        text = format_pending_list(rows, f"Образы разработчика «{query}»")
        await message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    @dp.message(Command("stale"))
    async def cmd_stale(message: Message, command: CommandObject) -> None:
        if message.from_user and _rate_limited(message.from_user.id):
            await message.answer("⏳ Подождите немного перед повторным запросом.")
            return
        days_raw = (command.args or "3").strip()
        try:
            days = max(1, int(days_raw))
        except ValueError:
            await message.answer("Использование: /stale 3")
            return
        if not await _refresh_data(message, monitor, bot, storage):
            return
        rows = monitor.stale_rows(days)
        text = format_pending_list(rows, f"Образы без статуса ≥ {days} дн.")
        await message.answer(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    @dp.message(Command("sync"))
    async def cmd_sync(message: Message) -> None:
        if not message.from_user or not settings.is_admin(message.from_user.id):
            await message.answer("⛔ Команда только для администратора.")
            return
        wait = await message.answer("🔄 Синхронизация…")
        try:
            result = await monitor.scan()
            await broadcast_changes(bot, storage, result.changes)
            await wait.edit_text(
                f"✅ Синхронизация завершена.\n"
                f"Строк: {len(result.rows)}\n"
                f"Изменений: {len(result.changes)}",
            )
        except Exception as exc:
            await wait.edit_text(f"❌ Ошибка: {exc}")

    @dp.message(Command("stats"))
    async def cmd_stats(message: Message) -> None:
        if not message.from_user or not settings.is_admin(message.from_user.id):
            await message.answer("⛔ Команда только для администратора.")
            return
        last = storage.last_scan()
        lines = [
            "<b>Статистика бота</b>",
            f"Подписчиков: {storage.subscriber_count()}",
            f"Записей в кэше: {len(monitor.last_rows)}",
        ]
        if last:
            lines.extend(
                [
                    f"Последний опрос: {last['scanned_at']}",
                    f"Строк в таблице: {last['row_count']}",
                    f"Изменений: {last['changes_count']}",
                ]
            )
            if last["error"]:
                lines.append(f"Ошибка: {last['error']}")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

    @dp.message(F.text)
    async def unknown(message: Message) -> None:
        if message.text and message.text.startswith("/"):
            await message.answer("Неизвестная команда. /help")


async def broadcast_changes(bot: Bot, storage: Storage, changes) -> None:
    if not changes:
        return
    subscribers = storage.list_subscribers()
    if not subscribers:
        return
    for change in changes:
        text = format_change(change)
        for chat_id in subscribers:
            try:
                await bot.send_message(
                    chat_id,
                    text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Failed to notify chat %s", chat_id)
            await asyncio.sleep(0.05)


async def broadcast_reminder(bot: Bot, storage: Storage, monitor: RegistryMonitor) -> None:
    subscribers = storage.list_subscribers()
    if not subscribers:
        return
    try:
        await monitor.scan()
    except Exception:
        logger.exception("Reminder scan failed")
        return
    pending = monitor.pending_rows()
    if not pending:
        return
    text = format_reminder(pending)
    for chat_id in subscribers:
        try:
            await bot.send_message(
                chat_id,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Failed to send reminder to chat %s", chat_id)
        await asyncio.sleep(0.05)


async def scheduled_scan(bot: Bot, monitor: RegistryMonitor, storage: Storage) -> None:
    try:
        result = await monitor.scan()
        await broadcast_changes(bot, storage, result.changes)
    except Exception:
        logger.exception("Scheduled scan failed")


def setup_scheduler(
    bot: Bot,
    monitor: RegistryMonitor,
    storage: Storage,
) -> AsyncIOScheduler:
    tz = ZoneInfo(settings.timezone)
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        scheduled_scan,
        IntervalTrigger(minutes=settings.poll_interval_minutes),
        args=[bot, monitor, storage],
        id="poll_sheets",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    for hour in settings.reminder_hours:
        scheduler.add_job(
            broadcast_reminder,
            CronTrigger(hour=hour, minute=0, day_of_week="mon-fri"),
            args=[bot, storage, monitor],
            id=f"reminder_{hour}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    return scheduler
