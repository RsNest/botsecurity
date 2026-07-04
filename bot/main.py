from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from bot.handlers import setup_handlers, setup_scheduler
from bot.monitor import RegistryMonitor
from bot.storage import Storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def main() -> None:
    bot = Bot(
        token=settings.telegram_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    storage = Storage()
    monitor = RegistryMonitor(storage=storage)

    setup_handlers(dp, bot, monitor, storage)
    scheduler = setup_scheduler(bot, monitor, storage)
    scheduler.start()

    logger.info("Starting bot (admins: %s)", settings.admin_ids)
    try:
        await monitor.scan()
        logger.info("Initial scan OK: %s rows", len(monitor.last_rows))
    except Exception as exc:
        logger.warning("Initial scan failed (will retry on schedule): %s", exc)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
