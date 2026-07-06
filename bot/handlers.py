from __future__ import annotations

import asyncio
import logging
import secrets
from collections import defaultdict
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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
    format_developers_list,
    format_help,
    format_releases_list,
    format_report_preview,
    format_row_detail,
    format_rows_page_numbered,
    format_status_summary,
    format_user_history,
    format_users_overview,
    format_welcome,
)
from bot.keyboards import (
    BTN_BY_DATE,
    BTN_DEVS,
    BTN_FAILED,
    BTN_MENU,
    BTN_ON_REVIEW,
    BTN_PASSED,
    BTN_PENDING,
    BTN_REFRESH,
    BTN_RELEASES,
    BTN_STATUS,
    BTN_TODAY,
    REPLY_BUTTONS,
    inline_back_menu,
    inline_date_field_keyboard,
    inline_date_period_keyboard,
    inline_date_status_keyboard,
    inline_detail_card,
    inline_developers_keyboard,
    inline_main_menu,
    inline_paginated_menu,
    inline_releases_keyboard,
    inline_report_confirm,
    main_reply_keyboard,
)
from bot.models import ImageRow
from bot.monitor import RegistryMonitor
from bot.reports import (
    MAX_ARCHIVE_SIZE,
    ReportMatch,
    ReportParseError,
    extract_reports,
    match_reports,
)
from bot.scheduler import broadcast_changes
from bot.storage import Storage
from bot.utils import safe_edit, safe_send

logger = logging.getLogger(__name__)

_last_force_refresh: dict[int, datetime] = defaultdict(lambda: datetime.min)
_awaiting_custom_date: dict[int, str] = {}  # user_id -> date_field
# Per-chat dynamic result cache for paginating ad-hoc queries (find/by_dev/stale)
_dynamic_results: dict[int, tuple[str, list[ImageRow]]] = {}
# token -> parsed report matches awaiting admin confirmation
_pending_reports: dict[str, list[ReportMatch]] = {}

# token -> (title, monitor method name)
VIEWS: dict[str, tuple[str, str]] = {
    "pending": ("Образы, ожидающие передачи на проверку", "pending_rows"),
    "on_review": ("Образы на проверке у ИБ", "rows_on_review"),
    "passed": ("Образы, прошедшие проверку ИБ", "rows_passed"),
    "failed": ("Образы, не прошедшие проверку ИБ", "rows_failed"),
    "today": ("Образы, добавленные сегодня", "rows_for_today"),
    "last": ("Последние добавленные образы", "last_rows_added"),
}


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


async def _apply_force(message: Message) -> bool:
    """Return True if a forced refresh is allowed (and mark it)."""
    if not message.from_user:
        return True
    remaining = _force_refresh_blocked(message.from_user.id)
    if remaining > 0:
        await message.answer(
            f"🔄 Обновление доступно через {remaining} сек.\n"
            "Пока показываю данные из кэша."
        )
        return False
    _mark_force_refresh(message.from_user.id)
    return True


# --- Rendering ---------------------------------------------------------------

async def _render_page(
    target: Message,
    text: str,
    kb,
    *,
    edit: bool,
) -> None:
    if edit:
        await safe_edit(target, text, reply_markup=kb)
    else:
        await target.answer(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=kb
        )


async def _render_view(
    target: Message,
    monitor: RegistryMonitor,
    token: str,
    page: int,
    *,
    edit: bool,
) -> None:
    title, method = VIEWS[token]
    rows = getattr(monitor, method)()
    text, page, pages, row_numbers = format_rows_page_numbered(
        rows, title, page, footer=_cache_footer(monitor)
    )
    kb = inline_paginated_menu(f"pg:{token}", page, pages, row_numbers)
    await _render_page(target, text, kb, edit=edit)


async def _render_dynamic(
    target: Message,
    monitor: RegistryMonitor,
    chat_id: int,
    title: str,
    rows: list[ImageRow],
    page: int,
    *,
    edit: bool,
) -> None:
    _dynamic_results[chat_id] = (title, rows)
    text, page, pages, row_numbers = format_rows_page_numbered(
        rows, title, page, footer=_cache_footer(monitor)
    )
    kb = inline_paginated_menu("pgm", page, pages, row_numbers)
    await _render_page(target, text, kb, edit=edit)


async def _render_date(
    target: Message,
    monitor: RegistryMonitor,
    date_field: str,
    start: date,
    end: date,
    status_filter: str,
    page: int,
    *,
    edit: bool,
) -> None:
    rows = monitor.rows_by_date_range(start, end, date_field, status_filter)
    period = format_period_label(start, end)
    field = FIELD_LABELS.get(date_field, date_field)
    status = STATUS_LABELS.get(status_filter, status_filter)
    title = f"Выборка: {status} · {field} · {period}"
    text, page, pages, row_numbers = format_rows_page_numbered(
        rows, title, page, footer=_cache_footer(monitor)
    )
    token = f"{start.isoformat()}_{end.isoformat()}"
    prefix = f"pgd:{date_field}:{token}:{status_filter}"
    kb = inline_paginated_menu(prefix, page, pages, row_numbers)
    await _render_page(target, text, kb, edit=edit)


async def _serve_view(
    message: Message,
    monitor: RegistryMonitor,
    bot: Bot,
    storage: Storage,
    token: str,
    *,
    force: bool = False,
) -> None:
    if force and not await _apply_force(message):
        force = False

    wait = None
    if force:
        wait = await message.answer("⏳ Обновляю данные…")
    elif not monitor.last_rows:
        wait = await message.answer("⏳ Загружаю данные…")

    ok, err = await _load_data(monitor, bot, storage, force=force, notify_changes=force)
    if wait:
        try:
            await wait.delete()
        except Exception:
            pass

    if not ok:
        await message.answer(f"❌ Не удалось загрузить таблицу:\n<code>{err}</code>")
        return

    await _render_view(message, monitor, token, 0, edit=False)


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
        await _serve_view(message, monitor, bot, storage, "pending")

    @dp.message(Command("on_review"))
    async def cmd_on_review(message: Message) -> None:
        await _serve_view(message, monitor, bot, storage, "on_review")

    @dp.message(Command("passed"))
    async def cmd_passed(message: Message) -> None:
        await _serve_view(message, monitor, bot, storage, "passed")

    @dp.message(Command("failed"))
    async def cmd_failed(message: Message) -> None:
        await _serve_view(message, monitor, bot, storage, "failed")

    @dp.message(Command("today"))
    async def cmd_today(message: Message) -> None:
        await _serve_view(message, monitor, bot, storage, "today")

    @dp.message(Command("last"))
    async def cmd_last(message: Message) -> None:
        await _serve_view(message, monitor, bot, storage, "last")

    @dp.message(Command("devs"))
    async def cmd_devs(message: Message) -> None:
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await message.answer(f"❌ {err}")
            return
        devs = monitor.developers_summary()
        await message.answer(
            format_developers_list(devs),
            parse_mode=ParseMode.HTML,
            reply_markup=inline_developers_keyboard(devs),
        )

    @dp.message(Command("releases"))
    async def cmd_releases(message: Message) -> None:
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await message.answer(f"❌ {err}")
            return
        releases = monitor.releases_summary(limit=20)
        await message.answer(
            format_releases_list(releases),
            parse_mode=ParseMode.HTML,
            reply_markup=inline_releases_keyboard(releases),
        )

    @dp.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        if not monitor.last_rows:
            wait = await message.answer("⏳ Загружаю данные…")
        else:
            wait = None
        ok, err = await _load_data(monitor, bot, storage)
        if wait:
            try:
                await wait.delete()
            except Exception:
                pass
        if not ok:
            await message.answer(f"❌ {err}")
            return
        summary = monitor.status_summary(monitor.last_rows)
        text = format_status_summary(
            summary, len(monitor.last_rows), footer=_cache_footer(monitor)
        )
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=inline_back_menu(),
        )

    @dp.message(Command("find"))
    async def cmd_find(message: Message, command: CommandObject) -> None:
        query = (command.args or "").strip()
        if not query:
            await message.answer("Использование: /find leadgen")
            return
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await message.answer(f"❌ {err}")
            return
        rows = monitor.find_rows(query)
        await _render_dynamic(
            message, monitor, message.chat.id,
            f"Поиск: «{query}»", rows, 0, edit=False,
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
        await _render_dynamic(
            message, monitor, message.chat.id,
            f"Образы разработчика «{query}»", rows, 0, edit=False,
        )

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
        await _render_dynamic(
            message, monitor, message.chat.id,
            f"Образы без статуса ≥ {days} дн.", rows, 0, edit=False,
        )

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
            f"✅ Синхронизация завершена.\nСтрок: {len(monitor.last_rows)}\nКэш обновлён."
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

    @dp.message(Command("users"))
    async def cmd_users(message: Message, command: CommandObject) -> None:
        if not message.from_user or not settings.is_admin(message.from_user.id):
            await message.answer("⛔ Команда только для администратора.")
            return
        try:
            days = max(1, min(365, int((command.args or "7").strip())))
        except ValueError:
            days = 7
        overview = storage.activity_overview(days)
        recent = storage.recent_activity(limit=10)
        await message.answer(
            format_users_overview(overview, recent),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @dp.message(Command("user"))
    async def cmd_user(message: Message, command: CommandObject) -> None:
        if not message.from_user or not settings.is_admin(message.from_user.id):
            await message.answer("⛔ Команда только для администратора.")
            return
        raw = (command.args or "").strip()
        if not raw.isdigit():
            await message.answer("Использование: /user 145212489\n(id можно взять из /users)")
            return
        items = storage.user_activity(int(raw))
        await message.answer(
            format_user_history(int(raw), items),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @dp.message(Command("broadcast"))
    async def cmd_broadcast(message: Message, command: CommandObject) -> None:
        if not message.from_user or not settings.is_admin(message.from_user.id):
            await message.answer("⛔ Команда только для администратора.")
            return
        text = (command.args or "").strip()
        if not text:
            await message.answer("Использование: /broadcast текст сообщения")
            return
        subs = storage.list_subscribers()
        sent = 0
        for chat_id in subs:
            if await safe_send(bot, chat_id, f"📢 <b>Объявление</b>\n\n{text}"):
                sent += 1
            await asyncio.sleep(0.05)
        await message.answer(f"✅ Отправлено {sent}/{len(subs)} подписчикам.")

    # --- IB scan report archives -------------------------------------------

    @dp.message(F.document)
    async def handle_report_archive(message: Message) -> None:
        doc = message.document
        if not doc or not doc.file_name:
            return
        name = doc.file_name.lower()
        if not (name.endswith(".7z") or name.endswith(".zip")):
            await message.answer(
                "📎 Я умею обрабатывать архивы отчётов ИБ (.7z или .zip).\n"
                "Пришлите архив со сканами — я разберу его и предложу статусы."
            )
            return
        if not message.from_user or not settings.is_admin(message.from_user.id):
            await message.answer(
                "⛔ Обработка отчётов ИБ доступна только администратору."
            )
            return
        if doc.file_size and doc.file_size > MAX_ARCHIVE_SIZE:
            await message.answer(
                f"❌ Архив слишком большой ({doc.file_size // 1024 // 1024} МБ). "
                "Telegram позволяет ботам скачивать файлы до 20 МБ."
            )
            return

        wait = await message.answer("⏳ Скачиваю и разбираю архив…")
        try:
            file = await bot.get_file(doc.file_id)
            buffer = await bot.download_file(file.file_path)
            data = buffer.read()
            reports = await asyncio.to_thread(extract_reports, data, doc.file_name)
        except ReportParseError as exc:
            await safe_edit(wait, f"❌ {exc}")
            return
        except Exception:
            logger.exception("Failed to process report archive")
            await safe_edit(wait, "❌ Не удалось обработать архив. Попробуйте ещё раз.")
            return

        ok, err = await _load_data(monitor, bot, storage, force=True)
        if not ok:
            await safe_edit(wait, f"❌ Архив разобран, но таблица недоступна:\n{err}")
            return

        matches = match_reports(reports, monitor.last_rows)
        can_write = monitor.sheets.can_write
        text = format_report_preview(matches, can_write)

        applicable = [m for m in matches if m.row is not None]
        kb = None
        if can_write and applicable:
            token = secrets.token_hex(4)
            _pending_reports[token] = matches
            # Keep only the few most recent pending reports
            while len(_pending_reports) > 5:
                _pending_reports.pop(next(iter(_pending_reports)))
            kb = inline_report_confirm(token)
        await safe_edit(wait, text, reply_markup=kb)

    @dp.message(F.photo)
    async def handle_photo(message: Message) -> None:
        await message.answer(
            "🖼 Скриншоты я пока не распознаю — по картинке легко ошибиться "
            "со статусом.\n\nПришлите архив отчёта ИБ (.7z или .zip) — "
            "я разберу его точно и сам предложу статусы для таблицы."
        )

    @dp.callback_query(F.data.startswith("rep:"))
    async def cb_report(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("Ошибка")
            return
        action, token = parts[1], parts[2]

        if not callback.from_user or not settings.is_admin(callback.from_user.id):
            await callback.answer("Только для администратора", show_alert=True)
            return

        matches = _pending_reports.get(token)
        if action == "cancel":
            _pending_reports.pop(token, None)
            await callback.answer("Отменено")
            if callback.message:
                await safe_edit(
                    callback.message,
                    "❌ Проставление статусов отменено. Таблица не изменена.",
                )
            return

        if matches is None:
            await callback.answer(
                "Этот отчёт устарел, пришлите архив ещё раз.", show_alert=True
            )
            return

        await callback.answer("Записываю…")
        if callback.message:
            await safe_edit(callback.message, "⏳ Проставляю статусы в таблице…")

        today_str = date.today().strftime("%d.%m.%Y")
        updates = []
        for m in matches:
            if m.row is None:
                continue
            check_date = today_str if not m.row.check_date else ""
            updates.append((m.row.row_number, m.report.verdict_status, check_date))

        try:
            await monitor.sheets.update_statuses(updates)
        except Exception as exc:
            logger.exception("Failed to write statuses to sheet")
            if callback.message:
                await safe_edit(
                    callback.message,
                    f"❌ Не удалось записать в таблицу:\n<code>{exc}</code>\n\n"
                    "Проверьте, что сервисный аккаунт имеет права редактора.",
                    reply_markup=inline_report_confirm(token),
                )
            return

        _pending_reports.pop(token, None)
        failed_count = sum(1 for m in matches if m.row and not m.report.passed)
        passed_count = sum(1 for m in matches if m.row and m.report.passed)
        if callback.message:
            await safe_edit(
                callback.message,
                "✅ <b>Статусы проставлены в таблице</b>\n\n"
                f"✅ Прошло проверку: {passed_count}\n"
                f"❌ Не прошло проверку: {failed_count}\n"
                f"Всего обновлено строк: {len(updates)}",
            )
        # Refresh the cache and notify subscribers through the usual diff flow.
        await _load_data(monitor, bot, storage, force=True, notify_changes=True)

    # --- Callbacks ---

    @dp.callback_query(F.data == "noop")
    async def cb_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    @dp.callback_query(F.data == "menu")
    async def cb_menu(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await safe_edit(
                callback.message,
                "🏠 <b>Главное меню</b>\n\nВыберите действие:",
                reply_markup=inline_main_menu(),
            )

    @dp.callback_query(F.data.startswith("row:"))
    async def cb_row_detail(callback: CallbackQuery) -> None:
        # row:<row_number> → detail card as a NEW message (list stays intact)
        try:
            row_number = int(callback.data.split(":", 1)[1])
        except ValueError:
            await callback.answer("Ошибка")
            return
        ok, _ = await _load_data(monitor, bot, storage)
        if not ok:
            await callback.answer("Не удалось загрузить данные", show_alert=True)
            return
        row = monitor.get_row(row_number)
        if not row:
            await callback.answer("Строка не найдена (данные обновились)", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await callback.message.answer(
                format_row_detail(row),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=inline_detail_card(),
            )

    @dp.callback_query(F.data == "devs")
    async def cb_devs(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await safe_edit(callback.message, f"❌ {err}", reply_markup=inline_main_menu())
            return
        devs = monitor.developers_summary()
        await safe_edit(
            callback.message,
            format_developers_list(devs),
            reply_markup=inline_developers_keyboard(devs),
        )

    @dp.callback_query(F.data == "rels")
    async def cb_rels(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await safe_edit(callback.message, f"❌ {err}", reply_markup=inline_main_menu())
            return
        releases = monitor.releases_summary(limit=20)
        await safe_edit(
            callback.message,
            format_releases_list(releases),
            reply_markup=inline_releases_keyboard(releases),
        )

    @dp.callback_query(F.data.startswith("dev:"))
    async def cb_dev_rows(callback: CallbackQuery) -> None:
        name = callback.data.split(":", 1)[1]
        await callback.answer("Загружаю…")
        if not callback.message:
            return
        ok, _ = await _load_data(monitor, bot, storage)
        if not ok:
            return
        rows = monitor.rows_by_developer(name)
        await _render_dynamic(
            callback.message, monitor, callback.message.chat.id,
            f"Образы разработчика «{name}»", rows, 0, edit=True,
        )

    @dp.callback_query(F.data.startswith("rel:"))
    async def cb_rel_rows(callback: CallbackQuery) -> None:
        release = callback.data.split(":", 1)[1]
        await callback.answer("Загружаю…")
        if not callback.message:
            return
        ok, _ = await _load_data(monitor, bot, storage)
        if not ok:
            return
        rows = monitor.rows_by_release(release)
        await _render_dynamic(
            callback.message, monitor, callback.message.chat.id,
            f"Образы релиза «{release}»", rows, 0, edit=True,
        )

    @dp.callback_query(F.data.startswith("pg:"))
    async def cb_page_view(callback: CallbackQuery) -> None:
        # pg:<token>:<page>
        parts = callback.data.split(":")
        if len(parts) != 3 or parts[1] not in VIEWS:
            await callback.answer("Ошибка")
            return
        token, page = parts[1], int(parts[2])
        await callback.answer()
        if not callback.message:
            return
        ok, _ = await _load_data(monitor, bot, storage)
        if not ok:
            return
        await _render_view(callback.message, monitor, token, page, edit=True)

    @dp.callback_query(F.data.startswith("pgm:"))
    async def cb_page_dynamic(callback: CallbackQuery) -> None:
        # pgm:<page>
        parts = callback.data.split(":")
        if len(parts) != 2:
            await callback.answer("Ошибка")
            return
        page = int(parts[1])
        await callback.answer()
        if not callback.message:
            return
        cached = _dynamic_results.get(callback.message.chat.id)
        if not cached:
            await safe_edit(
                callback.message,
                "Результаты устарели, повторите запрос.",
                reply_markup=inline_main_menu(),
            )
            return
        title, rows = cached
        await _render_dynamic(
            callback.message, monitor, callback.message.chat.id, title, rows, page, edit=True
        )

    @dp.callback_query(F.data.startswith("pgd:"))
    async def cb_page_date(callback: CallbackQuery) -> None:
        # pgd:<field>:<start>_<end>:<status>:<page>
        parts = callback.data.split(":")
        if len(parts) != 5:
            await callback.answer("Ошибка")
            return
        date_field, token, status_filter, page = parts[1], parts[2], parts[3], int(parts[4])
        await callback.answer()
        if not callback.message:
            return
        try:
            start_raw, end_raw = token.split("_", 1)
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError:
            await callback.answer("Неверный период")
            return
        ok, _ = await _load_data(monitor, bot, storage)
        if not ok:
            return
        await _render_date(
            callback.message, monitor, date_field, start, end, status_filter, page, edit=True
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
                    await callback.answer(f"Подождите {remaining} сек", show_alert=True)
                    return
                _mark_force_refresh(callback.from_user.id)
            await callback.answer("Обновляю…")
            await safe_edit(callback.message, "⏳ Обновляю данные…")
            ok, err = await _load_data(
                monitor, bot, storage, force=True, notify_changes=True
            )
            if not ok:
                await safe_edit(callback.message, f"❌ {err}", reply_markup=inline_main_menu())
                return
            await safe_edit(
                callback.message,
                f"✅ Данные обновлены.\n{_cache_footer(monitor)}",
                reply_markup=inline_main_menu(),
            )
            return

        await callback.answer("Загружаю…")

        if action == "status":
            ok, err = await _load_data(monitor, bot, storage)
            if not ok:
                await safe_edit(callback.message, f"❌ {err}", reply_markup=inline_main_menu())
                return
            summary = monitor.status_summary(monitor.last_rows)
            text = format_status_summary(
                summary, len(monitor.last_rows), footer=_cache_footer(monitor)
            )
            await safe_edit(callback.message, text, reply_markup=inline_back_menu())
            return

        if action in VIEWS:
            ok, err = await _load_data(monitor, bot, storage)
            if not ok:
                await safe_edit(callback.message, f"❌ {err}", reply_markup=inline_main_menu())
                return
            await _render_view(callback.message, monitor, action, 0, edit=True)

    @dp.callback_query(F.data == "date:start")
    async def cb_date_start(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await safe_edit(
                callback.message,
                "📅 <b>Выборка по датам</b>\n\nПо какой дате фильтровать?",
                reply_markup=inline_date_field_keyboard(),
            )

    @dp.callback_query(F.data.startswith("date:f:"))
    async def cb_date_field(callback: CallbackQuery) -> None:
        date_field = callback.data.rsplit(":", 1)[1]
        await callback.answer()
        if callback.message:
            label = FIELD_LABELS.get(date_field, date_field)
            await safe_edit(
                callback.message,
                f"📅 <b>{label.capitalize()}</b>\n\nВыберите период:",
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
                await safe_edit(
                    callback.message,
                    "✏️ <b>Свой период</b>\n\n"
                    "Отправьте дату или диапазон:\n"
                    "• <code>15.06.2026</code>\n"
                    "• <code>01.06.2026-15.06.2026</code>",
                )
            return

        if callback.message:
            await safe_edit(
                callback.message,
                "📋 <b>Результат проверки</b>\n\nЧто показать?",
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
            await callback.answer("Неверный период")
            return
        ok, _ = await _load_data(monitor, bot, storage)
        if not ok:
            return
        await _render_date(
            callback.message, monitor, date_field, start, end, status_filter, 0, edit=True
        )

    @dp.callback_query(F.data.startswith("date:c:"))
    async def cb_date_custom(callback: CallbackQuery) -> None:
        # date:c:<field>:<start>_<end>:<status>
        parts = callback.data.split(":")
        if len(parts) != 5:
            await callback.answer("Ошибка")
            return
        date_field, range_token, status_filter = parts[2], parts[3], parts[4]
        await callback.answer("Загружаю…")
        if not callback.message:
            return
        try:
            start_raw, end_raw = range_token.split("_", 1)
            start = date.fromisoformat(start_raw)
            end = date.fromisoformat(end_raw)
        except ValueError:
            await callback.answer("Неверный период")
            return
        ok, _ = await _load_data(monitor, bot, storage)
        if not ok:
            return
        await _render_date(
            callback.message, monitor, date_field, start, end, status_filter, 0, edit=True
        )

    # --- Reply keyboard buttons ---

    @dp.message(F.text.in_(REPLY_BUTTONS))
    async def reply_buttons(message: Message) -> None:
        text = message.text or ""
        mapping = {
            BTN_PENDING: "pending",
            BTN_ON_REVIEW: "on_review",
            BTN_PASSED: "passed",
            BTN_FAILED: "failed",
            BTN_TODAY: "today",
        }
        if text in mapping:
            await _serve_view(message, monitor, bot, storage, mapping[text])
        elif text == BTN_STATUS:
            await cmd_status_via_button(message)
        elif text == BTN_DEVS:
            ok, err = await _load_data(monitor, bot, storage)
            if not ok:
                await message.answer(f"❌ {err}")
                return
            devs = monitor.developers_summary()
            await message.answer(
                format_developers_list(devs),
                parse_mode=ParseMode.HTML,
                reply_markup=inline_developers_keyboard(devs),
            )
        elif text == BTN_RELEASES:
            ok, err = await _load_data(monitor, bot, storage)
            if not ok:
                await message.answer(f"❌ {err}")
                return
            releases = monitor.releases_summary(limit=20)
            await message.answer(
                format_releases_list(releases),
                parse_mode=ParseMode.HTML,
                reply_markup=inline_releases_keyboard(releases),
            )
        elif text == BTN_BY_DATE:
            await message.answer(
                "📅 <b>Выборка по датам</b>\n\nВыберите, по какой дате фильтровать:",
                parse_mode=ParseMode.HTML,
                reply_markup=inline_date_field_keyboard(),
            )
        elif text == BTN_REFRESH:
            if not await _apply_force(message):
                return
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

    async def cmd_status_via_button(message: Message) -> None:
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await message.answer(f"❌ {err}")
            return
        summary = monitor.status_summary(monitor.last_rows)
        text = format_status_summary(
            summary, len(monitor.last_rows), footer=_cache_footer(monitor)
        )
        await message.answer(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=inline_back_menu(),
        )

    @dp.message(F.text)
    async def text_input(message: Message) -> None:
        text = (message.text or "").strip()
        if text.startswith("/"):
            await message.answer("Неизвестная команда. /help")
            return

        user_id = message.from_user.id if message.from_user else None

        # 1) Waiting for a custom date range?
        if user_id and user_id in _awaiting_custom_date:
            parsed = parse_date_range(text)
            if not parsed:
                await message.answer(
                    "❌ Неверный формат.\nПример: <code>15.06.2026</code> или "
                    "<code>01.06.2026-15.06.2026</code>\n\n"
                    "Или напишите текст ещё раз после выхода в /menu.",
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
            return

        # 2) A bare date? Treat as transfer-date filter.
        parsed = parse_date_range(text)
        if parsed:
            start, end = parsed
            ok, _ = await _load_data(monitor, bot, storage)
            if ok:
                await _render_date(message, monitor, "tr", start, end, "all", 0, edit=False)
            return

        # 3) Free-text search across the registry.
        if len(text) < 2:
            return
        ok, err = await _load_data(monitor, bot, storage)
        if not ok:
            await message.answer(f"❌ {err}")
            return
        rows = monitor.find_rows(text)
        if not rows:
            await message.answer(
                f"🔎 По запросу «{text}» ничего не найдено.\n"
                "Поиск идёт по тегу, релизу, разработчику и статусу.\n"
                "Попробуйте короче, например часть имени образа.",
            )
            return
        await _render_dynamic(
            message, monitor, message.chat.id,
            f"Поиск: «{text}»", rows, 0, edit=False,
        )
