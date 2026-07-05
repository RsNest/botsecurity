from __future__ import annotations

from math import ceil

from bot.config import FIELD_NAMES, settings
from bot.models import ImageRow, RowChange, normalize_status
from bot.utils import esc

SHEET_URL = (
    f"https://docs.google.com/spreadsheets/d/{settings.spreadsheet_id}/"
    f"edit#gid={settings.sheet_gid}"
)

PAGE_SIZE = 10

_SHEET_LINK = f'<a href="{SHEET_URL}">Открыть таблицу</a>'


def _status_label(row: ImageRow) -> str:
    if not row.status:
        return "⏳ не передано"
    status = row.status_normalized()
    labels = {
        "на проверке": "🔍 на проверке",
        "прошло проверку": "✅ прошло проверку",
        "не прошло проверку": "❌ не прошло проверку",
        "не передано": "⚠️ не передано",
    }
    return labels.get(status, esc(row.status))


def format_row_brief(row: ImageRow, index: int | None = None) -> str:
    bullet = f"{index}." if index is not None else "•"
    lines = [
        f"{bullet} <b>{esc(row.short_tag())}</b>",
        f"  👤 {esc(row.developer) or '—'} | 📅 {esc(row.transfer_date) or '—'}",
        f"  {_status_label(row)}",
    ]
    if row.release:
        lines.append(f"  🏷 {esc(row.release)}")
    return "\n".join(lines)


def format_row_detail(row: ImageRow) -> str:
    lines = [
        f"<b>Строка {row.row_number}</b>",
        f"Тег: <code>{esc(row.tag) or '—'}</code>",
    ]
    if row.corrected_tag:
        lines.append(f"Исправленный тег: <code>{esc(row.corrected_tag)}</code>")
    lines.extend(
        [
            f"Разработчик: {esc(row.developer) or '—'}",
            f"Дата передачи: {esc(row.transfer_date) or '—'}",
            f"Релиз: {esc(row.release) or '—'}",
            f"Статус: {_status_label(row)}",
        ]
    )
    if row.check_date:
        lines.append(f"Дата проверки: {esc(row.check_date)}")
    if row.final_tag:
        lines.append(f"Итоговый тег: <code>{esc(row.final_tag)}</code>")
    if row.uploaded_mf:
        lines.append(f"Залито в МФ: {esc(row.uploaded_mf)}")
    if row.actual_release_date:
        lines.append(f"Дата релиза: {esc(row.actual_release_date)}")
    return "\n".join(lines)


def format_change(change: RowChange) -> str:
    row = change.row
    if change.change_type == "new":
        header = "🆕 <b>Новый образ в реестре</b>"
    elif change.change_type == "removed":
        header = "🗑 <b>Строка удалена из реестра</b>"
    elif _is_failed_status_change(change):
        header = "❌ <b>Образ не прошёл проверку ИБ</b>"
    elif _is_on_review_status_change(change):
        header = "🔍 <b>Образ передан на проверку ИБ</b>"
    elif _is_passed_status_change(change):
        header = "✅ <b>Образ прошёл проверку ИБ</b>"
    else:
        header = "✏️ <b>Изменение в реестре</b>"

    body = format_row_detail(row)
    if change.change_type == "updated" and change.changed_fields:
        fields = []
        for key, (old_val, new_val) in change.changed_fields.items():
            label = FIELD_NAMES.get(key, key)
            fields.append(f"{esc(label)}: {esc(old_val)} → {esc(new_val)}")
        if fields:
            body += "\n\nИзменения:\n" + "\n".join(f"• {line}" for line in fields)
    return f"{header}\n\n{body}\n\n{_SHEET_LINK}"


def _status_change_to(change: RowChange) -> str | None:
    status = change.changed_fields.get("status")
    return normalize_status(status[1]) if status else None


def _is_failed_status_change(change: RowChange) -> bool:
    return _status_change_to(change) == "не прошло проверку"


def _is_on_review_status_change(change: RowChange) -> bool:
    return _status_change_to(change) == "на проверке"


def _is_passed_status_change(change: RowChange) -> bool:
    return _status_change_to(change) == "прошло проверку"


def total_pages(rows_count: int) -> int:
    return max(1, ceil(rows_count / PAGE_SIZE))


def format_rows_page(
    rows: list[ImageRow],
    title: str,
    page: int = 0,
    footer: str = "",
) -> tuple[str, int, int]:
    """Return (text, page, total_pages) for a paginated list view."""
    text, page, pages, _ = format_rows_page_numbered(rows, title, page, footer)
    return text, page, pages


def format_rows_page_numbered(
    rows: list[ImageRow],
    title: str,
    page: int = 0,
    footer: str = "",
) -> tuple[str, int, int, list[int]]:
    """Paginated list where items are numbered 1..N within the page.

    Returns (text, page, total_pages, row_numbers_on_page) so the keyboard
    can attach detail buttons for each numbered item.
    """
    pages = total_pages(len(rows))
    page = max(0, min(page, pages - 1))

    if not rows:
        text = f"<b>{esc(title)}</b>\n\nНет записей."
        if footer:
            text += f"\n\n{footer}"
        text += f"\n\n{_SHEET_LINK}"
        return text, 0, 1, []

    start = page * PAGE_SIZE
    chunk = rows[start : start + PAGE_SIZE]

    header = f"<b>{esc(title)}</b>\nВсего: {len(rows)}"
    if pages > 1:
        header += f" · стр. {page + 1}/{pages}"

    parts = [header, ""]
    parts.extend(
        format_row_brief(row, index=i + 1) for i, row in enumerate(chunk)
    )
    if len(chunk) > 0:
        parts.append("\n🔎 Подробнее — кнопки с номерами ниже")
    if footer:
        parts.append(f"\n{footer}")
    parts.append(f"\n{_SHEET_LINK}")
    return "\n".join(parts), page, pages, [row.row_number for row in chunk]


def format_developers_list(devs: list[tuple[str, int, int]]) -> str:
    if not devs:
        return "<b>Разработчики</b>\n\nНет данных."
    parts = ["<b>👥 Разработчики в реестре</b>", ""]
    for name, total, pending in devs[:25]:
        line = f"• <b>{esc(name)}</b> — {total}"
        if pending:
            line += f" (⏳ {pending} ждут)"
        parts.append(line)
    parts.append("\nНажмите кнопку, чтобы посмотреть образы.")
    return "\n".join(parts)


def format_releases_list(releases: list[tuple[str, int]]) -> str:
    if not releases:
        return "<b>Релизы</b>\n\nНет данных."
    parts = ["<b>🏷 Релизы в реестре</b> (свежие сверху)", ""]
    for release, count in releases:
        parts.append(f"• <b>{esc(release)}</b> — {count}")
    parts.append("\nНажмите кнопку, чтобы посмотреть образы релиза.")
    return "\n".join(parts)


def format_digest(summary: dict[str, int], total: int, new_week: int, stale: int) -> str:
    return (
        "📬 <b>Еженедельный дайджест реестра ИБ</b>\n\n"
        f"Всего записей: {total}\n"
        f"🆕 Новых за неделю: {new_week}\n"
        f"⏳ Ожидают передачи: {summary['pending'] + summary['not_transferred']}\n"
        f"🔍 На проверке: {summary['on_review']}\n"
        f"✅ Прошло проверку: {summary['passed']}\n"
        f"❌ Не прошло проверку: {summary['failed']}\n"
        f"🕰 Висят без статуса ≥ 3 дней: {stale}\n"
        f"\n{_SHEET_LINK}"
    )


def format_status_summary(summary: dict[str, int], total: int, footer: str = "") -> str:
    text = (
        "<b>Сводка по реестру образов ИБ</b>\n\n"
        f"Всего записей: {total}\n"
        f"⏳ Без статуса / ждут передачи: {summary['pending'] + summary['not_transferred']}\n"
        f"🔍 На проверке: {summary['on_review']}\n"
        f"✅ Прошло проверку: {summary['passed']}\n"
        f"❌ Не прошло проверку: {summary['failed']}\n"
        f"⚠️ Не передано: {summary['not_transferred']}"
    )
    if summary.get("other"):
        text += f"\n❔ Прочее: {summary['other']}"
    if footer:
        text += f"\n\n{footer}"
    text += f"\n\n{_SHEET_LINK}"
    return text


def format_reminder(rows: list[ImageRow]) -> str:
    title = "🔔 Напоминание: образы ждут передачи на проверку"
    text, _, _ = format_rows_page(rows, title, page=0)
    return text


def format_help(is_subscribed: bool) -> str:
    sub_state = "✅ вы подписаны на уведомления" if is_subscribed else "🔕 вы не подписаны"
    return (
        "<b>Бот реестра образов ИБ</b>\n\n"
        "Публичный бот для мониторинга Google-таблицы с образами.\n\n"
        "🔎 <b>Поиск</b>: просто напишите текст в чат — например,\n"
        "<code>leadgen</code> или <code>Зуев api</code>\n\n"
        "<b>Списки:</b>\n"
        "/pending — ждут передачи на проверку\n"
        "/on_review — на проверке у ИБ\n"
        "/passed — прошли проверку\n"
        "/failed — не прошли проверку\n"
        "/today — добавленные сегодня\n"
        "/last — последние добавленные\n"
        "/stale 3 — висят без статуса ≥ N дней\n\n"
        "<b>Навигация:</b>\n"
        "/devs — по разработчикам (кнопки)\n"
        "/releases — по релизам (кнопки)\n"
        "/dates — выборка по датам (кнопки)\n"
        "/status — сводка по статусам\n\n"
        "<b>Поиск командой:</b>\n"
        "/find текст — поиск по всем полям\n"
        "/by_dev фамилия — образы разработчика\n\n"
        "<b>Подписка:</b>\n"
        "/subscribe — подписаться на уведомления\n"
        "/unsubscribe — отписаться\n\n"
        f"{sub_state}\n"
        f"{_SHEET_LINK}"
    )


def format_welcome() -> str:
    return (
        "👋 <b>Реестр образов ИБ</b>\n\n"
        "Я слежу за Google-таблицей и присылаю:\n"
        "• новые образы от разработчиков\n"
        "• изменения статусов\n"
        "• напоминания о непереданных образах\n\n"
        "🔎 <b>Поиск</b>: просто напишите текст в чат\n"
        "(например <code>leadgen</code> или <code>Зуев api</code>)\n\n"
        "Вы подписаны на уведомления.\n"
        "Кнопки ниже — быстрый доступ, /help — все команды."
    )
