import asyncio
import html
import json
import logging
from typing import Optional, Any, Dict

from sqlalchemy import select
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.session import get_db
from database.models import User, Event, EventStatus, EventPhoto

logger = logging.getLogger(__name__)

DESC_PREVIEW_LEN = 160

# Словарь категорий EN→RU
CATEGORY_RU = {
    "EXHIBITION": "🖼️ Выставка",
    "CONCERT": "🎤 Концерт",
    "THEATER": "🎭 Театр",
    "SPORT": "⚽ Спорт",
    "PERFORMANCE": "🎭 Выступление",
    "FESTIVAL": "🎉 Фестиваль",
    "WORKSHOP": "📚 Мастер-класс",
    "LECTURE": "🎓 Лекция",
    "EXCURSION": "🚌 Экскурсия",
    "MEETING": "🤝 Встреча",
    "OTHER": "📋 Другое",
}


def _h(x: Any) -> str:
    return html.escape(str(x)) if x is not None else "—"


def _compact(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split())


def _short(text: str | None, limit: int = DESC_PREVIEW_LEN) -> str:
    t = _compact(text)
    if not t:
        return "—"
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def _category_code(cat: Any) -> str:
    """
    Нормализует category:
    - Enum -> 'CONCERT' (через .value или .name)
    - строка 'EventCategory.CONCERT' -> 'CONCERT'
    - строка 'CONCERT' -> 'CONCERT'
    """
    if cat is None:
        return "OTHER"

    v = getattr(cat, "value", None)
    if v is not None:
        return str(v)

    n = getattr(cat, "name", None)
    if n is not None:
        return str(n)

    s = str(cat)
    if "." in s:
        s = s.split(".")[-1]
    return s


def _event_period_text(event: Event) -> str:
    """Форматирует дату"""
    if getattr(event, "period_start", None) and getattr(event, "period_end", None):
        return f"{event.period_start}–{event.period_end}"
    if getattr(event, "event_date", None):
        return str(event.event_date)
    if getattr(event, "period_start", None):
        return f"с {event.period_start}"
    return "Постоянно"


def _event_time_text(event: Event) -> str:
    """Форматирует время"""

    def format_time(t) -> str:
        if t is None:
            return ""
        t_str = str(t)
        return t_str.split(".")[0] if "." in t_str else t_str

    if getattr(event, "event_time_start", None) and getattr(event, "event_time_end", None):
        return f"{format_time(event.event_time_start)}–{format_time(event.event_time_end)}"
    if getattr(event, "working_hours_start", None) and getattr(event, "working_hours_end", None):
        return f"{format_time(event.working_hours_start)}–{format_time(event.working_hours_end)}"
    if getattr(event, "event_time_start", None):
        return format_time(event.event_time_start)
    return "Весь день"


def _event_price_text(event: Event) -> str:
    """
    Возвращает ТОЛЬКО значение цены (без 'Цена:' и без '💰'),
    чтобы префикс добавлялся единообразно в карточке.
    """
    raw = getattr(event, "admission_price_json", None)

    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data:
                items: list[tuple[str, float]] = []

                for k, v in data.items():
                    key = str(k).strip() if k is not None else ""
                    if not key:
                        continue
                    try:
                        val = float(v)
                    except Exception:
                        continue
                    if val < 0:
                        continue
                    items.append((key, val))

                def _fmt_num(x: float) -> str:
                    return str(int(x)) if float(x).is_integer() else str(x)

                if items:
                    preferred = ["все", "дети", "студенты", "взрослые", "пенсионеры"]
                    order = {name: i for i, name in enumerate(preferred)}
                    items.sort(key=lambda kv: (order.get(kv[0].lower(), 999), kv[0].lower()))

                    # если только "все" — выводим просто число
                    if len(items) == 1 and items[0][0].lower() == "все":
                        return f"{_fmt_num(items[0][1])} ₽"

                    # иначе список тарифов
                    parts = [f"{k} — {_fmt_num(v)} ₽" for k, v in items]
                    return "; ".join(parts)

        except Exception:
            return "уточняйте"

    # fallback на price_admission
    v = getattr(event, "price_admission", None)
    if v is None:
        return "бесплатно"

    try:
        v = float(v)
        s = str(int(v)) if v.is_integer() else str(v)
        return f"{s} ₽"
    except Exception:
        return str(v)


def _event_push_text(event: Event) -> str:
    """Форматирует текст уведомления"""
    code = _category_code(getattr(event, "category", None))
    cat_ru = CATEGORY_RU.get(code, CATEGORY_RU.get("OTHER", "📋 Другое"))

    return (
        "🆕 Новое событие в твоём городе!\n\n"
        f"🎫 <b>{_h(event.title)}</b>\n"
        f"🏷️ {cat_ru}\n"
        f"📍 <b>{_h(event.location)}</b>\n"
        f"🗓️ {_event_period_text(event)}\n"
        f"⏰ {_event_time_text(event)}\n"
        f"💰 Цена: {_h(_event_price_text(event))}\n\n"
        f"📝 {_h(_short(event.description))}"
    )


async def _fetch_event(event_id: int) -> Optional[Event]:
    async with get_db() as db:
        return (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()


async def _fetch_event_first_photo_file_id(event_id: int) -> Optional[str]:
    async with get_db() as db:
        p = (
            await db.execute(
                select(EventPhoto)
                .where(EventPhoto.event_id == event_id)
                .order_by(EventPhoto.position.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return p.file_id if p else None


async def _fetch_recipients(city_slug: str) -> list[int]:
    """Получатели: жители города с last_seen_at != NULL"""
    async with get_db() as db:
        ids = (
            await db.execute(
                select(User.telegram_id)
                .where(User.city_slug == city_slug)
                .where(User.last_seen_at.is_not(None))
            )
        ).scalars().all()
        return list(ids)


async def notify_new_event_published(
    bot,
    event_id: int,
    *,
    throttle_sec: float = 0.05,
    skip_organizer: bool = True,
) -> Dict[str, int]:
    """
    Рассылка по факту публикации события.
    Возвращает: {"sent": int, "failed": int, "skipped": int, "recipients": int}
    """
    logger.warning("NOTIFY: TRY event_id=%s", event_id)

    event = await _fetch_event(event_id)
    if not event or event.status != EventStatus.ACTIVE:
        logger.warning("NOTIFY skip: event not active or missing: id=%s", event_id)
        return {"sent": 0, "failed": 0, "skipped": 1, "recipients": 0}

    recipients = await _fetch_recipients(event.city_slug)
    logger.warning("NOTIFY recipients=%s for city=%s", len(recipients), event.city_slug)

    if not recipients:
        logger.warning("NOTIFY no recipients for event_id=%s city=%s", event_id, event.city_slug)
        return {"sent": 0, "failed": 0, "skipped": 0, "recipients": 0}

    file_id = await _fetch_event_first_photo_file_id(event_id)
    text = _event_push_text(event)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👉 Посмотреть",
                    url=f"https://t.me/Events_Now_bot?start=app_event_{event_id}",
                )
            ]
        ]
    )

    sent = 0
    failed = 0
    skipped = 0

    for uid in recipients:
        if skip_organizer and uid == event.user_id:
            logger.warning("NOTIFY skip organizer uid=%s", uid)
            skipped += 1
            continue

        try:
            if file_id:
                await bot.send_photo(
                    chat_id=uid,
                    photo=file_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            else:
                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )

            sent += 1
            logger.warning("NOTIFY ok uid=%s", uid)

        except Exception as e:
            failed += 1
            logger.error("NOTIFY fail uid=%s: %s", uid, e)

        if throttle_sec:
            await asyncio.sleep(throttle_sec)

    result = {"sent": sent, "failed": failed, "skipped": skipped, "recipients": len(recipients)}
    logger.warning("NOTIFY done event_id=%s %s", event_id, result)
    return result
