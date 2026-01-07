from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import CITIES, DEFAULT_CITY

router = Router()

CITIES_PER_PAGE = 5


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

    # pagination
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="« Назад", callback_data=f"res_page:{page-1}")
    if page < total_pages - 1:
        nav.button(text="Вперёд »", callback_data=f"res_page:{page+1}")
    if page > 0 or page < total_pages - 1:
        kb.row(*nav.buttons)

    # extras (геолокацию пока оставляем, но можно временно не использовать)
    kb.button(text="🔍 Поиск города", callback_data="res_search:city")
    kb.button(text="🏠 Главное меню", callback_data="res_nav:main")

    kb.adjust(1)
    return kb.as_markup()


@router.message(F.text == "🏠 Житель")
async def resident_entry(message: Message):
    default_city_name = CITIES.get(DEFAULT_CITY, {}).get("name", "Город не задан")
    await message.answer(
        f"🏠 **Житель**\n\n"
        f"🌍 По умолчанию: *{default_city_name}*\n\n"
        "👇 Выбери город:",
        reply_markup=cities_keyboard(page=0),
        parse_mode="Markdown",
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

    # 1) Убираем клавиатуру у сообщения со списком городов
    # чтобы пользователь не нажимал ещё раз и чтобы "подтверждение" было ниже.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # 2) Отправляем новое сообщение — оно будет НИЖЕ (последним)
    if status != "active":
        await callback.message.answer(
            f"⏳ **{city_name}** — раздел в разработке.\n\n"
            "Выбери другой город:",
            reply_markup=cities_keyboard(page=0),
            parse_mode="Markdown",
        )
    else:
        await callback.message.answer(
            f"✅ **{city_name} выбран!**\n\n"
            "Пока событий нет — дальше подключим организаторов и модерацию.",
            parse_mode="Markdown",
        )

    await callback.answer()
