import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent

logger = logging.getLogger("eventsnow")

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import BOT_TOKEN  # noqa: E402
from database.session import init_db  # noqa: E402

from handlers.start_handler import router as start_router  # noqa: E402
from handlers.admin_handler import router as admin_router  # noqa: E402
from handlers.admin_tools_handler import router as admin_tools_router  # noqa: E402
from handlers.resident_handler import router as resident_router  # noqa: E402
from handlers.organizer_handler import router as organizer_router  # noqa: E402
from handlers.feedback_handler import router as feedback_router  # noqa: E402

from services.event_archive import archive_expired_events  # noqa: E402

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler(),
    ],
)


async def main():
    """Инициализация и запуск бота"""
    await init_db()

    # Архивируем истекшие события
    try:
        n = await archive_expired_events()
        if n > 0:
            logger.info("🗂 Archived expired events: %s", n)
    except Exception as e:
        logger.exception("Archive job failed: %s", e)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    @dp.errors()
    async def on_error(event: ErrorEvent):
        logger.exception("UNHANDLED_ERROR: %r", event.exception)
        return True

    # Порядок важен: админ до resident/organizer
    dp.include_router(start_router)
    dp.include_router(admin_router)
    dp.include_router(admin_tools_router)
    dp.include_router(resident_router)
    dp.include_router(organizer_router)
    dp.include_router(feedback_router)

    logger.info("🤖 EventsNow started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
