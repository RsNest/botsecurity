from __future__ import annotations

from datetime import datetime
from math import ceil
from zoneinfo import ZoneInfo

from bot.config import FIELD_NAMES, STATUS_NOT_TRANSFERRED, settings
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


def format_report_preview(matches, can_write: bool) -> str:
    """Preview of parsed IB scan reports and their proposed verdicts."""
    failed = [m for m in matches if not m.report.passed]
    passed = [m for m in matches if m.report.passed]
    unmatched = [m for m in matches if m.row is None]

    parts = [
        "🛡 <b>Отчёт сканирования ИБ</b>",
        f"Образов в отчёте: {len(matches)} · "
        f"✅ прошло: {len(passed)} · ❌ не прошло: {len(failed)}",
        "",
    ]

    def line(m) -> str:
        r = m.report
        icon = "✅" if r.passed else "❌"
        counts = []
        if r.critical:
            counts.append(f"crit {r.critical}")
        if r.high:
            counts.append(f"high {r.high}")
        suffix = f" ({', '.join(counts)})" if counts else ""
        row_ref = f" → стр. {m.row.row_number}" if m.row else " → ⚠️ не найден в таблице"
        return f"{icon} <code>{esc(r.short_name)}</code>{suffix}{row_ref}"

    if failed:
        parts.append("<b>Не прошли проверку:</b>")
        parts.extend(line(m) for m in failed)
        parts.append("")
    if passed:
        parts.append("<b>Прошли проверку:</b>")
        parts.extend(line(m) for m in passed)
        parts.append("")
    if unmatched:
        parts.append(
            f"⚠️ Не сопоставлено с таблицей: {len(unmatched)} "
            "(статус для них проставлен не будет)."
        )
        parts.append("")

    applicable = sum(1 for m in matches if m.row is not None)
    if not can_write:
        parts.append(
            "🔒 <b>Запись в таблицу недоступна</b> — нет credentials.json "
            "с правами редактора. Могу только показать вердикты."
        )
    elif applicable:
        parts.append(
            f"Нажмите кнопку, чтобы проставить статусы в таблице ({applicable} строк)."
        )
    return "\n".join(parts)


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


def _fmt_local_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        local = dt.astimezone(ZoneInfo(settings.timezone))
        return local.strftime("%d.%m %H:%M")
    except ValueError:
        return iso[:16]


def _user_label(row: dict) -> str:
    name = row.get("full_name") or ""
    username = row.get("username") or ""
    label = esc(name) if name else f"id{row.get('user_id')}"
    if username:
        label += f" (@{esc(username)})"
    return label


def format_users_overview(overview: dict, recent: list[dict]) -> str:
    days = overview["days"]
    parts = [
        "👥 <b>Активность пользователей</b>",
        f"Всего пользователей за всё время: {overview['total_users']}",
        f"Активных за {days} дн.: {overview['active_users']} "
        f"({overview['actions_period']} действий)",
        "",
    ]

    if overview["top_users"]:
        parts.append(f"<b>Топ пользователей за {days} дн.:</b>")
        for i, u in enumerate(overview["top_users"], 1):
            parts.append(
                f"{i}. {_user_label(u)} — {u['cnt']} действ., "
                f"посл.: {_fmt_local_time(u['last_at'])}"
            )
        parts.append("")

    if overview["top_actions"]:
        parts.append(f"<b>Что запрашивают (за {days} дн.):</b>")
        for a in overview["top_actions"]:
            parts.append(f"• {esc(a['action'])} — {a['cnt']}")
        parts.append("")

    if recent:
        parts.append("<b>Последние действия:</b>")
        for r in recent:
            line = (
                f"{_fmt_local_time(r['at'])} · {_user_label(r)} · "
                f"{esc(r['action'])}"
            )
            detail = (r.get("detail") or "").strip()
            if detail and detail != r["action"]:
                line += f" <i>{esc(detail[:40])}</i>"
            parts.append(line)

    parts.append("\nПериод: <code>/users 30</code> · история: <code>/user 123456</code>")
    return "\n".join(parts)


def format_user_history(user_id: int, items: list[dict]) -> str:
    if not items:
        return f"Действий пользователя <code>{user_id}</code> не найдено."
    parts = [f"📜 <b>История пользователя</b> <code>{user_id}</code>", ""]
    for r in items:
        line = f"{_fmt_local_time(r['at'])} · {esc(r['action'])}"
        detail = (r.get("detail") or "").strip()
        if detail and detail != r["action"]:
            line += f" <i>{esc(detail[:60])}</i>"
        parts.append(line)
    return "\n".join(parts)


def format_add_preview(
    *,
    tags: list[str],
    release: str,
    surname: str,
    transfer_date: str,
) -> str:
    tag_lines = "\n".join(f"• <code>{esc(t)}</code>" for t in tags)
    return (
        "➕ <b>Проверьте перед записью</b>\n\n"
        f"👤 Разработчик: <b>{esc(surname)}</b>\n"
        f"📅 Дата передачи: {esc(transfer_date)}\n"
        f"🏷 Релиз: <b>{esc(release)}</b>\n\n"
        f"Теги ({len(tags)}):\n{tag_lines}\n\n"
        "Статус будет пустым — образ попадёт в реестр как новый."
    )


def format_my_rows(surname: str, rows: list[ImageRow]) -> str:
    if not rows:
        return (
            f"📋 <b>Ваши образы</b> ({esc(surname)})\n\n"
            "Пока ничего не найдено по вашей фамилии в реестре."
        )
    parts = [f"📋 <b>Ваши образы</b> ({esc(surname)}) · {len(rows)}", ""]
    for row in rows[:20]:
        parts.append(format_row_brief(row))
        if row.corrected_tag:
            parts.append(f"  🔧 исправленный: <code>{esc(row.corrected_tag)}</code>")
    if len(rows) > 20:
        parts.append(f"\n… и ещё {len(rows) - 20}")
    parts.append("\nДобавить новый тег — /add")
    return "\n".join(parts)


def format_personal_status_change(change: RowChange) -> str | None:
    """Personal notification text for status changes. None if not status-related."""
    row = change.row
    status = change.changed_fields.get("status")
    if change.change_type == "updated" and status:
        new_status = normalize_status(status[1])
        if new_status == "на проверке":
            return (
                "🔍 <b>Ваш образ передан на проверку ИБ</b>\n\n"
                + format_row_detail(row)
            )
        if new_status == "прошло проверку":
            return (
                "✅ <b>Ваш образ прошёл проверку ИБ</b>\n\n"
                + format_row_detail(row)
            )
        if new_status == "не прошло проверку":
            return (
                "❌ <b>Ваш образ не прошёл проверку ИБ</b>\n\n"
                + format_row_detail(row)
                + "\n\nКогда пересоберёте образ — пришлите новый тег сюда "
                "или нажмите кнопку ниже. Бот запишет его в «Исправленный тег» "
                f"и вернёт статус «{STATUS_NOT_TRANSFERRED}»."
            )
    if change.change_type == "new":
        return "🆕 <b>Ваш образ добавлен в реестр</b>\n\n" + format_row_detail(row)
    return None


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
        "<b>Разработчикам:</b>\n"
        "/add — добавить тег в реестр (дата, фамилия, тег, релиз)\n"
        "/my — ваши образы и их статусы\n"
        "/profile Фамилия — ваш профиль для /my и уведомлений\n"
        "После провала проверки — пришлите исправленный тег: бот запишет "
        "его в таблицу и вернёт статус «не передано»\n\n"
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
        "🛡 <b>Отчёты ИБ</b> (для администратора):\n"
        "пришлите архив сканирования (.7z / .zip) — бот разберёт его, "
        "определит вердикты (high/critical → не прошло) и проставит "
        "статусы в таблице.\n\n"
        f"{sub_state}\n"
        f"{_SHEET_LINK}"
    )


def format_welcome() -> str:
    return (
        "👋 <b>Привет! Я бот реестра образов ИБ.</b>\n\n"
        "Я слежу за <a href=\""
        + SHEET_URL
        + "\">Google-таблицей</a>, куда разработчики добавляют "
        "docker-образы для проверки безопасности. Помогаю не лазить в "
        "таблицу руками: показываю статусы, ищу образы и сам присылаю "
        "уведомления.\n\n"
        "<b>Что я умею автоматически:</b>\n"
        "• сообщать о новых образах и любых изменениях в реестре\n"
        "• присылать отдельное уведомление, когда образ прошёл "
        "или не прошёл проверку ИБ\n"
        "• напоминать в рабочие дни про образы, которые ещё не переданы "
        "на проверку\n"
        "• по понедельникам присылать дайджест за неделю\n"
        "• лично сообщать вам, когда <b>ваш</b> образ сменил статус\n\n"
        "➕ <b>Разработчикам:</b> /add — добавить тег прямо из бота, "
        "/my — ваши образы. Если проверка не прошла — пришлите "
        "пересобранный тег, бот запишет его как исправленный и "
        "вернёт образ на повторную передачу (статус «не передано»).\n\n"
        "🔎 <b>Главная фишка — поиск:</b> просто напишите текст в чат.\n"
        "Например: <code>leadgen</code>, <code>Зуев api</code>, "
        "<code>15.06.2026</code> (покажу образы за дату).\n\n"
        "<b>Кнопки внизу экрана:</b>\n"
        "➕ <b>Добавить тег</b> — записать образ в реестр\n"
        "📋 <b>Мои образы</b> — только ваши теги и статусы\n"
        "⏳ <b>Ожидают</b> — образы без статуса, ещё не переданы ИБ\n"
        "🔍 <b>На проверке</b> — сейчас проверяются у ИБ\n"
        "✅ <b>Прошли</b> / ❌ <b>Не прошли</b> — результаты проверок\n"
        "👥 <b>Разработчики</b> — образы по конкретному человеку\n"
        "🏷 <b>Релизы</b> — образы по релизу\n"
        "📊 <b>Статус</b> — общая сводка: сколько где\n"
        "📅 <b>По датам</b> — выборка за период (прошли / не прошли)\n"
        "📆 <b>Сегодня</b> — что добавили сегодня\n"
        "🔄 <b>Обновить</b> — принудительно перечитать таблицу\n\n"
        "В каждом списке нумерованные кнопки открывают карточку образа "
        "со всеми полями, стрелки ◀️ ▶️ листают страницы.\n\n"
        "✅ Вы подписаны на уведомления (отключить — /unsubscribe).\n"
        "Все команды — /help"
    )
