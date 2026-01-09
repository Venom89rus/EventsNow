from datetime import date

from sqlalchemy import update, and_, or_

from database.session import get_db
from database.models import Event, EventStatus

async def archive_expired_events(today: date | None = None) -> int:
    """
    Переводит прошедшие ACTIVE события в ARCHIVED.
    Правило:
      - daily: event_date < today
      - period: period_end < today
    Возвращает кол-во затронутых строк (если поддерживается диалектом).
    """
    today = today or date.today()

    cond_daily_expired = and_(
        Event.event_date.is_not(None),
        Event.event_date < today,
    )

    cond_period_expired = and_(
        Event.period_end.is_not(None),
        Event.period_end < today,
    )

    stmt = (
        update(Event)
        .where(
            Event.status == EventStatus.ACTIVE,
            or_(cond_daily_expired, cond_period_expired),
        )
        .values(status=EventStatus.ARCHIVED)
    )

    async with get_db() as db:
        res = await db.execute(stmt)
        # rowcount может быть -1 на некоторых драйверах — это ок
        return int(res.rowcount or 0)
