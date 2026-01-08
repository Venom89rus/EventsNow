import html
from datetime import date, timedelta

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, desc, and_, or_

from config import CITIES, DEFAULT_CITY
from database.session import get_db
from database.models import Event, EventStatus, EventCategory

router = Router()

CITIES_PER_PAGE = 5
EVENTS_LIMIT_DEFAULT = 5
DESC_PREVIEW_LEN = 100


# ---------- FSM ----------
class ResidentState(StatesGroup):
    choosing_city = State()
    browsing = State()


# ---------- basics ----------
def h(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def compact(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split())


def short(text: str | None, limit: int = DESC_PREVIEW_LEN) -> str:
    t = compact(text)
    if not t:
        return "—"
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


# ---------- reply keyboard (нижнее меню жителя) ----------
def resident_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🕘 Сегодня"), KeyboardButton(text="📆 3 дня"), KeyboardButton(text="📅 Неделя")],
            [KeyboardButton(text="🗓 Месяц"), KeyboardButton(text="🆕 Последние")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


# ---------- cities inline keyboard ----------
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

    kb.adjust(1)
    return kb.as_markup()


# ---------- formatting ----------
def category_ru(cat: EventCategory | str) -> str:
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


def category_emoji(cat: EventCategory | str) -> str:
    code = cat.value if hasattr(cat, "value") else str(cat)
    mapping = {
        "EXHIBITION": "🖼",
        "MASTERCLASS": "🧑‍🏫",
        "CONCERT": "🎤",
        "PERFORMANCE": "🎭",
        "LECTURE": "🎓",
        "OTHER": "✨",
    }
    return mapping.get(code, "✨")


def fmt_when(e: Event) -> str:
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


def fmt_price(e: Event) -> str:
    if e.price_admission is None:
        return "—"
    try:
        v = float(e.price_admission)
        s = str(int(v)) if v.is_integer() else str(v)
    except Exception:
        s = str(e.price_admission)

    # Концерт — "от"
    if e.category == EventCategory.CONCERT:
        return f"от {s} ₽"
    return f"{s} ₽"


# ---------- filtering ----------
def _event_overlaps_range_condition(date_from: date, date_to: date):
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
    today = date.today()

    where = [Event.city_slug == city_slug, Event.status == EventStatus.ACTIVE]
    order_by = [desc(Event.created_at)]

    if mode == "today":
        where.append(_event_overlaps_range_condition(today, today))
        order_by = [Event.event_date.asc().nullslast(), Event.period_start.asc().nullslast(), desc(Event.created_at)]
    elif mode in ("3d", "7d", "30d"):
        days = int(mode.replace("d", ""))
        d2 = today + timedelta(days=days - 1)
        where.append(_event_overlaps_range_condition(today, d2))
        order_by = [Event.event_date.asc().nullslast(), Event.period_start.asc().nullslast(), desc(Event.created_at)]
    else:
        mode = "last"

    async with get_db() as db:
        events = (
            await db.execute(select(Event).where(*where).order_by(*order_by).limit(EVENTS_LIMIT_DEFAULT))
        ).scalars().all()

    return events, mode


# ---------- inline details (in-place edit) ----------
def event_preview_kb(event_id: int, can_expand: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if can_expand:
        kb.button(text="📄 Подробнее", callback_data=f"res_event_open:{event_id}")
    kb.adjust(1)
    return kb.as_markup()


def event_details_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=f"res_event_close:{event_id}")
    kb.adjust(1)
    return kb.as_markup()


def event_preview_text(e: Event) -> str:
    cat = f"{category_emoji(e.category)} {category_ru(e.category)}"
    return (
        f"🎫 <b>{h(e.title)}</b>\n"
        f"🏷 <b>{h(cat)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>Когда:</b> {h(fmt_when(e))}\n"
        f"📍 <b>Где:</b> {h(e.location)}\n"
        f"💳 <b>Цена:</b> {h(fmt_price(e))}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>Описание:</b> {h(short(e.description))}"
    )


def event_details_text(e: Event) -> str:
    cat = f"{category_emoji(e.category)} {category_ru(e.category)}"
    return (
        f"📄 <b>{h(e.title)}</b>\n"
        f"🏷 <b>{h(cat)}</b>\n"
        f"🏙 <b>{h(e.city_slug)}</b>\n\n"
        f"📅 <b>Когда:</b> {h(fmt_when(e))}\n"
        f"📍 <b>Где:</b> {h(e.location)}\n"
        f"💳 <b>Цена:</b> {h(fmt_price(e))}\n\n"
        f"📝 <b>Описание:</b>\n{h(compact(e.description) or '—')}"
    )


async def send_events_list(message: Message, city_slug: str, mode: str):
    city_name = CITIES.get(city_slug, {}).get("name", city_slug)
    title_map = {
        "last": "🆕 Последние мероприятия",
        "today": "🕘 Мероприятия на сегодня",
        "3d": "📆 Мероприятия на 3 дня",
        "7d": "📅 Мероприятия на неделю",
        "30d": "🗓 Мероприятия на месяц",
    }

    events, mode = await fetch_events(city_slug, mode)

    await message.answer(
        f"🏠 <b>События города: {h(city_name)}</b>\n"
        f"{h(title_map.get(mode, title_map['last']))}\n"
        f"Показываю: {EVENTS_LIMIT_DEFAULT}",
        parse_mode="HTML",
    )

    if not events:
        await message.answer("Пока ничего не найдено по этому фильтру.", parse_mode="HTML")
        return

    for e in events:
        full_desc = compact(e.description)
        can_expand = bool(full_desc) and len(full_desc) > DESC_PREVIEW_LEN
        await message.answer(
            event_preview_text(e),
            parse_mode="HTML",
            reply_markup=event_preview_kb(e.id, can_expand),
        )


# ---------- entry ----------
@router.message(F.text == "🏠 Житель")
async def resident_entry(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ResidentState.choosing_city)

    default_city_name = CITIES.get(DEFAULT_CITY, {}).get("name", "Город не задан")
    await message.answer(
        f"🏠 <b>Житель</b>\n\n"
        f"🌍 По умолчанию: <b>{h(default_city_name)}</b>\n\n"
        "👇 Выбери город:",
        reply_markup=resident_menu_kb(),
        parse_mode="HTML",
    )
    await message.answer("Список городов:", reply_markup=cities_keyboard(page=0), parse_mode="HTML")


@router.callback_query(F.data.startswith("res_page:"))
async def resident_page(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=cities_keyboard(page=page))
    await callback.answer()


@router.callback_query(F.data.startswith("res_city:"))
async def resident_city_select(callback: CallbackQuery, state: FSMContext):
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
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await state.set_state(ResidentState.browsing)
    await state.update_data(city_slug=slug, mode="last")

    await callback.message.answer(f"✅ <b>{h(city_name)} выбран!</b>", parse_mode="HTML")
    await send_events_list(callback.message, slug, mode="last")
    await callback.answer()


# ---------- resident menu filters ----------
@router.message(F.text.in_({"🕘 Сегодня", "📆 3 дня", "📅 Неделя", "🗓 Месяц", "🆕 Последние"}))
async def resident_filters(message: Message, state: FSMContext):
    data = await state.get_data()
    city_slug = data.get("city_slug")
    if not city_slug:
        await message.answer("Сначала выбери город.", parse_mode="HTML")
        return

    text_to_mode = {
        "🕘 Сегодня": "today",
        "📆 3 дня": "3d",
        "📅 Неделя": "7d",
        "🗓 Месяц": "30d",
        "🆕 Последние": "last",
    }
    mode = text_to_mode.get(message.text, "last")
    await state.update_data(mode=mode)

    await send_events_list(message, city_slug, mode=mode)


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Житель"), KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="🛡 Админ"), KeyboardButton(text="✍️ Обратная связь")],
        ],
        resize_keyboard=True,
    )

@router.message(F.text == "⬅️ Назад")
async def resident_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


# ---------- inline: open/close details in place ----------
@router.callback_query(F.data.startswith("res_event_open:"))
async def resident_event_open(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()

    if not e or e.status != EventStatus.ACTIVE:
        await callback.answer("Событие не найдено", show_alert=True)
        return

    await callback.message.edit_text(
        event_details_text(e),
        parse_mode="HTML",
        reply_markup=event_details_kb(event_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("res_event_close:"))
async def resident_event_close(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()

    if not e or e.status != EventStatus.ACTIVE:
        await callback.answer("Событие не найдено", show_alert=True)
        return

    full_desc = compact(e.description)
    can_expand = bool(full_desc) and len(full_desc) > DESC_PREVIEW_LEN

    await callback.message.edit_text(
        event_preview_text(e),
        parse_mode="HTML",
        reply_markup=event_preview_kb(event_id, can_expand),
    )
    await callback.answer()
