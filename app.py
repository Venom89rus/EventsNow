import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import BOT_TOKEN  # noqa: E402
from database.session import init_db  # noqa: E402

# routers
from handlers.start_handler import router as start_router  # noqa: E402
from handlers.resident_handler import router as resident_router  # noqa: E402
from handlers.organizer_handler import router as organizer_router  # noqa: E402
from handlers.feedback_handler import router as feedback_router  # noqa: E402

from handlers.admin_handler import router as admin_router

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger("eventsnow")


async def main():
    await init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    # порядок важен: start -> resident -> organizer -> feedback
    dp.include_router(start_router)
    dp.include_router(resident_router)
    dp.include_router(organizer_router)
    dp.include_router(admin_router)
    dp.include_router(feedback_router)

    logger.info("🤖 EventsNow started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
