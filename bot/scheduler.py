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
from bot.formatters import format_change, format_digest, format_reminder
from bot.models import RowChange
from bot.monitor import RegistryMonitor
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
        text = format_change(change)
        for chat_id in subscribers:
            await safe_send(bot, chat_id, text)
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


async def scheduled_scan(
    bot: Bot,
    monitor: RegistryMonitor,
    storage: Storage,
) -> None:
    try:
        result = await monitor.ensure_fresh(force=True)
        await broadcast_changes(bot, storage, result.changes)
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
        await safe_send(bot, chat_id, text)
        await asyncio.sleep(0.05)


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
