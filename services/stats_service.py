from datetime import datetime, timedelta
from sqlalchemy import select, func

from database.session import get_db
from database.models import User

async def get_global_user_stats() -> dict:
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    dt_7d = now - timedelta(days=7)
    dt_30d = now - timedelta(days=30)

    async with get_db() as db:
        total_users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        new_today = (await db.execute(select(func.count()).select_from(User).where(User.created_at >= today_start))).scalar_one()
        active_7d = (
            await db.execute(select(func.count()).select_from(User).where(User.last_seen_at.is_not(None), User.last_seen_at >= dt_7d))
        ).scalar_one()
        active_30d = (
            await db.execute(select(func.count()).select_from(User).where(User.last_seen_at.is_not(None), User.last_seen_at >= dt_30d))
        ).scalar_one()

    return {
        "total_users": int(total_users or 0),
        "new_today": int(new_today or 0),
        "active_7d": int(active_7d or 0),
        "active_30d": int(active_30d or 0),
    }
