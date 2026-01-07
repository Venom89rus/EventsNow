import asyncio
from .session import init_db

async def create_database():
    """Создаёт все таблицы в БД"""
    await init_db()
    print("🎉 База данных готова к работе!")

if __name__ == "__main__":
    asyncio.run(create_database())
