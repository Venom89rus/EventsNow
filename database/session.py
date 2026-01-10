from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from config import DATABASE_URL

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

@asynccontextmanager
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def init_db():
    from database.models import Base
    from database.migrations import apply_sqlite_migrations

    async with engine.begin() as conn:
        # 1) Создаём отсутствующие таблицы по моделям
        await conn.run_sync(Base.metadata.create_all)

        # 2) Докидываем недостающие колонки/таблицы в уже существующую БД
        await apply_sqlite_migrations(conn)

    print("✅ База данных инициализирована!")

