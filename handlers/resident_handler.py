import html
import json
from datetime import date, timedelta

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
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


# ---------- FSM for Resident mode ----------
class ResidentState(StatesGroup):
    choosing_city = State()
    browsing = State()  # city выбран, можно фильтровать


def h(x) -> str:
    return html.escape(str(x)) if x is not None else ""


# ---------- Reply keyboards ----------
def main_menu_kb() -> ReplyKeyboardMarkup:
    # Если у тебя главная клавиатура строится в другом месте — скажи, интегрируем туда.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Житель"), KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="🛡 Админ"), KeyboardButton(text="✍️ Обратная связь")],
        ],
        resize_keyboard=True,
    )


def resident_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🕘 Сегодня"), KeyboardButton(text="📆 3 дня"), KeyboardButton(text="📅 Неделя")],
            [KeyboardButton(text="🗓 Месяц"), KeyboardButton(text="🆕 Последние")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


# ---------- Inline keyboard for city picking ----------
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


# ---------- Formatting helpers ----------
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
    return "Цена билета от" if e.category == EventCategory.CONCERT else "Цена билета"


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


# ---------- Inline "Подробнее" (оставляем как есть) ----------
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


def _event_preview_text(e: Event) -> str:
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
    return text


def _event_details_text(e: Event) -> str:
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
    return text


async def _fetch_events(city_slug: str, mode: str):
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

    title_map = {
        "last": "🆕 Последние мероприятия",
        "today": "🕘 Мероприятия на сегодня",
        "3d": "📆 Мероприятия на 3 дня",
        "7d": "📅 Мероприятия на неделю",
        "30d": "🗓 Мероприятия на месяц",
    }

    events, mode = await _fetch_events(city_slug, mode)

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
        full_desc = _compact(e.description)
        can_expand = bool(full_desc) and len(full_desc) > DESC_PREVIEW_LEN
        await message.answer(
            _event_preview_text(e),
            parse_mode="HTML",
            reply_markup=event_preview_kb(e.id, can_expand),
        )


# ---------- Entry / City choosing ----------
@router.message(F.text == "🏠 Житель")
async def resident_entry(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ResidentState.choosing_city)

    # меняем нижнее меню на “фильтры + назад”
    await message.answer(
        "🏠 <b>Житель</b>\n\n👇 Выбери город:",
        reply_markup=resident_menu_kb(),
        parse_mode="HTML",
    )
    await message.answer("Список городов:", reply_markup=cities_keyboard(page=0))


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


# ---------- Resident reply-menu фильтры ----------
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


@router.message(F.text == "⬅️ Назад")
async def resident_back(message: Message, state: FSMContext):
    # Возвращаем главное меню (нижняя клавиатура) и выходим из “режима жителя”
    await state.clear()
    await message.answer(
        "Главное меню:",
        reply_markup=main_menu_kb(),
        parse_mode="HTML",
    )


# ---------- Inline “Подробнее” (edit in-place) ----------
@router.callback_query(F.data.startswith("res_event_open:"))
async def resident_event_open(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()

    if not e or e.status != EventStatus.ACTIVE:
        await callback.answer("Событие не найдено", show_alert=True)
        return

    await callback.message.edit_text(
        _event_details_text(e),
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

    full_desc = _compact(e.description)
    can_expand = bool(full_desc) and len(full_desc) > DESC_PREVIEW_LEN

    await callback.message.edit_text(
        _event_preview_text(e),
        parse_mode="HTML",
        reply_markup=event_preview_kb(event_id, can_expand),
    )
    await callback.answer()
