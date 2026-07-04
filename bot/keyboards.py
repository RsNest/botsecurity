from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

# --- Reply keyboard (нижнее меню) ---

BTN_PENDING = "⏳ Ожидают"
BTN_ON_REVIEW = "🔍 На проверке"
BTN_PASSED = "✅ Прошли"
BTN_FAILED = "❌ Не прошли"
BTN_STATUS = "📊 Статус"
BTN_BY_DATE = "📅 По датам"
BTN_TODAY = "📆 Сегодня"
BTN_REFRESH = "🔄 Обновить"
BTN_MENU = "🏠 Меню"

REPLY_BUTTONS = {
    BTN_PENDING,
    BTN_ON_REVIEW,
    BTN_PASSED,
    BTN_FAILED,
    BTN_STATUS,
    BTN_BY_DATE,
    BTN_TODAY,
    BTN_REFRESH,
    BTN_MENU,
}


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PENDING), KeyboardButton(text=BTN_ON_REVIEW)],
            [KeyboardButton(text=BTN_PASSED), KeyboardButton(text=BTN_FAILED)],
            [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_BY_DATE)],
            [KeyboardButton(text=BTN_TODAY), KeyboardButton(text=BTN_REFRESH)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие или /help",
    )


# --- Inline keyboards ---

def inline_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_PENDING, callback_data="act:pending"),
                InlineKeyboardButton(text=BTN_ON_REVIEW, callback_data="act:on_review"),
            ],
            [
                InlineKeyboardButton(text=BTN_PASSED, callback_data="act:passed"),
                InlineKeyboardButton(text=BTN_FAILED, callback_data="act:failed"),
            ],
            [
                InlineKeyboardButton(text=BTN_STATUS, callback_data="act:status"),
                InlineKeyboardButton(text=BTN_BY_DATE, callback_data="date:start"),
            ],
            [InlineKeyboardButton(text=BTN_REFRESH, callback_data="act:refresh")],
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
