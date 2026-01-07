import html
import json

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, desc

from config import CITIES, DEFAULT_CITY
from database.session import get_db
from database.models import Event, EventStatus, EventCategory

router = Router()

CITIES_PER_PAGE = 5
EVENTS_PER_PAGE = 5


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
    # tiers json (для выставок/сложных ценников)
    apj = getattr(e, "admission_price_json", None)
    if apj:
        try:
            data = json.loads(apj)
            if isinstance(data, dict) and data:
                # сохраняем стабильный порядок
                order = ["все", "дети", "студенты", "взрослые", "пенсионеры"]
                parts = []
                for k in order:
                    if k in data:
                        parts.append(f"{k}: {_fmt_rub(data[k])}")
                # если пришли нестандартные ключи
                for k, v in data.items():
                    if k not in order:
                        parts.append(f"{k}: {_fmt_rub(v)}")
                return ", ".join(parts)
        except Exception:
            pass

    # simple float
    return _fmt_rub(e.price_admission)


def _price_label(e: Event) -> str:
    # Для концертов просили “Цена от”
    if e.category == EventCategory.CONCERT:
        return "Цена билета от"
    return "Цена билета"


def _format_free_kids(e: Event) -> str | None:
    age = getattr(e, "free_kids_upto_age", None)
    if age is None:
        return None
    return f"Бесплатно: детям до {age} лет"


def _short_description(text: str | None, limit: int = 350) -> str:
    if not text:
        return "—"
    t = " ".join(text.split())  # убрать лишние пробелы/переносы
    if len(t) <= limit:
        return t
    return t[:limit].rstrip() + "…"


def events_nav_kb(city_slug: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()

    if page > 0:
        kb.button(text="« Назад", callback_data=f"res_events:{city_slug}:{page-1}")
    if page < total_pages - 1:
        kb.button(text="Вперёд »", callback_data=f"res_events:{city_slug}:{page+1}")

    kb.adjust(2)
    kb.button(text="🌍 Сменить город", callback_data="res_nav:cities")
    kb.button(text="🏠 Главное меню", callback_data="res_nav:main")
    kb.adjust(1)

    return kb.as_markup()


async def send_events_page(message: Message, city_slug: str, page: int = 0):
    city_name = CITIES.get(city_slug, {}).get("name", city_slug)

    async with get_db() as db:
        ids = (
            await db.execute(
                select(Event.id).where(
                    Event.city_slug == city_slug,
                    Event.status == EventStatus.ACTIVE,
                )
            )
        ).all()
        total = len(ids)

        if total == 0:
            await message.answer(
                f"✅ <b>{h(city_name)} выбран!</b>\n\nПока опубликованных событий нет.",
                parse_mode="HTML",
                reply_markup=events_nav_kb(city_slug, 0, 1),
            )
            return

        total_pages = (total + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE
        page = max(0, min(page, total_pages - 1))
        offset = page * EVENTS_PER_PAGE

        events = (
            await db.execute(
                select(Event)
                .where(
                    Event.city_slug == city_slug,
                    Event.status == EventStatus.ACTIVE,
                )
                .order_by(desc(Event.created_at))
                .offset(offset)
                .limit(EVENTS_PER_PAGE)
            )
        ).scalars().all()

    lines = [
        f"🏠 <b>События города: {h(city_name)}</b>",
        f"Страница: {page+1}/{total_pages}",
        "",
    ]

    for e in events:
        price_line = f"{_price_label(e)}: {h(_format_admission_value(e))}"
        free_kids_line = _format_free_kids(e)

        block = [
            "━━━━━━━━━━━━━━━━━━",
            f"<b>{h(e.title)}</b>",
            f"Категория: {h(_category_ru(e.category))}",
            f"Когда: {h(_format_event_datetime(e))}",
            f"Где: {h(e.location)}",
            price_line,
        ]
        if free_kids_line:
            block.append(h(free_kids_line))

        block.append(f"Описание: {h(_short_description(e.description))}")

        lines.append("\n".join(block))
        lines.append("")  # пустая строка между событиями

    await message.answer(
        "\n".join(lines).strip(),
        parse_mode="HTML",
        reply_markup=events_nav_kb(city_slug, page, total_pages),
    )


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
    await send_events_page(callback.message, slug, page=0)
    await callback.answer()


@router.callback_query(F.data.startswith("res_events:"))
async def resident_events_page(callback: CallbackQuery):
    _, city_slug, page_str = callback.data.split(":")
    await send_events_page(callback.message, city_slug, page=int(page_str))
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
