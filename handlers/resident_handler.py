import html
import urllib.parse
from datetime import date, timedelta

from aiogram import Router, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.deep_linking import create_start_link
from sqlalchemy import select, desc, and_, or_, func

from config import CITIES, DEFAULT_CITY
from database.session import get_db
from database.models import Event, EventStatus, EventCategory, EventPhoto, Favorite
from services.user_activity import touch_user

router = Router()

CITIES_PER_PAGE = 5
EVENTS_LIMIT_DEFAULT = 15
DESC_PREVIEW_LEN = 100
MAX_PHOTOS = 5


# ---------------- FSM ----------------
class ResidentState(StatesGroup):
    choosing_city = State()
    choosing_period = State()
    choosing_category = State()
    browsing = State()


# ---------------- Utils ----------------
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


async def _touch_from_message(message: Message) -> None:
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )


async def _touch_from_callback(callback: CallbackQuery) -> None:
    await touch_user(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
    )


# ---------------- Share / Favorites ----------------
async def build_share_url(bot, event_id: int, title: str | None = None) -> str:
    deeplink = await create_start_link(bot, f"e{event_id}", encode=False)
    text = "Смотри событие в EventsNow" if not title else f"Смотри: {title}"
    return "https://t.me/share/url?" + urllib.parse.urlencode({"url": deeplink, "text": text})


async def is_favorite(user_id: int, event_id: int) -> bool:
    async with get_db() as db:
        fav = (
            await db.execute(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        return fav is not None


async def set_favorite(user_id: int, event_id: int, value: bool) -> bool:
    async with get_db() as db:
        fav = (
            await db.execute(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.event_id == event_id,
                )
            )
        ).scalar_one_or_none()

        if value:
            if fav:
                return True
            db.add(Favorite(user_id=user_id, event_id=event_id))
            return True

        if fav:
            await db.delete(fav)
        return False


# ---------------- Keyboards ----------------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Житель"), KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="✍️ Обратная связь")],
        ],
        resize_keyboard=True,
    )


def resident_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🕘 Сегодня"), KeyboardButton(text="📆 3 дня"), KeyboardButton(text="📅 Неделя")],
            [KeyboardButton(text="🗓 Месяц"), KeyboardButton(text="🆕 Последние"), KeyboardButton(text="🏷 Категории")],
            [KeyboardButton(text="⭐ Моё избранное"), KeyboardButton(text="🗂 Архив"), KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )

def city_choice_kb() -> ReplyKeyboardMarkup:
    """Нижняя клавиатура выбора города (пока 4 города)"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Ноябрьск"), KeyboardButton(text="🏙 Муравленко")],
            [KeyboardButton(text="🏙 Губкинский"), KeyboardButton(text="🏙 Новый Уренгой")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


def period_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🕘 Сегодня"), KeyboardButton(text="📆 3 дня"), KeyboardButton(text="📅 Неделя")],
            [KeyboardButton(text="🗓 Месяц"), KeyboardButton(text="🆕 Последние")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


def category_kb() -> ReplyKeyboardMarkup:
    # Важно: "Все категории" отдельной кнопкой
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🧩 Все категории")],
            [KeyboardButton(text="🖼 Выставка"), KeyboardButton(text="🧑🏫 Мастер-класс")],
            [KeyboardButton(text="🎤 Концерт"), KeyboardButton(text="🎭 Выступление")],
            [KeyboardButton(text="🎓 Лекция/семинар"), KeyboardButton(text="✨ Другое")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


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


# ---------------- Formatting ----------------
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
        "MASTERCLASS": "🧑🏫",
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
    raw_json = getattr(e, "admission_price_json", None)
    if raw_json:
        try:
            import json

            data = json.loads(raw_json)
            if isinstance(data, dict) and data:
                items: list[tuple[str, float]] = []
                for k, v in data.items():
                    if k is None:
                        continue
                    key = str(k).strip()
                    if not key:
                        continue
                    try:
                        val = float(v)
                    except Exception:
                        continue
                    if val < 0:
                        continue
                    items.append((key, val))

                if items:
                    preferred = ["все", "дети", "студенты", "взрослые", "пенсионеры"]
                    order = {name: i for i, name in enumerate(preferred)}
                    items.sort(key=lambda kv: (order.get(kv[0].lower(), 999), kv[0].lower()))

                    def _fmt_num(x: float) -> str:
                        return str(int(x)) if float(x).is_integer() else str(x)

                    if len(items) == 1 and items[0][0].lower() == "все":
                        s = _fmt_num(items[0][1])
                        if e.category == EventCategory.CONCERT:
                            return f"от {s} ₽"
                        return f"{s} ₽"

                    parts = [f"{k} — {_fmt_num(v)} ₽" for k, v in items]
                    return "; ".join(parts)
        except Exception:
            pass

    if e.price_admission is None:
        return "—"

    try:
        v = float(e.price_admission)
        s = str(int(v)) if v.is_integer() else str(v)
    except Exception:
        s = str(e.price_admission)

    if e.category == EventCategory.CONCERT:
        return f"от {s} ₽"
    return f"{s} ₽"


# ---------------- Data fetch ----------------
async def fetch_event_photos(event_id: int) -> list[EventPhoto]:
    async with get_db() as db:
        rows = (
            await db.execute(
                select(EventPhoto)
                .where(EventPhoto.event_id == event_id)
                .order_by(EventPhoto.position.asc())
                .limit(MAX_PHOTOS)
            )
        ).scalars().all()
        return list(rows)


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


def _event_is_upcoming_or_ongoing_condition(today: date):
    return or_(
        and_(Event.event_date.is_not(None), Event.event_date >= today),
        and_(Event.period_end.is_not(None), Event.period_end >= today),
    )


async def fetch_events(city_slug: str, mode: str, category: EventCategory | None = None):
    """
    category=None => все категории
    """
    today = date.today()
    start_dt = func.coalesce(Event.event_date, Event.period_start)

    if mode == "archive":
        # Архив не категоризируем (по твоему требованию)
        where = [
            Event.city_slug == city_slug,
            Event.status == EventStatus.ARCHIVED,
        ]
        order_by = [start_dt.desc().nullslast(), desc(Event.created_at)]
    else:
        where = [
            Event.city_slug == city_slug,
            Event.status == EventStatus.ACTIVE,
        ]

        # category фильтр только для не-архива
        if category is not None:
            where.append(Event.category == category)

        if mode == "today":
            where.append(_event_overlaps_range_condition(today, today))
            # Сортировка от позднего к раннему
            order_by = [start_dt.desc().nullslast(), desc(Event.created_at)]
        elif mode in ("3d", "7d", "30d"):
            days = int(mode.replace("d", ""))
            d2 = today + timedelta(days=days - 1)
            where.append(_event_overlaps_range_condition(today, d2))
            order_by = [start_dt.desc().nullslast(), desc(Event.created_at)]
        else:
            mode = "last"
            where.append(_event_is_upcoming_or_ongoing_condition(today))
            # "последние" тоже от позднего к раннему по дате события (fallback created_at)
            order_by = [start_dt.desc().nullslast(), desc(Event.created_at)]

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


async def fetch_favorite_event_ids(user_id: int, city_slug: str | None = None) -> list[int]:
    async with get_db() as db:
        q = (
            select(Favorite.event_id)
            .join(Event, Event.id == Favorite.event_id)
            .where(
                Favorite.user_id == user_id,
                Event.status == EventStatus.ACTIVE,
            )
            .order_by(desc(Favorite.added_at))
        )
        if city_slug:
            q = q.where(Event.city_slug == city_slug)

        ids = (await db.execute(q)).scalars().all()
        return list(ids)


async def fetch_event(event_id: int) -> Event | None:
    async with get_db() as db:
        return (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()


# ---------------- Cards ----------------
def event_preview_kb(event_id: int, can_expand: bool, fav: bool, share_url: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    star_text = "⭐ В избранное" if not fav else "✅ В избранном"
    kb.button(text=star_text, callback_data=f"res_fav_toggle:{event_id}")
    kb.button(text="🔗 Поделиться", url=share_url)
    if can_expand:
        kb.button(text="🔎 Подробнее", callback_data=f"res_event_open:{event_id}:1")
    kb.adjust(2, 1)
    return kb.as_markup()


def event_details_kb(
    event_id: int,
    idx: int,
    total: int,
    fav: bool,
    share_url: str,
    back_cb: str | None = None,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    star_text = "⭐ В избранное" if not fav else "✅ В избранном"
    kb.button(text=star_text, callback_data=f"res_fav_toggle:{event_id}")
    kb.button(text="🔗 Поделиться", url=share_url)

    if total > 1:
        if idx > 1:
            kb.button(text="⬅️", callback_data=f"res_event_open:{event_id}:{idx-1}")
        kb.button(text=f"Фото {idx}/{total}", callback_data="noop")
        if idx < total:
            kb.button(text="➡️", callback_data=f"res_event_open:{event_id}:{idx+1}")
        kb.adjust(3)

    if back_cb:
        kb.button(text="↩️ В избранное", callback_data=back_cb)

    kb.button(text="❌ Закрыть", callback_data=f"res_event_close:{event_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def favorites_carousel_kb(
    pos: int,
    total: int,
    event_id: int,
    fav: bool,
    can_expand: bool,
    city_slug: str | None,
    share_url: str,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    city_part = city_slug or "all"

    if total > 1:
        kb.button(text="⬅️", callback_data=f"res_fav_car:{max(0, pos-1)}:{city_part}")
        kb.button(text=f"{pos+1}/{total}", callback_data="noop")
        kb.button(text="➡️", callback_data=f"res_fav_car:{min(total-1, pos+1)}:{city_part}")
        kb.adjust(3)

    star_text = "✅ В избранном" if fav else "⭐ В избранное"
    kb.button(text=star_text, callback_data=f"res_fav_toggle:{event_id}")
    kb.button(text="🔗 Поделиться", url=share_url)

    if can_expand:
        kb.button(text="🔎 Подробнее", callback_data=f"res_event_open_fav:{event_id}:1:{pos}:{city_part}")

    kb.button(text="❌ Закрыть", callback_data="res_fav_close")
    kb.adjust(2, 1, 1)
    return kb.as_markup()


def event_preview_text(e: Event) -> str:
    cat = f"{category_emoji(e.category)} {category_ru(e.category)}"
    return (
        f"{h(e.title)}\n"
        f"{h(cat)}\n\n"
        f"Когда: {h(fmt_when(e))}\n"
        f"Где: {h(e.location)}\n"
        f"Цена: {h(fmt_price(e))}\n\n"
        f"{h(short(e.description))}"
    )


def event_details_text(e: Event) -> str:
    cat = f"{category_emoji(e.category)} {category_ru(e.category)}"
    city_name = CITIES.get(e.city_slug, {}).get("name", e.city_slug)
    return (
        f"{h(e.title)}\n"
        f"{h(cat)}\n"
        f"{h(city_name)}\n\n"
        f"Когда: {h(fmt_when(e))}\n"
        f"Где: {h(e.location)}\n"
        f"Цена: {h(fmt_price(e))}\n\n"
        f"{h(compact(e.description) or '—')}"
    )


async def send_event_preview(message: Message, e: Event):
    full_desc = compact(e.description)
    can_expand = bool(full_desc) and len(full_desc) > DESC_PREVIEW_LEN
    fav = await is_favorite(message.from_user.id, e.id)
    share_url = await build_share_url(message.bot, e.id, title=e.title)
    photos = await fetch_event_photos(e.id)

    if photos:
        await message.answer_photo(
            photo=photos[0].file_id,
            caption=event_preview_text(e),
            parse_mode="HTML",
            reply_markup=event_preview_kb(e.id, can_expand, fav, share_url),
        )
    else:
        await message.answer(
            event_preview_text(e),
            parse_mode="HTML",
            reply_markup=event_preview_kb(e.id, can_expand, fav, share_url),
        )


async def send_events_list(message: Message, city_slug: str, mode: str, category: EventCategory | None):
    city_name = CITIES.get(city_slug, {}).get("name", city_slug)
    title_map = {
        "last": "🆕 Последние (актуальные)",
        "today": "🕘 Сегодня",
        "3d": "📆 3 дня",
        "7d": "📅 Неделя",
        "30d": "🗓 Месяц",
        "archive": "🗂 Архив",
    }

    events, mode = await fetch_events(city_slug, mode, category=category)
    cat_label = "Все категории" if category is None else category_ru(category)

    await message.answer(
        f"{h(city_name)}\n{h(title_map.get(mode, mode))}\nКатегория: {h(cat_label)}\n"
        f"Показано: {len(events)} (лимит {EVENTS_LIMIT_DEFAULT})",
        parse_mode="HTML",
    )

    if not events:
        await message.answer("Пока нет событий по выбранному фильтру.", parse_mode="HTML")
        return

    for e in events:
        await send_event_preview(message, e)


    await message.answer(
        "Можно поменять период, открыть избранное или архив.",
        reply_markup=resident_menu_kb(),
        parse_mode="HTML",
    )

# ---------------- Favorites UI ----------------
async def show_favorites_carousel(
    message: Message,
    user_id: int,
    city_slug: str | None,
    pos: int,
    edit_message: Message | None = None,
):
    ids = await fetch_favorite_event_ids(user_id=user_id, city_slug=city_slug)
    total = len(ids)

    city_title = "Все города" if not city_slug else CITIES.get(city_slug, {}).get("name", city_slug)
    header = f"⭐ Моё избранное • {h(city_title)}"

    if total == 0:
        text = f"{header}\n\nПока пусто."
        if edit_message:
            try:
                await edit_message.edit_text(text, parse_mode="HTML", reply_markup=None)
            except Exception:
                await message.answer(text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
        return

    pos = max(0, min(pos, total - 1))
    event_id = ids[pos]
    e = await fetch_event(event_id)

    if not e or e.status != EventStatus.ACTIVE:
        text = f"{header}\n\nСобытие недоступно."
        if edit_message:
            try:
                await edit_message.edit_text(text, parse_mode="HTML", reply_markup=None)
            except Exception:
                await message.answer(text, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")
        return

    full_desc = compact(e.description)
    can_expand = bool(full_desc) and len(full_desc) > DESC_PREVIEW_LEN
    share_url = await build_share_url(message.bot, e.id, title=e.title)

    caption = f"{header}\n\n{event_preview_text(e)}"
    kb = favorites_carousel_kb(pos, total, e.id, True, can_expand, city_slug, share_url)

    photos = await fetch_event_photos(e.id)
    if photos:
        media = InputMediaPhoto(media=photos[0].file_id, caption=caption, parse_mode="HTML")
        if edit_message:
            try:
                await edit_message.edit_media(media=media, reply_markup=kb)
            except Exception:
                await message.answer_photo(photos[0].file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer_photo(photos[0].file_id, caption=caption, parse_mode="HTML", reply_markup=kb)
    else:
        if edit_message:
            try:
                await edit_message.edit_text(caption, parse_mode="HTML", reply_markup=kb)
            except Exception:
                await message.answer(caption, parse_mode="HTML", reply_markup=kb)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=kb)


# ---------------- Resident flow ----------------
TEXT_TO_MODE = {
    "🕘 Сегодня": "today",
    "📆 3 дня": "3d",
    "📅 Неделя": "7d",
    "🗓 Месяц": "30d",
    "🆕 Последние": "last",
    "🗂 Архив": "archive",
}

TEXT_TO_CATEGORY: dict[str, EventCategory | None] = {
    "🧩 Все категории": None,
    "🖼 Выставка": EventCategory.EXHIBITION,
    "🧑🏫 Мастер-класс": EventCategory.MASTERCLASS,
    "🎤 Концерт": EventCategory.CONCERT,
    "🎭 Выступление": EventCategory.PERFORMANCE,
    "🎓 Лекция/семинар": EventCategory.LECTURE,
    "✨ Другое": EventCategory.OTHER,
}


@router.message(F.text == "🏠 Житель")
async def resident_entry(message: Message, state: FSMContext):
    await _touch_from_message(message)
    await state.clear()
    await state.set_state(ResidentState.choosing_city)

    default_city_name = CITIES.get(DEFAULT_CITY, {}).get("name", DEFAULT_CITY)
    await message.answer(
        f"Город по умолчанию: {h(default_city_name)}",
        reply_markup=resident_menu_kb(),
        parse_mode="HTML",
    )
    await message.answer(
        "Выбери город:",
        reply_markup=city_choice_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("res_page:"))
async def resident_page_cb(callback: CallbackQuery):
    page = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=cities_keyboard(page=page))
    await callback.answer()


CITY_TEXT_TO_SLUG = {
    "✅ Ноябрьск": "nojabrsk",
    "🏙 Муравленко": "muravlenko",
    "🏙 Губкинский": "gubkinskiy",
    "🏙 Новый Уренгой": "novy_urengoy",
}

@router.message(ResidentState.choosing_city, F.text.in_(set(CITY_TEXT_TO_SLUG.keys())))
async def resident_choose_city_from_bottom(message: Message, state: FSMContext):
    await _touch_from_message(message)

    slug = CITY_TEXT_TO_SLUG.get(message.text)
    if not slug:
        await message.answer("Выбери город кнопками ниже.", reply_markup=city_choice_kb())
        return

    # Ноябрьск — рабочий
    if slug == "nojabrsk":
        await state.set_state(ResidentState.choosing_period)
        await state.update_data(city_slug=slug, mode=None, category=None)

        city_name = (CITIES.get(slug) or {}).get("name", slug)
        await message.answer(f"{h(city_name)} выбран!", parse_mode="HTML")
        await message.answer("Выберите период:", reply_markup=period_kb(), parse_mode="HTML")
        return

    # Остальные города — заглушка
    city_name = (CITIES.get(slug) or {}).get("name", slug)
    await message.answer(
        f"{h(city_name)} — раздел в разработке.",
        parse_mode="HTML",
        reply_markup=city_choice_kb(),
    )


@router.callback_query(F.data.startswith("res_city:"))
async def resident_city_select(callback: CallbackQuery, state: FSMContext):
    await _touch_from_callback(callback)

    slug = callback.data.split(":")[1]
    info = CITIES.get(slug)
    if not info:
        await callback.answer("Город не найден", show_alert=True)
        return

    city_name = info["name"]
    status = info.get("status", "comingsoon")

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if status != "active":
        await callback.message.answer(f"{h(city_name)}\nГород пока недоступен.", parse_mode="HTML")
        await callback.answer()
        return

    # Новый flow: сначала период
    await state.set_state(ResidentState.choosing_period)
    await state.update_data(city_slug=slug, mode=None, category=None)

    await callback.message.answer(f"{h(city_name)} выбран!", parse_mode="HTML")
    await callback.message.answer("Выбери период проведения мероприятий:", reply_markup=period_kb(), parse_mode="HTML")
    await callback.answer()


@router.message(ResidentState.choosing_period, F.text.in_({"🕘 Сегодня", "📆 3 дня", "📅 Неделя", "🗓 Месяц", "🆕 Последние"}))
async def resident_choose_period(message: Message, state: FSMContext):
    await _touch_from_message(message)

    mode = TEXT_TO_MODE.get(message.text)
    if not mode:
        await message.answer("Выбери период кнопкой ниже.", reply_markup=period_kb())
        return

    await state.update_data(mode=mode)
    await state.set_state(ResidentState.choosing_category)

    await message.answer("Теперь выбери категорию:", reply_markup=category_kb(), parse_mode="HTML")


@router.message(ResidentState.choosing_category, F.text.in_(set(TEXT_TO_CATEGORY.keys())))
async def resident_choose_category(message: Message, state: FSMContext):
    await _touch_from_message(message)

    data = await state.get_data()
    city_slug = data.get("city_slug")
    mode = data.get("mode")
    if not city_slug or not mode:
        await message.answer("Сначала выбери город и период.", parse_mode="HTML")
        return

    category = TEXT_TO_CATEGORY.get(message.text, None)
    await state.update_data(category=category)
    await state.set_state(ResidentState.browsing)

    await send_events_list(message, city_slug, mode, category)


# Быстрые фильтры периодов (старые кнопки) в browsing — оставляем, но теперь учитываем category
@router.message(ResidentState.browsing, F.text.in_({"🕘 Сегодня", "📆 3 дня", "📅 Неделя", "🗓 Месяц", "🆕 Последние"}))
async def resident_filters_browsing(message: Message, state: FSMContext):
    await _touch_from_message(message)

    data = await state.get_data()
    city_slug = data.get("city_slug")
    if not city_slug:
        await message.answer("Сначала выбери город.", parse_mode="HTML")
        return

    mode = TEXT_TO_MODE.get(message.text, "last")
    await state.update_data(mode=mode)

    category = data.get("category")
    await send_events_list(message, city_slug, mode, category)


@router.message(ResidentState.browsing, F.text.in_(set(TEXT_TO_CATEGORY.keys())))
async def resident_change_category_browsing(message: Message, state: FSMContext):
    await _touch_from_message(message)

    data = await state.get_data()
    city_slug = data.get("city_slug")
    mode = data.get("mode") or "last"
    if not city_slug:
        await message.answer("Сначала выбери город.", parse_mode="HTML")
        return

    category = TEXT_TO_CATEGORY.get(message.text, None)
    await state.update_data(category=category)

    await send_events_list(message, city_slug, mode, category)


# Архив оставляем как было (без категоризации)
@router.message(F.text == "🗂 Архив")
async def resident_archive(message: Message, state: FSMContext):
    await _touch_from_message(message)

    data = await state.get_data()
    city_slug = data.get("city_slug")
    if not city_slug:
        await message.answer("Сначала выбери город.", parse_mode="HTML")
        return

    await state.set_state(ResidentState.browsing)
    await state.update_data(mode="archive")
    await send_events_list(message, city_slug, "archive", category=None)


@router.message(F.text == "⭐ Моё избранное")
async def resident_favorites_entry(message: Message, state: FSMContext):
    await _touch_from_message(message)
    data = await state.get_data()
    city_slug = data.get("city_slug")
    await show_favorites_carousel(
        message=message,
        user_id=message.from_user.id,
        city_slug=city_slug,
        pos=0,
        edit_message=None,
    )


@router.message(F.text == "⬅️ Назад")
async def resident_back(message: Message, state: FSMContext):
    await _touch_from_message(message)

    cur = await state.get_state()
    data = await state.get_data()
    city_slug = data.get("city_slug")

    # Если мы внутри Жителя и город уже выбран — "назад" ведёт на выбор периода
    if city_slug and cur in (
        ResidentState.choosing_category.state,
        ResidentState.browsing.state,
    ):
        await state.set_state(ResidentState.choosing_period)
        await message.answer("Выбери период проведения мероприятий:", reply_markup=period_kb(), parse_mode="HTML")
        return

    # Если уже на выборе периода — возвращаем на выбор города (не в главное меню)
    if cur == ResidentState.choosing_period.state:
        await state.set_state(ResidentState.choosing_city)
        await message.answer("Выбери город:", reply_markup=city_choice_kb(), parse_mode="HTML")
        return

    # Во всех остальных случаях (не Житель-флоу) — как было: в главное меню
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())

@router.message(F.text == "🏷 Категории")
async def resident_open_categories(message: Message, state: FSMContext):
    await _touch_from_message(message)

    data = await state.get_data()
    if not data.get("city_slug"):
        await message.answer("Сначала выбери город.", parse_mode="HTML")
        return

    # Переходим на выбор категории, период остаётся сохранённым в state
    await state.set_state(ResidentState.choosing_category)
    await message.answer("Выбери категорию:", reply_markup=category_kb(), parse_mode="HTML")


# ---------------- Callbacks: favorites carousel ----------------
@router.callback_query(F.data.startswith("res_fav_car:"))
async def resident_favorites_carousel_cb(callback: CallbackQuery):
    await _touch_from_callback(callback)
    parts = callback.data.split(":")
    pos = int(parts[1])
    city_part = parts[2] if len(parts) >= 3 else "all"
    city_slug = None if city_part == "all" else city_part
    await show_favorites_carousel(
        message=callback.message,
        user_id=callback.from_user.id,
        city_slug=city_slug,
        pos=pos,
        edit_message=callback.message,
    )
    await callback.answer()


@router.callback_query(F.data == "res_fav_close")
async def resident_favorites_close_cb(callback: CallbackQuery):
    await _touch_from_callback(callback)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()


# ---------------- Callbacks: open event from favorites ----------------
@router.callback_query(F.data.startswith("res_event_open_fav:"))
async def resident_event_open_from_fav(callback: CallbackQuery):
    await _touch_from_callback(callback)
    parts = callback.data.split(":")
    event_id = int(parts[1])
    idx = int(parts[2])
    pos = int(parts[3])
    city_part = parts[4] if len(parts) >= 5 else "all"

    e = await fetch_event(event_id)
    if not e or e.status != EventStatus.ACTIVE:
        await callback.answer("Событие недоступно", show_alert=True)
        return

    fav = await is_favorite(callback.from_user.id, event_id)
    photos = await fetch_event_photos(event_id)
    total = len(photos)
    back_cb = f"res_fav_car:{pos}:{city_part}"
    share_url = await build_share_url(callback.bot, event_id, title=e.title)

    if total == 0:
        await callback.message.edit_text(
            event_details_text(e),
            parse_mode="HTML",
            reply_markup=event_details_kb(event_id, 1, 0, fav, share_url, back_cb=back_cb),
        )
        await callback.answer()
        return

    idx = max(1, min(idx, total))
    file_id = photos[idx - 1].file_id

    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=file_id, caption=event_details_text(e), parse_mode="HTML"),
            reply_markup=event_details_kb(event_id, idx, total, fav, share_url, back_cb=back_cb),
        )
    except Exception:
        await callback.message.answer_photo(
            photo=file_id,
            caption=event_details_text(e),
            parse_mode="HTML",
            reply_markup=event_details_kb(event_id, idx, total, fav, share_url, back_cb=back_cb),
        )

    await callback.answer()


# ---------------- Callbacks: open event ----------------
@router.callback_query(F.data.startswith("res_event_open:"))
async def resident_event_open(callback: CallbackQuery):
    await _touch_from_callback(callback)
    parts = callback.data.split(":")
    event_id = int(parts[1])
    idx = int(parts[2]) if len(parts) >= 3 else 1

    e = await fetch_event(event_id)
    if not e or e.status != EventStatus.ACTIVE:
        await callback.answer("Событие недоступно", show_alert=True)
        return

    fav = await is_favorite(callback.from_user.id, event_id)
    photos = await fetch_event_photos(event_id)
    total = len(photos)
    share_url = await build_share_url(callback.bot, event_id, title=e.title)

    if total == 0:
        await callback.message.edit_text(
            event_details_text(e),
            parse_mode="HTML",
            reply_markup=event_details_kb(event_id, 1, 0, fav, share_url, back_cb=None),
        )
        await callback.answer()
        return

    idx = max(1, min(idx, total))
    file_id = photos[idx - 1].file_id

    try:
        await callback.message.edit_media(
            media=InputMediaPhoto(media=file_id, caption=event_details_text(e), parse_mode="HTML"),
            reply_markup=event_details_kb(event_id, idx, total, fav, share_url, back_cb=None),
        )
    except Exception:
        await callback.message.answer_photo(
            photo=file_id,
            caption=event_details_text(e),
            parse_mode="HTML",
            reply_markup=event_details_kb(event_id, idx, total, fav, share_url, back_cb=None),
        )

    await callback.answer()


@router.callback_query(F.data.startswith("res_event_close:"))
async def resident_event_close(callback: CallbackQuery):
    await _touch_from_callback(callback)
    event_id = int(callback.data.split(":")[1])

    e = await fetch_event(event_id)
    if not e or e.status != EventStatus.ACTIVE:
        await callback.answer("Событие недоступно", show_alert=True)
        return

    fav = await is_favorite(callback.from_user.id, event_id)
    full_desc = compact(e.description)
    can_expand = bool(full_desc) and len(full_desc) > DESC_PREVIEW_LEN
    share_url = await build_share_url(callback.bot, event_id, title=e.title)
    photos = await fetch_event_photos(event_id)

    if photos:
        try:
            await callback.message.edit_media(
                media=InputMediaPhoto(media=photos[0].file_id, caption=event_preview_text(e), parse_mode="HTML"),
                reply_markup=event_preview_kb(event_id, can_expand, fav, share_url),
            )
        except Exception:
            await callback.message.answer_photo(
                photo=photos[0].file_id,
                caption=event_preview_text(e),
                parse_mode="HTML",
                reply_markup=event_preview_kb(event_id, can_expand, fav, share_url),
            )
    else:
        await callback.message.edit_text(
            event_preview_text(e),
            parse_mode="HTML",
            reply_markup=event_preview_kb(event_id, can_expand, fav, share_url),
        )

    await callback.answer()


@router.callback_query(F.data.startswith("res_fav_toggle:"))
async def resident_fav_toggle(callback: CallbackQuery):
    await _touch_from_callback(callback)
    event_id = int(callback.data.split(":")[1])

    e = await fetch_event(event_id)
    if not e or e.status != EventStatus.ACTIVE:
        await callback.answer("Событие недоступно", show_alert=True)
        return

    current = await is_favorite(callback.from_user.id, event_id)
    new_state = await set_favorite(callback.from_user.id, event_id, value=not current)

    is_details = False
    idx = 1
    total = 0
    has_back_to_fav = False
    back_cb = None

    if callback.message and callback.message.reply_markup:
        for row in callback.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.text == "⬅️":
                    is_details = True
                if btn.text.startswith("Фото ") and "/" in btn.text:
                    try:
                        right = btn.text.split("Фото ", 1)[1]
                        a, b = right.split("/", 1)
                        idx = int(a.strip())
                        total = int(b.strip())
                    except Exception:
                        pass
                if btn.text == "↩️ В избранное":
                    has_back_to_fav = True
                    back_cb = btn.callback_data

    share_url = await build_share_url(callback.bot, event_id, title=e.title)

    if is_details:
        if total <= 0:
            photos = await fetch_event_photos(event_id)
            total = len(photos)
        idx = min(max(1, idx), max(1, total))
        await callback.message.edit_reply_markup(
            reply_markup=event_details_kb(
                event_id,
                idx,
                total,
                new_state,
                share_url,
                back_cb=back_cb if has_back_to_fav else None,
            )
        )
        await callback.answer("Добавлено" if new_state else "Убрано")
        return

    text = (callback.message.text or "") if callback.message else ""
    caption = (callback.message.caption or "") if callback.message else ""
    is_fav_carousel = ("⭐ Моё избранное" in text) or ("⭐ Моё избранное" in caption)

    if is_fav_carousel:
        pos = 0
        city_slug = None
        if callback.message and callback.message.reply_markup:
            for row in callback.message.reply_markup.inline_keyboard:
                for btn in row:
                    if btn.callback_data and btn.callback_data.startswith("res_fav_car:"):
                        parts = btn.callback_data.split(":")
                        if len(parts) >= 3:
                            city_part = parts[2]
                            city_slug = None if city_part == "all" else city_part
                        try:
                            pos = int(parts[1])
                        except Exception:
                            pass
                        break

        await show_favorites_carousel(
            message=callback.message,
            user_id=callback.from_user.id,
            city_slug=city_slug,
            pos=max(0, pos),
            edit_message=callback.message,
        )
        await callback.answer("Добавлено" if new_state else "Убрано")
        return

    full_desc = compact(e.description)
    can_expand = bool(full_desc) and len(full_desc) > DESC_PREVIEW_LEN
    await callback.message.edit_reply_markup(
        reply_markup=event_preview_kb(event_id, can_expand, new_state, share_url)
    )
    await callback.answer("Добавлено" if new_state else "Убрано")


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()
