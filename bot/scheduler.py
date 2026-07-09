from __future__ import annotations

import asyncio
import logging
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from datetime import date, timedelta

from bot.config import settings
from bot.formatters import format_audit_alert, format_change, format_digest, format_personal_status_change, format_reminder, format_sla_reminder
from bot.keyboards import inline_fix_tag
from bot.models import RowChange
from bot.monitor import RegistryMonitor
from bot.submit import resolve_developer_user_ids
from bot.storage import Storage
from bot.utils import safe_send

logger = logging.getLogger(__name__)


async def broadcast_changes(
    bot: Bot,
    storage: Storage,
    changes: list[RowChange],
) -> None:
    if not changes:
        return
    subscribers = storage.list_subscribers()
    if not subscribers:
        return
    # Cap noisy bursts: if a huge number of rows changed at once, summarise.
    if len(changes) > 15:
        text = (
            f"✏️ <b>В реестре {len(changes)} изменений</b>\n\n"
            "Слишком много за раз — откройте бота и посмотрите /status, "
            "/pending или таблицу."
        )
        for chat_id in subscribers:
            await safe_send(bot, chat_id, text)
            await asyncio.sleep(0.05)
        return

    for change in changes:
        personal = format_personal_status_change(change)
        status = change.changed_fields.get("status")
        dev_ids: set[int] = set()
        if personal:
            dev_ids = set(resolve_developer_user_ids(storage, change.row))

        text = format_change(change)
        for chat_id in subscribers:
            if chat_id in dev_ids:
                continue
            mode = storage.notification_mode(chat_id)
            new_status = status[1] if status else ""
            failed = new_status.strip().lower() == "не прошло проверку"
            if mode in {"off", "mine", "digest"} or (mode == "fail" and not failed):
                continue
            await safe_send(bot, chat_id, text)
            await asyncio.sleep(0.05)

        if not personal or not dev_ids:
            continue
        kb = None
        if status and (status[1] or "").strip().lower() == "не прошло проверку":
            kb = inline_fix_tag(change.row.row_number)
            for uid in dev_ids:
                storage.set_pending_fix(uid, change.row.row_number)
        for uid in dev_ids:
            if storage.notification_mode(uid) == "off":
                continue
            await safe_send(bot, uid, personal, reply_markup=kb)
            await asyncio.sleep(0.05)


async def broadcast_reminder(
    bot: Bot,
    storage: Storage,
    monitor: RegistryMonitor,
) -> None:
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
        await safe_send(bot, chat_id, text)
        await asyncio.sleep(0.05)


async def broadcast_audit_issues(bot: Bot, issues: list) -> None:
    if not issues or not settings.admin_ids:
        return
    text = format_audit_alert(issues)
    for admin_id in settings.admin_ids:
        await safe_send(bot, admin_id, text)
        await asyncio.sleep(0.05)


async def process_audit(
    bot: Bot,
    monitor: RegistryMonitor,
    storage: Storage,
) -> int:
    """Track registry issues; notify admins about newly detected ones."""
    issues = monitor.audit_issues()
    current = {issue.key() for issue in issues}
    previous = storage.get_audit_issue_keys()

    if not storage.audit_bootstrapped():
        storage.set_audit_issue_keys(current, bootstrapped=True)
        logger.info(
            "Audit bootstrap: tracking %s issues without notification",
            len(current),
        )
        return len(issues)

    new_keys = current - previous
    if new_keys:
        new_issues = [issue for issue in issues if issue.key() in new_keys]
        logger.warning("Audit: %s new issue(s) detected", len(new_issues))
        await broadcast_audit_issues(bot, new_issues)

    if current != previous:
        storage.set_audit_issue_keys(current)
    return len(issues)


async def scheduled_scan(
    bot: Bot,
    monitor: RegistryMonitor,
    storage: Storage,
) -> None:
    try:
        result = await monitor.ensure_fresh(force=True)
        await broadcast_changes(bot, storage, result.changes)
        await process_audit(bot, monitor, storage)
    except Exception:
        logger.exception("Scheduled scan failed")


async def weekly_digest(
    bot: Bot,
    storage: Storage,
    monitor: RegistryMonitor,
) -> None:
    subscribers = storage.list_subscribers()
    if not subscribers:
        return
    try:
        await monitor.ensure_fresh(force=True)
    except Exception:
        logger.exception("Digest scan failed")
        return

    storage.prune_activity(keep_days=90)
    rows = monitor.last_rows
    summary = monitor.status_summary(rows)
    week_ago = date.today() - timedelta(days=7)
    new_week = sum(
        1
        for row in rows
        if (d := row.parse_transfer_date()) and d >= week_ago
    )
    stale = len(monitor.stale_rows(3))
    text = format_digest(summary, len(rows), new_week, stale)
    for chat_id in subscribers:
        if storage.notification_mode(chat_id) == "off":
            continue
        await safe_send(bot, chat_id, text)
        await asyncio.sleep(0.05)


async def broadcast_sla_reminder(
    bot: Bot,
    monitor: RegistryMonitor,
    storage: Storage,
) -> None:
    """Notify owners and administrators about overdue pending transfers."""
    try:
        await monitor.ensure_fresh(force=True)
    except Exception:
        logger.exception("SLA scan failed")
        return
    rows = monitor.stale_rows(settings.sla_pending_days)
    if not rows:
        return
    grouped: dict[int, list] = {}
    unowned = []
    for row in rows:
        owners = resolve_developer_user_ids(storage, row)
        if owners:
            for user_id in owners:
                grouped.setdefault(user_id, []).append(row)
        else:
            unowned.append(row)
    for user_id, user_rows in grouped.items():
        if storage.notification_mode(user_id) == "off":
            continue
        await safe_send(bot, user_id, format_sla_reminder(user_rows, settings.sla_pending_days))
    if unowned:
        text = format_sla_reminder(unowned, settings.sla_pending_days)
        for admin_id in settings.admin_ids:
            await safe_send(bot, admin_id, text)


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

    scheduler.add_job(
        broadcast_sla_reminder,
        CronTrigger(hour=9, minute=10, day_of_week="mon-fri"),
        args=[bot, monitor, storage],
        id="sla_reminder",
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

    scheduler.add_job(
        weekly_digest,
        CronTrigger(day_of_week="mon", hour=9, minute=30),
        args=[bot, storage, monitor],
        id="weekly_digest",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    return scheduler
