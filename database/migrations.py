from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


async def _has_column(conn: AsyncConnection, table: str, column: str) -> bool:
    rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).mappings().all()
    return any(r["name"] == column for r in rows)


async def _has_table(conn: AsyncConnection, table: str) -> bool:
    row = (await conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
        {"t": table},
    )).first()
    return row is not None


async def apply_sqlite_migrations(conn: AsyncConnection) -> None:
    # 1) users.last_seen_at (нужно для touch_user/stats)
    if await _has_table(conn, "users"):
        if not await _has_column(conn, "users", "last_seen_at"):
            await conn.execute(text("ALTER TABLE users ADD COLUMN last_seen_at DATETIME"))

    # 2) event_photos (нужно для галереи/карусели)
    if not await _has_table(conn, "event_photos"):
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS event_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                file_id VARCHAR(255) NOT NULL,
                position INTEGER NOT NULL DEFAULT 1,
                created_at DATETIME,
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE,
                CONSTRAINT uq_event_photos_event_pos UNIQUE (event_id, position)
            )
        """))

    # 3) events.admission_price_json / free_kids_upto_age / reject_reason (если у тебя это реально используется)
    if await _has_table(conn, "events"):
        if not await _has_column(conn, "events", "admission_price_json"):
            await conn.execute(text("ALTER TABLE events ADD COLUMN admission_price_json TEXT"))
        if not await _has_column(conn, "events", "free_kids_upto_age"):
            await conn.execute(text("ALTER TABLE events ADD COLUMN free_kids_upto_age INTEGER"))
        if not await _has_column(conn, "events", "reject_reason"):
            await conn.execute(text("ALTER TABLE events ADD COLUMN reject_reason TEXT"))
