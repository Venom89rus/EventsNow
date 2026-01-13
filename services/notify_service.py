import asyncio
import html
import json  # ✅ ДОБАВЛЕН
from typing import Optional, Any, Dict

from sqlalchemy import select
from database.session import get_db
from database.models import User, Event, EventStatus, EventPhoto
import logging
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

DESC_PREVIEW_LEN = 160

# Словарь категорий EN→RU
CATEGORY_RU = {
    'EXHIBITION': '🖼️ Выставка',
    'CONCERT': '🎤 Концерт',
    'THEATER': '🎭 Театр',
    'SPORT': '⚽ Спорт',
    'PERFORMANCE': '🎭 Выступление',
    'FESTIVAL': '🎉 Фестиваль',
    'WORKSHOP': '📚 Мастер-класс',
    'LECTURE': '🎓 Лекция',
    'EXCURSION': '🚌 Экскурсия',
    'MEETING': '🤝 Встреча',
    'OTHER': '📋 Другое'
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

def _event_period_text(event: Event) -> str:
    """Форматирует дату"""
    if event.period_start and event.period_end:
        return f"{event.period_start}–{event.period_end}"
    elif event.event_date:
        return str(event.event_date)
    elif event.period_start:
        return f"с {event.period_start}"
    else:
        return "Постоянно"

def _event_time_text(event: Event) -> str:
    """Форматирует время"""
    # ✅ ФИКС 1: безопасный format_time для time объектов
    def format_time(t):
        if t is None:
            return ""
        t_str = str(t)
        return t_str.split('.')[0] if '.' in t_str else t_str  # str(t) ПЕРЕД split!

    if event.event_time_start and event.event_time_end:
        return f"{format_time(event.event_time_start)}–{format_time(event.event_time_end)}"
    elif event.working_hours_start and event.working_hours_end:
        return f"{format_time(event.working_hours_start)}–{format_time(event.working_hours_end)}"
    elif event.event_time_start:
        return format_time(event.event_time_start)
    else:
        return "Весь день"

def _event_price_text(event: Event) -> str:
    """Возвращает цену одной строкой: '2000 ₽' или 'Бесплатно'"""
    raw = getattr(event, "admission_price_json", None)

    if raw:
        try:
            prices = json.loads(raw)

            # admission_price_json иногда может быть строкой -> тогда это не dict
            if isinstance(prices, dict):
                # приоритет: все -> взрослые -> вход -> дети
                for key in ("все", "взрослые", "вход", "дети"):
                    v = prices.get(key)
                    if v is None:
                        continue
                    try:
                        v = float(v)
                    except Exception:
                        return "Уточняйте"

                    # "только цифра"
                    s = str(int(v)) if v.is_integer() else str(v)
                    return f"{s} ₽"

        except Exception:
            return "Уточняйте"

    # fallback на обычную цену, если она есть
    v = getattr(event, "price_admission", None)
    if v is None:
        return "Бесплатно"

    try:
        v = float(v)
        s = str(int(v)) if v.is_integer() else str(v)
        return f"{s} ₽"
    except Exception:
        return "Уточняйте"

def _event_push_text(event: Event) -> str:
    """Форматирует текст уведомления"""
    cat_ru = CATEGORY_RU.get(event.category, event.category)

    return (
        f"🆕 Новое событие в твоём городе!\n\n"
        f"🎫 <b>{_h(event.title)}</b>\n"
        f"🏷️ {cat_ru}\n"
        f"📍 <b>{_h(event.location)}</b>\n"
        f"🗓️ {_event_period_text(event)}\n"
        f"⏰ {_event_time_text(event)}\n"
        f"{_event_price_text(event)}\n\n"
        f"📝 {_h(_short(event.description))}\n\n"
        f"<a href='t.me/Events_Now_bot/app?start=app_event_{event.id}'>👉 Посмотреть</a>"  # ✅ кнопка
    )

async def _fetch_event(event_id: int) -> Optional[Event]:
    async with get_db() as db:
        return (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()

async def _fetch_event_first_photo_file_id(event_id: int) -> Optional[str]:
    async with get_db() as db:
        p = (await db.execute(
            select(EventPhoto)
            .where(EventPhoto.event_id == event_id)
            .order_by(EventPhoto.position.asc())
            .limit(1)
        )).scalar_one_or_none()
        return p.file_id if p else None

async def _fetch_recipients(city_slug: str) -> list[int]:
    """Получатели: жители города с last_seen_at != NULL"""
    async with get_db() as db:
        ids = (await db.execute(
            select(User.telegram_id)
            .where(User.city_slug == city_slug)
            .where(User.last_seen_at.is_not(None))
        )).scalars().all()
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

    logger.warning("NOTIFY start event_id=%s city=%s recipients=%s",
                   event_id, event.city_slug, recipients)

    sent = 0
    failed = 0
    skipped = 0

    for uid in recipients:
        if skip_organizer and uid == event.user_id:
            logger.warning("NOTIFY skip organizer uid=%s", uid)
            skipped += 1
            continue

        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="👉 Посмотреть",
                url=f"https://t.me/Events_Now_bot?start=app_event_{event_id}"
            )
        ]])

        if file_id:
            await bot.send_photo(chat_id=uid, photo=file_id, caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            await bot.send_message(chat_id=uid, text=text, parse_mode="HTML", reply_markup=kb)

        try:
            if file_id:
                await bot.send_photo(chat_id=uid, photo=file_id, caption=text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
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
