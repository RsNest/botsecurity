"""Developer flows: add new tags and submit corrected tags after a failed check."""

from __future__ import annotations

import logging
import re
from datetime import date

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import STATUS_ON_REVIEW, settings
from bot.dates import parse_flexible_date
from bot.formatters import format_add_preview, format_my_rows, format_row_detail
from bot.keyboards import BTN_ADD, BTN_MY, main_reply_keyboard
from bot.models import ImageRow
from bot.monitor import RegistryMonitor
from bot.storage import Storage
from bot.utils import esc, safe_edit

logger = logging.getLogger(__name__)

_TAG_LINE_RE = re.compile(
    r"^[\w./:@\-]+(?:\s+[\w./:@\-]+)*$",
    re.UNICODE,
)


class AddTagStates(StatesGroup):
    tags = State()
    release = State()
    surname = State()
    transfer_date = State()
    confirm = State()


class FixTagStates(StatesGroup):
    pick_row = State()
    tag = State()
    confirm = State()


def _today_str() -> str:
    return date.today().strftime("%d.%m.%Y")


def _parse_tags(text: str) -> list[str]:
    tags: list[str] = []
    for line in text.splitlines():
        tag = line.strip()
        if not tag:
            continue
        if len(tag) < 3 or " " in tag:
            continue
        tags.append(tag)
    return tags


def _is_plausible_tag(text: str) -> bool:
    text = text.strip()
    if len(text) < 3 or len(text) > 300:
        return False
    if " " in text:
        return False
    return bool(_TAG_LINE_RE.match(text))


def _surname_keyboard(names: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for name in names[:20]:
        pair.append(
            InlineKeyboardButton(text=name, callback_data=f"add:surname:{name[:30]}")
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="add:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _release_keyboard(releases: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for release, _ in releases[:12]:
        rows.append([
            InlineKeyboardButton(
                text=release[:40], callback_data=f"add:release:{release[:50]}"
            )
        ])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="add:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Записать в таблицу", callback_data=f"{prefix}:save"
                )
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"{prefix}:cancel")],
        ]
    )


def _fix_pick_keyboard(rows: list[ImageRow]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows[:10]:
        label = row.short_tag(35)
        buttons.append([
            InlineKeyboardButton(
                text=f"стр.{row.row_number}: {label}",
                callback_data=f"fix:pick:{row.row_number}",
            )
        ])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="fix:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _notify_admins(bot: Bot, text: str) -> None:
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Failed to notify admin %s", admin_id)


class PendingFixTagFilter(BaseFilter):
    """Only match when user has failed rows awaiting a corrected tag."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def __call__(self, message: Message) -> bool:
        if not message.from_user or not message.text:
            return False
        if not _is_plausible_tag(message.text.strip()):
            return False
        return bool(self.storage.get_pending_fixes(message.from_user.id))


def setup_submit_handlers(
    dp: Dispatcher,
    bot: Bot,
    monitor: RegistryMonitor,
    storage: Storage,
) -> None:
    @dp.message(Command("add"))
    @dp.message(F.text == BTN_ADD)
    async def cmd_add(message: Message, state: FSMContext) -> None:
        if not monitor.sheets.can_write:
            await message.answer(
                "🔒 Добавление тегов временно недоступно — нет доступа к записи "
                "в таблицу. Обратитесь к администратору."
            )
            return
        await state.clear()
        await state.set_state(AddTagStates.tags)
        await message.answer(
            "➕ <b>Добавление тега в реестр</b>\n\n"
            "Пришлите один или несколько тегов (docker-образов), "
            "каждый с новой строки:\n\n"
            "<code>harbor.uis.st/images/my/service:app-1.0.0</code>\n\n"
            "Отмена — /cancel",
            parse_mode=ParseMode.HTML,
        )

    @dp.message(Command("my"))
    @dp.message(F.text == BTN_MY)
    async def cmd_my(message: Message) -> None:
        if not message.from_user:
            return
        profile = storage.get_profile(message.from_user.id)
        if not profile:
            await message.answer(
                "У вас пока нет профиля разработчика.\n"
                "Добавьте первый тег — /add"
            )
            return
        ok, err = await _ensure_fresh(monitor)
        if not ok:
            await message.answer(f"❌ {err}")
            return
        rows = monitor.rows_by_developer(profile["surname"])
        await message.answer(
            format_my_rows(profile["surname"], rows),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    @dp.message(Command("cancel"))
    async def cmd_cancel(message: Message, state: FSMContext) -> None:
        current = await state.get_state()
        if not current:
            await message.answer("Нечего отменять.")
            return
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_reply_keyboard())

    # --- Add tag FSM ---------------------------------------------------------

    @dp.message(AddTagStates.tags)
    async def add_tags(message: Message, state: FSMContext) -> None:
        tags = _parse_tags(message.text or "")
        if not tags:
            await message.answer(
                "❌ Не нашёл тегов. Пришлите образ построчно, например:\n"
                "<code>harbor.uis.st/images/app/api:1.0.0</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        ok, err = await _ensure_fresh(monitor)
        if not ok:
            await message.answer(f"❌ {err}")
            return

        dupes = [t for t in tags if monitor.find_duplicate_tag(t)]
        if dupes:
            lines = "\n".join(f"• <code>{esc(t)}</code>" for t in dupes[:5])
            await message.answer(
                f"❌ Эти теги уже есть в реестре:\n{lines}\n\n"
                "Если нужно отправить исправленную версию — дождитесь "
                "уведомления о провале или пришлите новый тег после ❌.",
                parse_mode=ParseMode.HTML,
            )
            return

        await state.update_data(tags=tags)
        releases = monitor.releases_summary(limit=12)
        await state.set_state(AddTagStates.release)
        await message.answer(
            "🏷 <b>Релиз</b>\n\nВыберите из списка или напишите название релиза:",
            parse_mode=ParseMode.HTML,
            reply_markup=_release_keyboard(releases),
        )

    @dp.callback_query(F.data.startswith("add:release:"))
    async def add_release_cb(callback: CallbackQuery, state: FSMContext) -> None:
        release = callback.data.split(":", 2)[2]
        await callback.answer()
        await state.update_data(release=release)
        await _ask_surname(callback.message, state, storage, callback.from_user)

    @dp.message(AddTagStates.release)
    async def add_release_text(message: Message, state: FSMContext) -> None:
        release = (message.text or "").strip()
        if len(release) < 2:
            await message.answer("❌ Слишком короткое название релиза.")
            return
        await state.update_data(release=release)
        await _ask_surname(message, state, storage, message.from_user)

    async def _ask_surname(
        target: Message,
        state: FSMContext,
        storage: Storage,
        user,
    ) -> None:
        profile = storage.get_profile(user.id) if user else None
        if profile:
            await state.update_data(surname=profile["surname"])
            await state.set_state(AddTagStates.transfer_date)
            await target.answer(
                f"👤 Разработчик: <b>{esc(profile['surname'])}</b> "
                "(из вашего профиля)\n\n"
                f"📅 Дата передачи — сегодня ({_today_str()})?\n"
                "Напишите другую дату (<code>DD.MM.YYYY</code>) или "
                "нажмите кнопку ниже.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"📅 Сегодня ({_today_str()})",
                                callback_data="add:date:today",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Отмена", callback_data="add:cancel"
                            )
                        ],
                    ]
                ),
            )
            return

        names = _developer_names(monitor)
        await state.set_state(AddTagStates.surname)
        await target.answer(
            "👤 <b>Ваша фамилия</b> (как в таблице):\n\n"
            "Выберите из списка или напишите текстом.",
            parse_mode=ParseMode.HTML,
            reply_markup=_surname_keyboard(names),
        )

    @dp.callback_query(F.data.startswith("add:surname:"))
    async def add_surname_cb(callback: CallbackQuery, state: FSMContext) -> None:
        surname = callback.data.split(":", 2)[2]
        await callback.answer()
        await state.update_data(surname=surname)
        await state.set_state(AddTagStates.transfer_date)
        if callback.message:
            await safe_edit(
                callback.message,
                f"📅 Дата передачи — сегодня ({_today_str()})?\n"
                "Или напишите свою (<code>DD.MM.YYYY</code>).",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=f"📅 Сегодня ({_today_str()})",
                                callback_data="add:date:today",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Отмена", callback_data="add:cancel"
                            )
                        ],
                    ]
                ),
            )

    @dp.message(AddTagStates.surname)
    async def add_surname_text(message: Message, state: FSMContext) -> None:
        surname = (message.text or "").strip()
        if len(surname) < 2 or len(surname) > 25:
            await message.answer("❌ Фамилия должна быть от 2 до 25 символов.")
            return
        await state.update_data(surname=surname)
        await state.set_state(AddTagStates.transfer_date)
        await message.answer(
            f"📅 Дата передачи — сегодня ({_today_str()})?\n"
            "Или напишите свою (<code>DD.MM.YYYY</code>).",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=f"📅 Сегодня ({_today_str()})",
                            callback_data="add:date:today",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена", callback_data="add:cancel"
                        )
                    ],
                ]
            ),
        )

    @dp.callback_query(F.data == "add:date:today")
    async def add_date_today(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.update_data(transfer_date=_today_str())
        await _show_add_preview(callback.message, state)

    @dp.message(AddTagStates.transfer_date)
    async def add_date_text(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        parsed = parse_flexible_date(raw)
        if not parsed:
            await message.answer(
                "❌ Неверная дата. Пример: <code>03.07.2026</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        await state.update_data(transfer_date=parsed.strftime("%d.%m.%Y"))
        await _show_add_preview(message, state)

    async def _show_add_preview(target: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.set_state(AddTagStates.confirm)
        text = format_add_preview(
            tags=data["tags"],
            release=data["release"],
            surname=data["surname"],
            transfer_date=data["transfer_date"],
        )
        await target.answer(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_confirm_keyboard("add"),
        )

    @dp.callback_query(F.data == "add:save", AddTagStates.confirm)
    async def add_save(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not callback.message:
            await callback.answer()
            return
        data = await state.get_data()
        await callback.answer("Записываю…")
        await safe_edit(callback.message, "⏳ Записываю в таблицу…")

        entries = [
            {
                "transfer_date": data["transfer_date"],
                "developer": data["surname"],
                "tag": tag,
                "release": data["release"],
            }
            for tag in data["tags"]
        ]
        try:
            row_numbers = await monitor.sheets.append_registry_rows(entries)
        except Exception as exc:
            logger.exception("Failed to append rows")
            await safe_edit(
                callback.message,
                f"❌ Не удалось записать в таблицу:\n<code>{exc}</code>",
            )
            return

        user = callback.from_user
        storage.set_profile(
            user.id,
            data["surname"],
            username=user.username or "",
            full_name=user.full_name or "",
        )
        for tag, row_number in zip(data["tags"], row_numbers, strict=True):
            storage.add_tag_author(tag, user.id)
            storage.set_row_author(row_number, user.id)

        await state.clear()
        lines = "\n".join(
            f"• стр. {rn}: <code>{esc(tag)}</code>"
            for tag, rn in zip(data["tags"], row_numbers, strict=True)
        )
        await safe_edit(
            callback.message,
            "✅ <b>Теги добавлены в реестр</b>\n\n"
            f"{lines}\n\n"
            "Статус пока пустой — образ попадёт на проверку после передачи в ИБ.\n"
            "Ваши образы: /my",
        )
        await _notify_admins(
            bot,
            f"➕ <b>Новые теги через бота</b>\n"
            f"👤 {esc(data['surname'])} (@{esc(user.username or '—')})\n\n{lines}",
        )
        await _ensure_fresh(monitor, force=True)

    @dp.callback_query(F.data == "add:cancel")
    async def add_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        if callback.message:
            await safe_edit(callback.message, "❌ Добавление отменено.")

    # --- Corrected tag flow --------------------------------------------------

    @dp.callback_query(F.data.startswith("fix:pick:"))
    async def fix_pick(callback: CallbackQuery, state: FSMContext) -> None:
        row_number = int(callback.data.split(":")[2])
        data = await state.get_data()
        corrected = data.get("corrected_tag")
        row = monitor.get_row(row_number)
        await callback.answer()
        if not row:
            await state.clear()
            if callback.message:
                await safe_edit(callback.message, "Строка не найдена.")
            return
        if corrected:
            await state.set_state(FixTagStates.confirm)
            await state.update_data(row_number=row_number, corrected_tag=corrected)
            if callback.message:
                await safe_edit(
                    callback.message,
                    "🔧 <b>Подтверждение исправленного тега</b>\n\n"
                    f"Строка {row.row_number}\n"
                    f"Было: <code>{esc(row.tag)}</code>\n"
                    f"Исправленный тег: <code>{esc(corrected)}</code>\n\n"
                    f"Статус → «{STATUS_ON_REVIEW}»",
                    reply_markup=_confirm_keyboard("fix"),
                )
            return
        await state.set_state(FixTagStates.tag)
        await state.update_data(row_number=row_number)
        if callback.message:
            await safe_edit(
                callback.message,
                "🔧 <b>Исправленный тег</b>\n\n"
                f"Строка {row.row_number}, исходный тег:\n"
                f"<code>{esc(row.tag)}</code>\n\n"
                "Пришлите новый (пересобранный) образ одной строкой.\n"
                "Отмена — /cancel",
            )

    @dp.callback_query(F.data == "act:add")
    async def cb_act_add(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if callback.message:
            await cmd_add(callback.message, state)

    @dp.callback_query(F.data == "act:my")
    async def cb_act_my(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await cmd_my(callback.message)

    @dp.callback_query(F.data.startswith("fix:start:"))
    async def fix_start(callback: CallbackQuery, state: FSMContext) -> None:
        row_number = int(callback.data.split(":")[2])
        await state.set_state(FixTagStates.tag)
        await state.update_data(row_number=row_number)
        await callback.answer()
        row = monitor.get_row(row_number)
        if callback.message and row:
            await callback.message.answer(
                "🔧 <b>Исправленный тег</b>\n\n"
                f"Строка {row.row_number}:\n"
                f"<code>{esc(row.tag)}</code>\n\n"
                "Пришлите пересобранный образ одной строкой.",
                parse_mode=ParseMode.HTML,
            )

    @dp.message(FixTagStates.tag)
    async def fix_tag_input(message: Message, state: FSMContext) -> None:
        tag = (message.text or "").strip()
        if not _is_plausible_tag(tag):
            await message.answer(
                "❌ Не похоже на тег образа. Одна строка, без пробелов."
            )
            return
        data = await state.get_data()
        row_number = data["row_number"]
        row = monitor.get_row(row_number)
        if not row:
            await state.clear()
            await message.answer("Строка не найдена — обновите данные (/refresh).")
            return
        if not row.is_failed():
            await state.clear()
            await message.answer(
                "Эта строка уже не в статусе «не прошло проверку». "
                "Исправленный тег можно добавить только после провала."
            )
            return
        await state.update_data(corrected_tag=tag)
        await state.set_state(FixTagStates.confirm)
        await message.answer(
            "🔧 <b>Подтверждение исправленного тега</b>\n\n"
            f"Строка {row.row_number}\n"
            f"Было: <code>{esc(row.tag)}</code>\n"
            f"Исправленный тег: <code>{esc(tag)}</code>\n\n"
            f"Статус будет сброшен на «{STATUS_ON_REVIEW}» — образ снова "
            "отправится на проверку ИБ.\n"
            "Дата проверки будет очищена.",
            parse_mode=ParseMode.HTML,
            reply_markup=_confirm_keyboard("fix"),
        )

    @dp.callback_query(F.data == "fix:save", FixTagStates.confirm)
    async def fix_save(callback: CallbackQuery, state: FSMContext) -> None:
        if not callback.from_user or not callback.message:
            await callback.answer()
            return
        data = await state.get_data()
        row_number = data["row_number"]
        corrected = data["corrected_tag"]
        await callback.answer("Записываю…")
        await safe_edit(callback.message, "⏳ Обновляю строку…")
        try:
            await monitor.sheets.submit_corrected_tag(row_number, corrected)
        except Exception as exc:
            logger.exception("Failed to submit corrected tag")
            await safe_edit(callback.message, f"❌ Ошибка записи:\n<code>{exc}</code>")
            return

        storage.clear_pending_fix(callback.from_user.id, row_number)
        await state.clear()
        await safe_edit(
            callback.message,
            "✅ <b>Исправленный тег записан</b>\n\n"
            f"Строка {row_number}\n"
            f"<code>{esc(corrected)}</code>\n\n"
            f"Статус: <b>{STATUS_ON_REVIEW}</b> — образ снова на проверке у ИБ.",
        )
        await _notify_admins(
            bot,
            f"🔧 <b>Исправленный тег</b> (@{esc(callback.from_user.username or '—')})\n"
            f"Стр. {row_number}: <code>{esc(corrected)}</code>\n"
            f"Статус → {STATUS_ON_REVIEW}",
        )
        await _ensure_fresh(monitor, force=True)

    @dp.callback_query(F.data == "fix:cancel")
    async def fix_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        if callback.message:
            await safe_edit(callback.message, "❌ Отменено.")

    @dp.message(F.text, StateFilter(None), PendingFixTagFilter(storage))
    async def maybe_corrected_tag(message: Message, state: FSMContext) -> None:
        """If user has pending failed rows, a lone tag message starts fix flow."""
        if not message.from_user:
            return
        text = (message.text or "").strip()
        pending = storage.get_pending_fixes(message.from_user.id)
        if not pending:
            return
        ok, _ = await _ensure_fresh(monitor)
        if not ok:
            return
        rows = [
            monitor.get_row(rn)
            for rn in pending
            if monitor.get_row(rn) and monitor.get_row(rn).is_failed()
        ]
        if not rows:
            storage.clear_all_pending_fixes(message.from_user.id)
            return
        if len(rows) == 1:
            row = rows[0]
            await state.set_state(FixTagStates.confirm)
            await state.update_data(row_number=row.row_number, corrected_tag=text)
            await message.answer(
                "🔧 Похоже, это исправленный тег для провалившейся проверки:\n\n"
                f"Строка {row.row_number}: <code>{esc(row.tag)}</code>\n"
                f"Новый тег: <code>{esc(text)}</code>\n\n"
                f"Записать в «Исправленный тег» и вернуть статус "
                f"«{STATUS_ON_REVIEW}»?",
                parse_mode=ParseMode.HTML,
                reply_markup=_confirm_keyboard("fix"),
            )
            return
        await state.set_state(FixTagStates.pick_row)
        await state.update_data(corrected_tag=text)
        await message.answer(
            "🔧 У вас несколько образов с провалом. Для какого этот тег?",
            reply_markup=_fix_pick_keyboard(rows),
        )


def _developer_names(monitor: RegistryMonitor) -> list[str]:
    return [name for name, _, _ in monitor.developers_summary()]


async def _ensure_fresh(monitor: RegistryMonitor, *, force: bool = False) -> tuple[bool, str | None]:
    try:
        await monitor.ensure_fresh(force=force)
        return True, None
    except Exception as exc:
        logger.exception("Failed to refresh registry")
        return False, str(exc)


def resolve_developer_user_ids(
    storage: Storage,
    row: ImageRow,
) -> list[int]:
    """Who should get a personal notification for this row."""
    author = storage.row_author(row.row_number)
    if author:
        return [author]
    return storage.user_ids_by_surname(row.developer)
