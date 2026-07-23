from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

# --- Reply keyboard (нижнее меню) ---

BTN_ADD = "➕ Добавить тег"
BTN_FIX = "🔧 Исправленный тег"
BTN_MY = "📋 Мои образы"
BTN_PENDING = "⏳ Ожидают"
BTN_ON_REVIEW = "🔍 На проверке"
BTN_PASSED = "✅ Прошли"
BTN_FAILED = "❌ Не прошли"
BTN_STATUS = "📊 Статус"
BTN_BY_DATE = "📅 По датам"
BTN_TODAY = "📆 Сегодня"
BTN_REFRESH = "🔄 Обновить"
BTN_MENU = "🏠 Меню"
BTN_DEVS = "👥 Разработчики"
BTN_RELEASES = "🏷 Релизы"

REPLY_BUTTONS = {
    BTN_ADD,
    BTN_FIX,
    BTN_MY,
    BTN_PENDING,
    BTN_ON_REVIEW,
    BTN_PASSED,
    BTN_FAILED,
    BTN_STATUS,
    BTN_BY_DATE,
    BTN_TODAY,
    BTN_REFRESH,
    BTN_MENU,
    BTN_DEVS,
    BTN_RELEASES,
}


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_FIX)],
            [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_PENDING)],
            [KeyboardButton(text=BTN_ON_REVIEW), KeyboardButton(text=BTN_PASSED)],
            [KeyboardButton(text=BTN_FAILED), KeyboardButton(text=BTN_DEVS)],
            [KeyboardButton(text=BTN_RELEASES), KeyboardButton(text=BTN_STATUS)],
            [KeyboardButton(text=BTN_BY_DATE), KeyboardButton(text=BTN_TODAY)],
            [KeyboardButton(text=BTN_REFRESH)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Поиск: напишите текст, например leadgen",
    )


# --- Inline keyboards ---

def inline_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_ADD, callback_data="act:add"),
                InlineKeyboardButton(text=BTN_FIX, callback_data="act:fix"),
            ],
            [
                InlineKeyboardButton(text=BTN_MY, callback_data="act:my"),
                InlineKeyboardButton(text=BTN_PENDING, callback_data="act:pending"),
            ],
            [
                InlineKeyboardButton(text=BTN_ON_REVIEW, callback_data="act:on_review"),
                InlineKeyboardButton(text=BTN_PASSED, callback_data="act:passed"),
            ],
            [
                InlineKeyboardButton(text=BTN_FAILED, callback_data="act:failed"),
                InlineKeyboardButton(text=BTN_DEVS, callback_data="devs"),
            ],
            [
                InlineKeyboardButton(text=BTN_RELEASES, callback_data="rels"),
                InlineKeyboardButton(text=BTN_STATUS, callback_data="act:status"),
            ],
            [
                InlineKeyboardButton(text=BTN_BY_DATE, callback_data="date:start"),
                InlineKeyboardButton(text=BTN_REFRESH, callback_data="act:refresh"),
            ],
        ]
    )


def inline_date_field_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Дата передачи", callback_data="date:f:tr")],
            [InlineKeyboardButton(text="🗓 Дата проверки ИБ", callback_data="date:f:ch")],
            [InlineKeyboardButton(text="« Назад", callback_data="menu")],
        ]
    )


def inline_date_period_keyboard(date_field: str) -> InlineKeyboardMarkup:
    prefix = f"date:p:{date_field}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data=f"{prefix}:td"),
                InlineKeyboardButton(text="Вчера", callback_data=f"{prefix}:yd"),
            ],
            [
                InlineKeyboardButton(text="7 дней", callback_data=f"{prefix}:7d"),
                InlineKeyboardButton(text="30 дней", callback_data=f"{prefix}:30d"),
            ],
            [InlineKeyboardButton(text="✏️ Свой период", callback_data=f"{prefix}:cu")],
            [InlineKeyboardButton(text="« Назад", callback_data="date:start")],
        ]
    )


def inline_date_status_keyboard(date_field: str, period: str) -> InlineKeyboardMarkup:
    prefix = f"date:s:{date_field}:{period}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Прошли проверку", callback_data=f"{prefix}:ok")],
            [InlineKeyboardButton(text="❌ Не прошли", callback_data=f"{prefix}:fail")],
            [InlineKeyboardButton(text="📋 Все результаты", callback_data=f"{prefix}:all")],
            [InlineKeyboardButton(text="« Назад", callback_data=f"date:f:{date_field}")],
        ]
    )


def inline_back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_BY_DATE, callback_data="date:start"),
                InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
            ],
        ]
    )


def inline_paginated_menu(
    page_callback_prefix: str,
    page: int,
    pages: int,
    row_numbers: list[int] | None = None,
) -> InlineKeyboardMarkup:
    """Keyboard with numbered detail buttons, prev/next nav, and shortcuts.

    page_callback_prefix already encodes the view, e.g. "pg:pending" or
    "pgd:tr:2026-06-01_2026-06-30:fail". The page index is appended.
    row_numbers are registry row numbers of items on this page; each gets a
    numbered button opening the detail card.
    """
    rows: list[list[InlineKeyboardButton]] = []

    if row_numbers:
        detail_row: list[InlineKeyboardButton] = []
        for i, rn in enumerate(row_numbers):
            detail_row.append(
                InlineKeyboardButton(text=str(i + 1), callback_data=f"row:{rn}")
            )
            if len(detail_row) == 5:
                rows.append(detail_row)
                detail_row = []
        if detail_row:
            rows.append(detail_row)

    if pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 0:
            nav.append(
                InlineKeyboardButton(
                    text="◀️", callback_data=f"{page_callback_prefix}:{page - 1}"
                )
            )
        nav.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{pages}", callback_data="noop"
            )
        )
        if page < pages - 1:
            nav.append(
                InlineKeyboardButton(
                    text="▶️", callback_data=f"{page_callback_prefix}:{page + 1}"
                )
            )
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text=BTN_BY_DATE, callback_data="date:start"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_detail_card() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu")],
        ]
    )


def inline_developers_keyboard(devs: list[tuple[str, int, int]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for name, total, pending in devs[:24]:
        label = f"{name} ({total})"
        if pending:
            label = f"{name} ({total}·⏳{pending})"
        # callback_data limit is 64 bytes; Cyrillic is 2 bytes/char
        pair.append(InlineKeyboardButton(text=label, callback_data=f"dev:{name[:25]}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_report_menu(
    token: str,
    *,
    failed: int,
    total: int,
    can_write: bool,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if failed:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"❌ Непрошедшие ({failed})",
                    callback_data=f"rep:fail:{token}:0",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"📋 Все образы ({total})",
                callback_data=f"rep:all:{token}:0",
            )
        ]
    )
    if can_write:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💾 Добавить результаты в таблицу?",
                    callback_data=f"rep:ask:{token}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="❌ Закрыть", callback_data=f"rep:cancel:{token}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_report_write_options(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да: обновить + добавить новые",
                    callback_data=f"rep:write:{token}:all",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Только найденные в таблице",
                    callback_data=f"rep:write:{token}:matched",
                )
            ],
            [
                InlineKeyboardButton(
                    text="« Назад", callback_data=f"rep:menu:{token}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"rep:cancel:{token}"
                )
            ],
        ]
    )


def inline_report_confirm(token: str) -> InlineKeyboardMarkup:
    """Backward-compatible alias: ask write options."""
    return inline_report_write_options(token)


def inline_report_list(
    token: str,
    *,
    mode: str,
    page: int,
    pages: int,
    items: list[tuple[int, str]],
) -> InlineKeyboardMarkup:
    """items: (match_index, button_label)."""
    rows: list[list[InlineKeyboardButton]] = []
    for idx, label in items:
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=f"rep:img:{token}:{idx}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"rep:{mode}:{token}:{page - 1}",
            )
        )
    if pages > 1:
        nav.append(
            InlineKeyboardButton(
                text=f"{page + 1}/{pages}",
                callback_data="noop",
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"rep:{mode}:{token}:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text="« К сводке", callback_data=f"rep:menu:{token}"),
            InlineKeyboardButton(text="❌", callback_data=f"rep:cancel:{token}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def inline_report_detail(token: str, index: int, mode: str = "fail") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="« К списку",
                    callback_data=f"rep:{mode}:{token}:0",
                ),
                InlineKeyboardButton(
                    text="« К сводке",
                    callback_data=f"rep:menu:{token}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="💾 В таблицу?",
                    callback_data=f"rep:ask:{token}",
                )
            ],
        ]
    )


def inline_fix_tag(row_number: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔧 Отправить исправленный тег",
                    callback_data=f"fix:start:{row_number}",
                )
            ],
        ]
    )


def inline_releases_keyboard(releases: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for release, count in releases[:20]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{release} ({count})", callback_data=f"rel:{release[:55]}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
