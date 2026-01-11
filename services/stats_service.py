from datetime import datetime, timedelta

from sqlalchemy import select, func, desc

from database.session import get_db
from database.models import User


def _user_to_dict(u: User) -> dict:
    return {
        "telegram_id": u.telegram_id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "created_at": getattr(u, "created_at", None),
        "last_seen_at": u.last_seen_at,
    }


async def get_global_user_stats(limit_users: int = 20) -> dict:
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    dt_7d = now - timedelta(days=7)
    dt_30d = now - timedelta(days=30)

    async with get_db() as db:
        total_users = (
            await db.execute(select(func.count()).select_from(User))
        ).scalar_one()

        new_today = (
            await db.execute(
                select(func.count()).select_from(User).where(User.created_at >= today_start)
            )
        ).scalar_one()

        active_7d = (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.last_seen_at.is_not(None), User.last_seen_at >= dt_7d)
            )
        ).scalar_one()

        active_30d = (
            await db.execute(
                select(func.count())
                .select_from(User)
                .where(User.last_seen_at.is_not(None), User.last_seen_at >= dt_30d)
            )
        ).scalar_one()

        recent_users = (
            await db.execute(
                select(User)
                .where(User.last_seen_at.is_not(None))
                .order_by(desc(User.last_seen_at))
                .limit(limit_users)
            )
        ).scalars().all()

        new_users_today = (
            await db.execute(
                select(User)
                .where(User.created_at >= today_start)
                .order_by(desc(User.created_at))
                .limit(limit_users)
            )
        ).scalars().all()

    return {
        "total_users": int(total_users or 0),
        "new_today": int(new_today or 0),
        "active_7d": int(active_7d or 0),
        "active_30d": int(active_30d or 0),
        "recent_users": [_user_to_dict(u) for u in recent_users],
        "new_users_today": [_user_to_dict(u) for u in new_users_today],
        "meta": {
            "now_utc": now,
            "today_start_utc": today_start,
            "limit_users": limit_users,
        },
    }
