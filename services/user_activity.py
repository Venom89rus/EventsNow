from datetime import datetime
from sqlalchemy import select

from database.session import get_db
from database.models import User

async def touch_user(telegram_id: int, username: str | None, first_name: str | None, last_name: str | None) -> None:
    now = datetime.utcnow()
    async with get_db() as db:
        u = (await db.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
        if not u:
            db.add(
                User(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    last_seen_at=now,
                )
            )
            return

        u.username = username
        u.first_name = first_name
        u.last_name = last_name
        u.last_seen_at = now
