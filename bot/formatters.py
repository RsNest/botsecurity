from __future__ import annotations

from bot.config import FIELD_NAMES, settings
from bot.models import ImageRow, RowChange

SHEET_URL = (
    f"https://docs.google.com/spreadsheets/d/{settings.spreadsheet_id}/"
    f"edit#gid={settings.sheet_gid}"
)


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
    return labels.get(status, row.status)


def format_row_brief(row: ImageRow) -> str:
    lines = [
        f"• <b>{row.short_tag()}</b>",
        f"  👤 {row.developer or '—'} | 📅 {row.transfer_date or '—'}",
        f"  {_status_label(row)}",
    ]
    if row.release:
        lines.append(f"  🏷 {row.release}")
    return "\n".join(lines)


def format_row_detail(row: ImageRow) -> str:
    lines = [
        f"<b>Строка {row.row_number}</b>",
        f"Тег: <code>{row.tag or '—'}</code>",
    ]
    if row.corrected_tag:
        lines.append(f"Исправленный тег: <code>{row.corrected_tag}</code>")
    lines.extend(
        [
            f"Разработчик: {row.developer or '—'}",
            f"Дата передачи: {row.transfer_date or '—'}",
            f"Релиз: {row.release or '—'}",
            f"Статус: {_status_label(row)}",
        ]
    )
    if row.check_date:
        lines.append(f"Дата проверки: {row.check_date}")
    if row.final_tag:
        lines.append(f"Итоговый тег: <code>{row.final_tag}</code>")
    if row.uploaded_mf:
        lines.append(f"Залито в МФ: {row.uploaded_mf}")
    if row.actual_release_date:
        lines.append(f"Дата релиза: {row.actual_release_date}")
    return "\n".join(lines)


def format_change(change: RowChange) -> str:
    row = change.row
    if change.change_type == "new":
        header = "🆕 <b>Новый образ в реестре</b>"
    else:
        header = "✏️ <b>Изменение в реестре</b>"

    body = format_row_detail(row)
    if change.change_type == "updated" and change.changed_fields:
        fields = []
        for key, (_, new_val) in change.changed_fields.items():
            label = FIELD_NAMES.get(key, key)
            fields.append(f"{label}: {new_val}")
        if fields:
            body += "\n\nОбновлено:\n" + "\n".join(f"• {line}" for line in fields)
    return f"{header}\n\n{body}\n\n<a href=\"{SHEET_URL}\">Открыть таблицу</a>"


def format_pending_list(rows: list[ImageRow], title: str) -> str:
    if not rows:
        return f"<b>{title}</b>\n\nНет записей."
    chunks = [f"<b>{title}</b>", f"Всего: {len(rows)}", ""]
    for row in rows[:30]:
        chunks.append(format_row_brief(row))
    if len(rows) > 30:
        chunks.append(f"\n… и ещё {len(rows) - 30}")
    chunks.append(f'\n<a href="{SHEET_URL}">Открыть таблицу</a>')
    return "\n".join(chunks)


def format_status_summary(summary: dict[str, int], total: int) -> str:
    return (
        "<b>Сводка по реестру образов ИБ</b>\n\n"
        f"Всего записей: {total}\n"
        f"⏳ Без статуса / ждут передачи: {summary['pending'] + summary['not_transferred']}\n"
        f"🔍 На проверке: {summary['on_review']}\n"
        f"✅ Прошло проверку: {summary['passed']}\n"
        f"❌ Не прошло проверку: {summary['failed']}\n"
        f"⚠️ Не передано: {summary['not_transferred']}\n"
        f'\n<a href="{SHEET_URL}">Открыть таблицу</a>'
    )


def format_reminder(rows: list[ImageRow]) -> str:
    title = "🔔 Напоминание: образы ждут передачи на проверку"
    return format_pending_list(rows, title)


def format_help(is_subscribed: bool) -> str:
    sub_state = "✅ вы подписаны на уведомления" if is_subscribed else "🔕 вы не подписаны"
    return (
        "<b>Бот реестра образов ИБ</b>\n\n"
        "Публичный бот для мониторинга Google-таблицы с образами.\n\n"
        "<b>Команды:</b>\n"
        "/pending — образы без статуса / не переданы\n"
        "/status — сводка по статусам\n"
        "/today — добавленные сегодня\n"
        "/by_dev фамилия — образы разработчика\n"
        "/stale 3 — висят без статуса ≥ N дней\n"
        "/subscribe — подписаться на уведомления\n"
        "/unsubscribe — отписаться\n"
        "/help — эта справка\n\n"
        f"{sub_state}\n"
        f'<a href="{SHEET_URL}">Открыть таблицу</a>'
    )


def format_welcome() -> str:
    return (
        "👋 <b>Реестр образов ИБ</b>\n\n"
        "Я слежу за Google-таблицей и присылаю:\n"
        "• новые образы от разработчиков\n"
        "• изменения статусов\n"
        "• напоминания о непереданных образах\n\n"
        "Вы подписаны на уведомления.\n"
        "Используйте /help для списка команд."
    )
