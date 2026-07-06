from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from bot.config import settings
from bot.handlers import setup_handlers
from bot.middleware import ActivityMiddleware
from bot.monitor import RegistryMonitor
from bot.scheduler import setup_scheduler
from bot.storage import Storage
from bot.submit import setup_submit_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

PUBLIC_COMMANDS = [
    BotCommand(command="add", description="Добавить тег в реестр"),
    BotCommand(command="my", description="Мои образы"),
    BotCommand(command="pending", description="Ожидают передачи на проверку"),
    BotCommand(command="on_review", description="На проверке у ИБ"),
    BotCommand(command="passed", description="Прошли проверку"),
    BotCommand(command="failed", description="Не прошли проверку"),
    BotCommand(command="devs", description="По разработчикам"),
    BotCommand(command="releases", description="По релизам"),
    BotCommand(command="dates", description="Выборка по датам"),
    BotCommand(command="find", description="Поиск (или просто напишите текст)"),
    BotCommand(command="status", description="Сводка по статусам"),
    BotCommand(command="today", description="Добавленные сегодня"),
    BotCommand(command="last", description="Последние добавленные"),
    BotCommand(command="by_dev", description="Образы разработчика"),
    BotCommand(command="stale", description="Висят без статуса N дней"),
    BotCommand(command="subscribe", description="Подписаться"),
    BotCommand(command="unsubscribe", description="Отписаться"),
    BotCommand(command="help", description="Справка"),
]

ADMIN_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand(command="sync", description="[админ] Синхронизация"),
    BotCommand(command="stats", description="[админ] Статистика бота"),
    BotCommand(command="users", description="[админ] Активность пользователей"),
    BotCommand(command="broadcast", description="[админ] Рассылка"),
]


async def _set_commands(bot: Bot) -> None:
    await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(
                ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except Exception:
            logger.warning("Could not set admin commands for %s", admin_id)


async def main() -> None:
    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    storage = Storage()
    monitor = RegistryMonitor(storage=storage)

    activity = ActivityMiddleware(storage)
    dp.message.middleware(activity)
    dp.callback_query.middleware(activity)

    setup_submit_handlers(dp, bot, monitor, storage)
    setup_handlers(dp, bot, monitor, storage)
    scheduler = setup_scheduler(bot, monitor, storage)
    scheduler.start()

    await _set_commands(bot)

    logger.info("Starting bot (admins: %s)", settings.admin_ids)
    try:
        await monitor.scan()
        logger.info("Initial scan OK: %s rows", len(monitor.last_rows))
    except Exception as exc:
        logger.warning("Initial scan failed (will retry on schedule): %s", exc)

    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        logger.info("Shutting down…")
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped")
