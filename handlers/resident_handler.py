import html
import json
from datetime import date, datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, desc, and_, or_

from config import CITIES, DEFAULT_CITY
from database.session import get_db
from database.models import Event, EventStatus, EventCategory

router = Router()

CITIES_PER_PAGE = 5
EVENTS_LIMIT_DEFAULT = 5
DESC_PREVIEW_LEN = 100  # как ты и просил


def h(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def _cities_sorted():
    return sorted(CITIES.items(), key=lambda x: x[1]["name"])


def cities_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    items = _cities_sorted()
    total = len(items)
    total_pages = (total + CITIES_PER_PAGE - 1) // CITIES_PER_PAGE

    page = max(0, page)
    if total_pages > 0:
        page = min(page, total_pages - 1)

    start = page * CITIES_PER_PAGE
    end = start + CITIES_PER_PAGE
    part = items[start:end]

    kb = InlineKeyboardBuilder()
    for slug, info in part:
        emoji = "✅" if info.get("status") == "active" else "⏳"
        kb.button(text=f"{emoji} {info['name']}", callback_data=f"res_city:{slug}")

    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="« Назад", callback_data=f"res_page:{page-1}")
    if page < total_pages - 1:
        nav.button(text="Вперёд »", callback_data=f"res_page:{page+1}")
    if page > 0 or page < total_pages - 1:
        kb.row(*nav.buttons)

    kb.button(text="🔍 Поиск города", callback_data="res_search:city")
    kb.button(text="🏠 Главное меню", callback_data="res_nav:main")
    kb.adjust(1)

    return kb.as_markup()


def _category_ru(cat: EventCategory | str) -> str:
    code = cat.value if hasattr(cat, "value") else str(cat)
    mapping = {
        "EXHIBITION": "Выставка",
        "MASTERCLASS": "Мастер-класс",
        "CONCERT": "Концерт",
        "PERFORMANCE": "Выступление",
        "LECTURE": "Лекция/семинар",
        "OTHER": "Другое",
    }
    return mapping.get(code, code)


def _format_event_datetime(e: Event) -> str:
    if e.event_date:
        ds = e.event_date.strftime("%d.%m.%Y")
        ts = e.event_time_start.strftime("%H:%M") if e.event_time_start else "—"
        te = e.event_time_end.strftime("%H:%M") if e.event_time_end else "—"
        return f"{ds} • {ts}-{te}"

    if e.period_start and e.period_end:
        ps = e.period_start.strftime("%d.%m.%Y")
        pe = e.period_end.strftime("%d.%m.%Y")
        ts = e.working_hours_start.strftime("%H:%M") if e.working_hours_start else "—"
        te = e.working_hours_end.strftime("%H:%M") if e.working_hours_end else "—"
        return f"{ps}-{pe} • {ts}-{te}"

    return "—"


def _fmt_rub(value) -> str:
    if value is None:
        return "—"
    try:
        v = float(value)
        if v.is_integer():
            return f"{int(v)} ₽"
        return f"{v} ₽"
    except Exception:
        return f"{value} ₽"


def _format_admission_value(e: Event) -> str:
    apj = getattr(e, "admission_price_json", None)
    if apj:
        try:
            data = json.loads(apj)
            if isinstance(data, dict) and data:
                order = ["все", "дети", "студенты", "взрослые", "пенсионеры"]
                parts = []
                for k in order:
                    if k in data:
                        parts.append(f"{k}: {_fmt_rub(data[k])}")
                for k, v in data.items():
                    if k not in order:
                        parts.append(f"{k}: {_fmt_rub(v)}")
                return ", ".join(parts)
        except Exception:
            pass

    return _fmt_rub(e.price_admission)


def _price_label(e: Event) -> str:
    if e.category == EventCategory.CONCERT:
        return "Цена билета от"
    return "Цена билета"


def _format_free_kids(e: Event) -> str | None:
    age = getattr(e, "free_kids_upto_age", None)
    if age is None:
        return None
    return f"Бесплатно: детям до {age} лет"


def _compact(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split())


def _short_description(text: str | None, limit: int = DESC_PREVIEW_LEN) -> str:
    t = _compact(text)
    if not t:
        return "—"
    if len(t) <= limit:
        return t
    return t[:limit].rstrip() + "…"


def event_more_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Подробнее", callback_data=f"res_event:{event_id}")
    kb.adjust(1)
    return kb.as_markup()


def schedule_kb(city_slug: str, mode: str) -> InlineKeyboardMarkup:
    # mode: last/today/3d/7d/30d
    kb = InlineKeyboardBuilder()
    kb.button(text="🕘 Сегодня", callback_data=f"res_sched:{city_slug}:today")
    kb.button(text="📆 3 дня", callback_data=f"res_sched:{city_slug}:3d")
    kb.button(text="📅 Неделя", callback_data=f"res_sched:{city_slug}:7d")
    kb.button(text="🗓 Месяц", callback_data=f"res_sched:{city_slug}:30d")
    kb.button(text="🆕 Последние", callback_data=f"res_sched:{city_slug}:last")
    kb.adjust(3, 2)

    kb.button(text="🌍 Сменить город", callback_data="res_nav:cities")
    kb.button(text="🏠 Главное меню", callback_data="res_nav:main")
    kb.adjust(1)

    return kb.as_markup()


def _event_overlaps_range_condition(date_from: date, date_to: date):
    # Идея пересечения:
    # 1) single-day: event_date between [from, to]
    # 2) period: period_start <= to AND period_end >= from
    return or_(
        and_(Event.event_date.is_not(None), Event.event_date >= date_from, Event.event_date <= date_to),
        and_(
            Event.period_start.is_not(None),
            Event.period_end.is_not(None),
            Event.period_start <= date_to,
            Event.period_end >= date_from,
        ),
    )


async def fetch_events(city_slug: str, mode: str):
    # mode: last/today/3d/7d/30d
    today = date.today()

    where = [Event.city_slug == city_slug, Event.status == EventStatus.ACTIVE]

    order_by = [desc(Event.created_at)]

    if mode == "last":
        pass

    elif mode == "today":
        d1 = today
        d2 = today
        where.append(_event_overlaps_range_condition(d1, d2))
        # для расписания логичнее сортировать по дате события
        order_by = [Event.event_date.asc().nullslast(), Event.period_start.asc().nullslast(), desc(Event.created_at)]

    elif mode in ("3d", "7d", "30d"):
        days = int(mode.replace("d", ""))
        d1 = today
        d2 = today + timedelta(days=days - 1)
        where.append(_event_overlaps_range_condition(d1, d2))
        order_by = [Event.event_date.asc().nullslast(), Event.period_start.asc().nullslast(), desc(Event.created_at)]

    else:
        mode = "last"

    async with get_db() as db:
        events = (
            await db.execute(
                select(Event)
                .where(*where)
                .order_by(*order_by)
                .limit(EVENTS_LIMIT_DEFAULT)
            )
        ).scalars().all()

    return events, mode


async def send_events_list(message: Message, city_slug: str, mode: str):
    city_name = CITIES.get(city_slug, {}).get("name", city_slug)

    events, mode = await fetch_events(city_slug, mode)

    title_map = {
        "last": "🆕 Последние мероприятия",
        "today": "🕘 Мероприятия на сегодня",
        "3d": "📆 Мероприятия на 3 дня",
        "7d": "📅 Мероприятия на неделю",
        "30d": "🗓 Мероприятия на месяц",
    }

    header = (
        f"🏠 <b>События города: {h(city_name)}</b>\n"
        f"{h(title_map.get(mode, title_map['last']))}\n"
        f"Показываю: {EVENTS_LIMIT_DEFAULT}"
    )

    if not events:
        await message.answer(
            header + "\n\nПока ничего не найдено по этому фильтру.",
            parse_mode="HTML",
            reply_markup=schedule_kb(city_slug, mode),
        )
        return

    await message.answer(
        header,
        parse_mode="HTML",
        reply_markup=schedule_kb(city_slug, mode),
    )

    # события — отдельными сообщениями (так красивее и не упираемся в лимиты Telegram)
    for e in events:
        price_line = f"{_price_label(e)}: {h(_format_admission_value(e))}"
        free_kids = _format_free_kids(e)

        text = (
            f"<b>{h(e.title)}</b>\n"
            f"Категория: {h(_category_ru(e.category))}\n"
            f"Когда: {h(_format_event_datetime(e))}\n"
            f"Где: {h(e.location)}\n"
            f"{price_line}\n"
        )
        if free_kids:
            text += f"{h(free_kids)}\n"

        text += f"Описание: {h(_short_description(e.description))}"

        # если описание обрезано — покажем кнопку “Подробнее”
        full_desc = _compact(e.description)
        if full_desc and len(full_desc) > DESC_PREVIEW_LEN:
            await message.answer(text, parse_mode="HTML", reply_markup=event_more_kb(e.id))
        else:
            await message.answer(text, parse_mode="HTML")


@router.message(F.text == "🏠 Житель")
async def resident_entry(message: Message):
    default_city_name = CITIES.get(DEFAULT_CITY, {}).get("name", "Город не задан")
    await message.answer(
        f"🏠 <b>Житель</b>\n\n"
        f"🌍 По умолчанию: <b>{h(default_city_name)}</b>\n\n"
        "👇 Выбери город:",
        reply_markup=cities_keyboard(page=0),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("res_page:"))
async def resident_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=cities_keyboard(page=page))
    await callback.answer()


@router.callback_query(F.data.startswith("res_city:"))
async def resident_city_select(callback: CallbackQuery):
    slug = callback.data.split(":")[1]
    info = CITIES.get(slug)
    if not info:
        await callback.answer("Город не найден", show_alert=True)
        return

    city_name = info["name"]
    status = info.get("status", "coming_soon")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if status != "active":
        await callback.message.answer(
            f"⏳ <b>{h(city_name)}</b> — раздел в разработке.\n\nВыбери другой город:",
            reply_markup=cities_keyboard(page=0),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await callback.message.answer(f"✅ <b>{h(city_name)} выбран!</b>", parse_mode="HTML")

    # при выборе города по умолчанию показываем последние 5
    await send_events_list(callback.message, slug, mode="last")
    await callback.answer()


@router.callback_query(F.data.startswith("res_sched:"))
async def resident_schedule(callback: CallbackQuery):
    # res_sched:{city_slug}:{mode}
    _, city_slug, mode = callback.data.split(":")
    await send_events_list(callback.message, city_slug, mode=mode)
    await callback.answer()


@router.callback_query(F.data.startswith("res_event:"))
async def resident_event_details(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()

    if not e or e.status != EventStatus.ACTIVE:
        await callback.answer("Событие не найдено", show_alert=True)
        return

    price_line = f"{_price_label(e)}: {h(_format_admission_value(e))}"
    free_kids = _format_free_kids(e)

    text = (
        f"📄 <b>{h(e.title)}</b>\n\n"
        f"Категория: {h(_category_ru(e.category))}\n"
        f"Когда: {h(_format_event_datetime(e))}\n"
        f"Где: {h(e.location)}\n"
        f"{price_line}\n"
    )
    if free_kids:
        text += f"{h(free_kids)}\n"

    text += "\n"
    text += f"<b>Описание:</b>\n{h(_compact(e.description) or '—')}"

    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data == "res_nav:cities")
async def res_nav_cities(callback: CallbackQuery):
    await callback.message.answer(
        "👇 Выбери город:",
        reply_markup=cities_keyboard(page=0),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "res_nav:main")
async def res_nav_main(callback: CallbackQuery):
    await callback.message.answer("🏠 Главное меню")
    await callback.answer()
