from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import settings
from bot.dates import (
    FIELD_LABELS,
    STATUS_LABELS,
    format_period_label,
    parse_date_range,
    period_to_range,
)
from bot.formatters import (
    format_change,
    format_help,
    format_pending_list,
    format_reminder,
    format_status_summary,
    format_welcome,
)
from bot.keyboards import (
    BTN_BY_DATE,
    BTN_FAILED,
    BTN_MENU,
    BTN_ON_REVIEW,
    BTN_PASSED,
    BTN_PENDING,
    BTN_REFRESH,
    BTN_STATUS,
    BTN_TODAY,
    REPLY_BUTTONS,
    inline_back_menu,
    inline_date_field_keyboard,
    inline_date_period_keyboard,
    inline_date_status_keyboard,
    inline_main_menu,
    main_reply_keyboard,
)
from bot.models import ImageRow
from bot.monitor import RegistryMonitor
from bot.storage import Storage

logger = logging.getLogger(__name__)

_last_force_refresh: dict[int, datetime] = defaultdict(lambda: datetime.min)


@dataclass
class DateInputState:
    date_field: str
    start: date
    end: date


_awaiting_custom_date: dict[int, str] = {}  # user_id -> date_field


def _cache_footer(monitor: RegistryMonitor) -> str:
    label = monitor.cache_age_label()
    if label:
        return f"{label} · автообновление каждые {settings.cache_ttl_seconds // 60} мин"
    return ""


def _force_refresh_blocked(user_id: int) -> int:
    elapsed = (datetime.now() - _last_force_refresh[user_id]).total_seconds()
    remaining = settings.force_refresh_cooldown - elapsed
    return max(0, int(remaining) + 1)


def _mark_force_refresh(user_id: int) -> None:
    _last_force_refresh[user_id] = datetime.now()


def _custom_date_keyboard(date_field: str, start: date, end: date) -> InlineKeyboardMarkup:
    token = f"{start.isoformat()}_{end.isoformat()}"
    prefix = f"date:c:{date_field}:{token}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Прошли", callback_data=f"{prefix}:ok")],
            [InlineKeyboardButton(text="❌ Не прошли", callback_data=f"{prefix}:fail")],
            [InlineKeyboardButton(text="📋 Все", callback_data=f"{prefix}:all")],
        ]
    )


async def _load_data(
    monitor: RegistryMonitor,
    bot: Bot,
    storage: Storage,
    *,
    force: bool = False,
    notify_changes: bool = False,
) -> tuple[bool, str | None]:
    try:
        result = await monitor.ensure_fresh(force=force)
        if notify_changes and result.changes:
            await broadcast_changes(bot, storage, result.changes)
        return True, None
    except Exception as exc:
        logger.exception("Failed to load registry data")
        return False, str(exc)


async def _send_rows(
    target: Message,
    monitor: RegistryMonitor,
    title: str,
    rows: list[ImageRow],
    *,
    edit: bool = False,
) -> None:
    text = format_pending_list(rows, title, footer=_cache_footer(monitor))
    markup = inline_back_menu()
    if edit:
        await target.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    else:
        await target.answer(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup,
        )


async def _query_rows(
    message: Message,
    monitor: RegistryMonitor,
    bot: Bot,
    storage: Storage,
    title: str,
    rows_fn: Callable[[], list[ImageRow]],
    *,
    force: bool = False,
) -> None:
    if force and message.from_user:
        remaining = _force_refresh_blocked(message.from_user.id)
        if remaining > 0:
            await message.answer(
                f"🔄 Обновление доступно через {remaining} сек.\n"
                "Пока показываю данные из кэша."
            )
            force = False
        else:
            _mark_force_refresh(message.from_user.id)

    wait = None
    if force:
        wait = await message.answer("⏳ Обновляю данные…")
    elif not monitor.last_rows:
        wait = await message.answer("⏳ Загружаю данные…")

    ok, err = await _load_data(
        monitor, bot, storage, force=force, notify_changes=force
    )
    if wait:
        await wait.delete()

    if not ok:
        await message.answer(f"❌ Не удалось загрузить таблицу:\n<code>{err}</code>")
        return

    await _send_rows(message, monitor, title, rows_fn())


async def _query_status(
    message: Message,
    monitor: RegistryMonitor,
    bot: Bot,
    storage: Storage,
    *,
    force: bool = False,
) -> None:
    if force and message.from_user:
        remaining = _force_refresh_blocked(message.from_user.id)
        if remaining > 0:
            await message.answer(
                f"🔄 Обновление доступно через {remaining} сек.\n"
                "Пока показываю данные из кэша."
            )
            force = False
        else:
            _mark_force_refresh(message.from_user.id)

    wait = None
    if force:
        wait = await message.answer("⏳ Обновляю данные…")
    elif not monitor.last_rows:
        wait = await message.answer("⏳ Загружаю данные…")

    ok, err = await _load_data(
        monitor, bot, storage, force=force, notify_changes=force
    )
    if wait:
        await wait.delete()

    if not ok:
        await message.answer(f"❌ Не удалось загрузить таблицу:\n<code>{err}</code>")
        return

    rows = monitor.last_rows
    summary = monitor.status_summary(rows)
    text = format_status_summary(summary, len(rows), footer=_cache_footer(monitor))
    await message.answer(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=inline_back_menu(),
    )


def _date_report_title(
    date_field: str,
    start: date,
    end: date,
    status_filter: str,
) -> str:
    period = format_period_label(start, end)
    field = FIELD_LABELS.get(date_field, date_field)
    status = STATUS_LABELS.get(status_filter, status_filter)
    return f"Выборка: {status} · {field} · {period}"


async def _show_date_report(
    target: Message,
    monitor: RegistryMonitor,
    bot: Bot,
    storage: Storage,
    date_field: str,
    start: date,
    end: date,
    status_filter: str,
    *,
    edit: bool = False,
) -> None:
    ok, err = await _load_data(monitor, bot, storage)
    if not ok:
        text = f"❌ Не удалось загрузить таблицу:\n<code>{err}</code>"
        if edit:
            await target.edit_text(text, parse_mode=ParseMode.HTML)
        else:
            await target.answer(text, parse_mode=ParseMode.HTML)
        return

    rows = monitor.rows_by_date_range(start, end, date_field, status_filter)
    title = _date_report_title(date_field, start, end, status_filter)
    await _send_rows(target, monitor, title, rows, edit=edit)


def setup_handlers(
    dp: Dispatcher,
    bot: Bot,
    monitor: RegistryMonitor,
    storage: Storage,
) -> None:
    @dp.message(Command("start"))
    async def cmd_start(message: Message) -> None:
        storage.add_subscriber(message.chat.id)
        await message.answer(
            format_welcome(),
            parse_mode=ParseMode.HTML,
            reply_markup=main_reply_keyboard(),
        )
        await message.answer("Быстрый доступ:", reply_markup=inline_main_menu())

    @dp.message(Command("help", "menu"))
    async def cmd_help(message: Message) -> None:
        subscribed = storage.is_subscriber(message.chat.id)
        await message.answer(
            format_help(subscribed),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=main_reply_keyboard(),
        )
        await message.answer("Быстрый доступ:", reply_markup=inline_main_menu())

    @dp.message(Command("dates"))
    async def cmd_dates(message: Message) -> None:
        await message.answer(
            "📅 <b>Выборка по датам</b>\n\nВыберите, по какой дате фильтровать:",
            parse_mode=ParseMode.HTML,
            reply_markup=inline_date_field_keyboard(),
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
        await _query_rows(
            message, monitor, bot, storage,
            "Образы, ожидающие передачи на проверку",
            monitor.pending_rows,
        )

    @dp.message(Command("on_review"))
    async def cmd_on_review(message: Message) -> None:
        await _query_rows(
            message, monitor, bot, storage,
            "Образы на проверке у ИБ",
            monitor.rows_on_review,
        )

    @dp.message(Command("passed"))
    async def cmd_passed(message: Message) -> None:
        await _query_rows(
            message, monitor, bot, storage,
            "Образы, прошедшие проверку ИБ",
            monitor.rows_passed,
        )

    @dp.message(Command("failed"))
    async def cmd_failed(message: Message) -> None:
        await _query_rows(
            message, monitor, bot, storage,
            "Образы, не прошедшие проверку ИБ",
            monitor.rows_failed,
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        await _query_status(message, monitor, bot, storage)

    @dp.message(Command("today"))
    async def cmd_today(message: Message) -> None:
        await _query_rows(
            message, monitor, bot, storage,
            "Образы, добавленные сегодня",
            monitor.rows_for_today,
        )

    @dp.message(Command("by_dev"))
    async def cmd_by_dev(message: Message, command: CommandObject) -> None:
        query = (command.args or "").strip()
        if not query:
            await message.answer("Использование: /by_dev Зуев")
            return
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await message.answer(f"❌ {err}")
            return
        rows = monitor.rows_by_developer(query)
        await _send_rows(message, monitor, f"Образы разработчика «{query}»", rows)

    @dp.message(Command("stale"))
    async def cmd_stale(message: Message, command: CommandObject) -> None:
        days_raw = (command.args or "3").strip()
        try:
            days = max(1, int(days_raw))
        except ValueError:
            await message.answer("Использование: /stale 3")
            return
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await message.answer(f"❌ {err}")
            return
        rows = monitor.stale_rows(days)
        await _send_rows(message, monitor, f"Образы без статуса ≥ {days} дн.", rows)

    @dp.message(Command("sync"))
    async def cmd_sync(message: Message) -> None:
        if not message.from_user or not settings.is_admin(message.from_user.id):
            await message.answer("⛔ Команда только для администратора.")
            return
        wait = await message.answer("🔄 Синхронизация…")
        ok, err = await _load_data(monitor, bot, storage, force=True, notify_changes=True)
        if not ok:
            await wait.edit_text(f"❌ Ошибка: {err}")
            return
        await wait.edit_text(
            f"✅ Синхронизация завершена.\n"
            f"Строк: {len(monitor.last_rows)}\n"
            f"Кэш обновлён.",
        )

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
            f"TTL кэша: {settings.cache_ttl_seconds} сек",
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

    @dp.callback_query(F.data == "menu")
    async def cb_menu(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "🏠 <b>Главное меню</b>\n\nВыберите действие:",
                parse_mode=ParseMode.HTML,
                reply_markup=inline_main_menu(),
            )

    @dp.callback_query(F.data.startswith("act:"))
    async def cb_action(callback: CallbackQuery) -> None:
        action = callback.data.split(":", 1)[1]

        if not callback.message:
            await callback.answer()
            return

        if action == "refresh":
            if callback.from_user:
                remaining = _force_refresh_blocked(callback.from_user.id)
                if remaining > 0:
                    await callback.answer(
                        f"Подождите {remaining} сек",
                        show_alert=True,
                    )
                    return
                _mark_force_refresh(callback.from_user.id)
            await callback.answer("Обновляю…")
            await callback.message.edit_text("⏳ Обновляю данные…")
            ok, err = await _load_data(
                monitor, bot, storage, force=True, notify_changes=True
            )
            if not ok:
                await callback.message.edit_text(f"❌ {err}")
                return
            await callback.message.edit_text(
                f"✅ Данные обновлены.\n{_cache_footer(monitor)}",
                reply_markup=inline_main_menu(),
            )
            return

        await callback.answer("Загружаю…")

        if action == "status":
            ok, err = await _load_data(monitor, bot, storage)
            if not ok:
                await callback.message.edit_text(f"❌ {err}")
                return
            summary = monitor.status_summary(monitor.last_rows)
            text = format_status_summary(
                summary, len(monitor.last_rows), footer=_cache_footer(monitor)
            )
            await callback.message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=inline_back_menu(),
            )
            return

        actions: dict[str, tuple[str, Callable[[], list[ImageRow]]]] = {
            "pending": ("Образы, ожидающие передачи на проверку", monitor.pending_rows),
            "on_review": ("Образы на проверке у ИБ", monitor.rows_on_review),
            "passed": ("Образы, прошедшие проверку ИБ", monitor.rows_passed),
            "failed": ("Образы, не прошедшие проверку ИБ", monitor.rows_failed),
            "today": ("Образы, добавленные сегодня", monitor.rows_for_today),
        }
        spec = actions.get(action)
        if not spec:
            return
        title, fn = spec
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await callback.message.edit_text(f"❌ {err}")
            return
        await _send_rows(callback.message, monitor, title, fn(), edit=True)

    @dp.callback_query(F.data == "date:start")
    async def cb_date_start(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "📅 <b>Выборка по датам</b>\n\nПо какой дате фильтровать?",
                parse_mode=ParseMode.HTML,
                reply_markup=inline_date_field_keyboard(),
            )

    @dp.callback_query(F.data.startswith("date:f:"))
    async def cb_date_field(callback: CallbackQuery) -> None:
        date_field = callback.data.rsplit(":", 1)[1]
        await callback.answer()
        if callback.message:
            label = FIELD_LABELS.get(date_field, date_field)
            await callback.message.edit_text(
                f"📅 <b>{label.capitalize()}</b>\n\nВыберите период:",
                parse_mode=ParseMode.HTML,
                reply_markup=inline_date_period_keyboard(date_field),
            )

    @dp.callback_query(F.data.startswith("date:p:"))
    async def cb_date_period(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 4:
            await callback.answer("Ошибка")
            return
        date_field, period = parts[2], parts[3]
        await callback.answer()

        if period == "cu":
            if callback.from_user:
                _awaiting_custom_date[callback.from_user.id] = date_field
            if callback.message:
                await callback.message.edit_text(
                    "✏️ <b>Свой период</b>\n\n"
                    "Отправьте дату или диапазон:\n"
                    "• <code>15.06.2026</code>\n"
                    "• <code>01.06.2026-15.06.2026</code>",
                    parse_mode=ParseMode.HTML,
                )
            return

        if callback.message:
            await callback.message.edit_text(
                "📋 <b>Результат проверки</b>\n\nЧто показать?",
                parse_mode=ParseMode.HTML,
                reply_markup=inline_date_status_keyboard(date_field, period),
            )

    @dp.callback_query(F.data.startswith("date:s:"))
    async def cb_date_status(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 5:
            await callback.answer("Ошибка")
            return
        date_field, period, status_filter = parts[2], parts[3], parts[4]
        await callback.answer("Загружаю…")

        if not callback.message:
            return

        try:
            start, end = period_to_range(period)
        except ValueError:
            await callback.message.edit_text("❌ Неверный период")
            return

        await _show_date_report(
            callback.message,
            monitor,
            bot,
            storage,
            date_field,
            start,
            end,
            status_filter,
            edit=True,
        )

    @dp.callback_query(F.data.startswith("date:c:"))
    async def cb_date_custom(callback: CallbackQuery) -> None:
        # date:c:tr:2026-06-01_2026-06-15:ok
        parts = callback.data.split(":")
        if len(parts) != 5:
            await callback.answer("Ошибка")
            return
        date_field = parts[2]
        range_token, status_filter = parts[3], parts[4]
        await callback.answer("Загружаю…")

        if not callback.message:
            return

        try:
            start_raw, end_raw = range_token.split("_", 1)
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError:
            await callback.message.edit_text("❌ Неверный период")
            return

        await _show_date_report(
            callback.message,
            monitor,
            bot,
            storage,
            date_field,
            start,
            end,
            status_filter,
            edit=True,
        )

    @dp.message(F.text.in_(REPLY_BUTTONS))
    async def reply_buttons(message: Message) -> None:
        text = message.text or ""
        if text == BTN_PENDING:
            await _query_rows(
                message, monitor, bot, storage,
                "Образы, ожидающие передачи на проверку",
                monitor.pending_rows,
            )
        elif text == BTN_ON_REVIEW:
            await _query_rows(
                message, monitor, bot, storage,
                "Образы на проверке у ИБ",
                monitor.rows_on_review,
            )
        elif text == BTN_PASSED:
            await _query_rows(
                message, monitor, bot, storage,
                "Образы, прошедшие проверку ИБ",
                monitor.rows_passed,
            )
        elif text == BTN_FAILED:
            await _query_rows(
                message, monitor, bot, storage,
                "Образы, не прошедшие проверку ИБ",
                monitor.rows_failed,
            )
        elif text == BTN_STATUS:
            await _query_status(message, monitor, bot, storage)
        elif text == BTN_BY_DATE:
            await message.answer(
                "📅 <b>Выборка по датам</b>\n\nВыберите, по какой дате фильтровать:",
                parse_mode=ParseMode.HTML,
                reply_markup=inline_date_field_keyboard(),
            )
        elif text == BTN_TODAY:
            await _query_rows(
                message, monitor, bot, storage,
                "Образы, добавленные сегодня",
                monitor.rows_for_today,
            )
        elif text == BTN_REFRESH:
            if message.from_user:
                remaining = _force_refresh_blocked(message.from_user.id)
                if remaining > 0:
                    await message.answer(
                        f"🔄 Обновление доступно через {remaining} сек."
                    )
                    return
                _mark_force_refresh(message.from_user.id)
            wait = await message.answer("⏳ Обновляю данные…")
            ok, err = await _load_data(
                monitor, bot, storage, force=True, notify_changes=True
            )
            if not ok:
                await wait.edit_text(f"❌ {err}")
                return
            await wait.edit_text(
                f"✅ Данные обновлены.\n{_cache_footer(monitor)}",
                reply_markup=inline_main_menu(),
            )
        elif text == BTN_MENU:
            await message.answer(
                "🏠 <b>Главное меню</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=inline_main_menu(),
            )

    @dp.message(F.text)
    async def text_input(message: Message) -> None:
        if message.text and message.text.startswith("/"):
            await message.answer("Неизвестная команда. /help")
            return

        user_id = message.from_user.id if message.from_user else None
        if not user_id or user_id not in _awaiting_custom_date:
            return

        parsed = parse_date_range(message.text or "")
        if not parsed:
            await message.answer(
                "❌ Неверный формат.\nПример: <code>15.06.2026</code> или "
                "<code>01.06.2026-15.06.2026</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        date_field = _awaiting_custom_date.pop(user_id)
        start, end = parsed
        await message.answer(
            "📋 <b>Результат проверки</b>\n\nЧто показать?",
            parse_mode=ParseMode.HTML,
            reply_markup=_custom_date_keyboard(date_field, start, end),
        )


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
        await monitor.ensure_fresh(force=True)
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
        result = await monitor.ensure_fresh(force=True)
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
