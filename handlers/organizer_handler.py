import html
import json
from datetime import datetime, date as ddate

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import ADMIN_IDS, CITIES, DEFAULT_CITY
from services.payment_service import calculate_price, PricingError

from database.session import get_db
from database.models import (
    User,
    UserRole,
    Event,
    EventCategory,
    EventStatus,
    PaymentStatus,
    EventPhoto,
)

router = Router()

DESC_PREVIEW_LEN = 140


# ---------- helpers ----------
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


def _parse_date(s: str) -> ddate:
    return datetime.strptime(s, "%d.%m.%Y").date()


def _parse_time(s: str):
    return datetime.strptime(s, "%H:%M").time()


CATEGORY_LABELS_RU = {
    "EXHIBITION": "Выставка",
    "MASTERCLASS": "Мастер-класс",
    "CONCERT": "Концерт",
    "PERFORMANCE": "Выступление",
    "LECTURE": "Лекция/семинар",
    "OTHER": "Другое",
}

PRICE_TIER_PRESETS = {
    "one": ["все"],
    "child_adult": ["дети", "взрослые"],
    "full": ["дети", "студенты", "взрослые", "пенсионеры"],
}


def _format_category_ru(code: str) -> str:
    return CATEGORY_LABELS_RU.get(code, code)


def _format_period_or_date(data: dict) -> str:
    if data.get("event_date"):
        # ISO yyyy-mm-dd
        d = ddate.fromisoformat(data["event_date"])
        return d.strftime("%d.%m.%Y")
    if data.get("period_start") and data.get("period_end"):
        ps = ddate.fromisoformat(data["period_start"]).strftime("%d.%m.%Y")
        pe = ddate.fromisoformat(data["period_end"]).strftime("%d.%m.%Y")
        return f"{ps}-{pe}"
    return "—"


def _format_free_kids(data: dict) -> str:
    age = data.get("free_kids_upto_age")
    if age is None:
        return "нет"
    return f"да, до {age}"


def _format_admission_price(data: dict) -> str:
    ap = data.get("admission_price")
    if ap is None:
        return "—"
    if isinstance(ap, (int, float)):
        v = float(ap)
        s = str(int(v)) if v.is_integer() else str(v)
        # правило "от" для концерта (у тебя оно в resident, здесь тоже красиво)
        if data.get("category") == "CONCERT":
            return f"от {s} ₽"
        return f"{s} ₽"
    if isinstance(ap, dict):
        parts = []
        for k, v in ap.items():
            parts.append(f"{k}={v}")
        return ", ".join(parts)
    return str(ap)


def _format_placement_short(placement: dict | None) -> str:
    if not placement:
        return "—"
    if placement.get("error"):
        return f"ошибка: {placement['error']}"
    package = placement.get("package_name") or placement.get("packagename") or placement.get("package") or "—"
    model = placement.get("model") or "—"
    total = placement.get("total_price") or placement.get("totalprice") or placement.get("price") or "—"
    return f"{package} ({model}) = {total}"


def _parse_tier_prices(text: str, allowed_keys: list[str]) -> dict:
    raw = text.replace(";", ",").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("empty")
    out = {}
    for p in parts:
        if "=" not in p:
            raise ValueError("noeq")
        k, v = p.split("=", 1)
        k = k.strip().lower()
        v = v.strip().replace(",", ".")
        if k not in allowed_keys:
            raise ValueError("badkey")
        price = float(v)
        if price < 0:
            raise ValueError("neg")
        out[k] = price
    for k in allowed_keys:
        if k not in out:
            raise ValueError("missing")
    return out


# ---------- keyboards ----------
def cities_kb_for_organizer() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for slug, info in sorted(CITIES.items(), key=lambda x: x[1]["name"]):
        emoji = "✅" if info.get("status") == "active" else "⏳"
        kb.button(text=f"{emoji} {info['name']}", callback_data=f"org_city:{slug}")
    kb.adjust(1)
    return kb.as_markup()


def categories_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🖼 Выставка", callback_data="org_cat:EXHIBITION")
    kb.button(text="🧑‍🏫 Мастер-класс", callback_data="org_cat:MASTERCLASS")
    kb.button(text="🎤 Концерт", callback_data="org_cat:CONCERT")
    kb.button(text="🎭 Выступление", callback_data="org_cat:PERFORMANCE")
    kb.button(text="🎓 Лекция/семинар", callback_data="org_cat:LECTURE")
    kb.button(text="✨ Другое", callback_data="org_cat:OTHER")
    kb.adjust(2)
    return kb.as_markup()


def yes_no_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=yes_cb)
    kb.button(text="❌ Нет", callback_data=no_cb)
    kb.adjust(2)
    return kb.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="org_confirm:yes")
    kb.button(text="❌ Отмена", callback_data="org_confirm:no")
    kb.adjust(2)
    return kb.as_markup()


def price_mode_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="1) Одна цена", callback_data="org_price_mode:one")
    kb.button(text="2) Дети/Взрослые", callback_data="org_price_mode:child_adult")
    kb.button(text="3) Полная (4 категории)", callback_data="org_price_mode:full")
    kb.adjust(1)
    return kb.as_markup()


def moderation_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"adm_ok:{event_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm_no:{event_id}")
    kb.button(text="📄 Подробнее", callback_data=f"adm_view:{event_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def photos_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="org_photos:done")
    kb.button(text="⏭ Пропустить", callback_data="org_photos:skip")
    kb.adjust(2)
    return kb.as_markup()


def organizer_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


# ---------- FSM ----------
class OrganizerEvent(StatesGroup):
    city = State()
    category = State()
    title = State()
    description = State()
    date_or_period = State()
    time_start = State()
    time_end = State()
    location = State()
    contact = State()
    admission_price_mode = State()
    admission_price = State()
    free_kids_question = State()
    free_kids_age = State()
    photos = State()  # NEW
    confirm = State()


# ---------- entry ----------
@router.message(F.text == "🎪 Организатор")
async def organizer_entry(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(OrganizerEvent.city)

    default_city_name = CITIES.get(DEFAULT_CITY, {}).get("name", DEFAULT_CITY)
    await message.answer(
        f"🎪 Организатор\n\n"
        f"🌍 По умолчанию: <b>{h(default_city_name)}</b>\n\n"
        f"👇 Выбери город:",
        reply_markup=organizer_menu_kb(),
        parse_mode="HTML",
    )
    await message.answer("Список городов:", reply_markup=cities_kb_for_organizer(), parse_mode="HTML")


@router.callback_query(F.data.startswith("org_city:"), OrganizerEvent.city)
async def organizer_city(callback: CallbackQuery, state: FSMContext):
    slug = callback.data.split(":")[1]
    info = CITIES.get(slug)
    if not info:
        await callback.answer("Город не найден", show_alert=True)
        return

    if info.get("status") != "active":
        await callback.message.answer(f"⏳ {h(info['name'])} — раздел в разработке.", parse_mode="HTML")
        await callback.answer()
        return

    await state.update_data(city_slug=slug, city_name=info["name"])
    await state.set_state(OrganizerEvent.category)

    await callback.message.answer(f"✅ {h(info['name'])}\n\nВыбери категорию:", reply_markup=categories_kb(), parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("org_cat:"), OrganizerEvent.category)
async def organizer_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split(":")[1]
    await state.update_data(category=category)
    await state.set_state(OrganizerEvent.title)

    await callback.message.answer("Введите <b>название</b> мероприятия:", parse_mode="HTML")
    await callback.answer()


@router.message(OrganizerEvent.title)
async def organizer_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if len(title) < 3:
        await message.answer("Название слишком короткое. Повтори.")
        return
    await state.update_data(title=title)
    await state.set_state(OrganizerEvent.description)
    await message.answer("Введите <b>описание</b> (минимум 10 символов):", parse_mode="HTML")


@router.message(OrganizerEvent.description)
async def organizer_description(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if len(desc) < 10:
        await message.answer("Описание слишком короткое. Повтори.")
        return
    await state.update_data(description=desc)
    await state.set_state(OrganizerEvent.date_or_period)

    await message.answer(
        "Введите дату/период:\n\n"
        "- Один день: <code>ДД.ММ.ГГГГ</code>\n"
        "- Выставка периодом: <code>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</code>\n\n"
        "Пример: <code>10.01.2026</code> или <code>10.01.2026-17.01.2026</code>",
        parse_mode="HTML",
    )


@router.message(OrganizerEvent.date_or_period)
async def organizer_date_or_period(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    data = await state.get_data()
    category = data.get("category")

    try:
        if "-" in text:
            if category != "EXHIBITION":
                await message.answer("Для этой категории нужен один день: <code>ДД.ММ.ГГГГ</code>", parse_mode="HTML")
                return
            a, b = text.split("-", 1)
            start = _parse_date(a.strip())
            end = _parse_date(b.strip())
            if start > end:
                raise ValueError("start>end")
            await state.update_data(period_start=str(start), period_end=str(end), event_date=None)
        else:
            d = _parse_date(text)
            await state.update_data(event_date=str(d), period_start=None, period_end=None)
    except Exception:
        await message.answer(
            "Неверный формат. Повтори: <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</code>",
            parse_mode="HTML",
        )
        return

    await state.set_state(OrganizerEvent.time_start)
    await message.answer("Введите <code>ЧЧ:ММ</code> (например <code>10:00</code>):", parse_mode="HTML")


@router.message(OrganizerEvent.time_start)
async def organizer_time_start(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    try:
        t = _parse_time(text)
    except Exception:
        await message.answer("Неверный формат времени. Пример: <code>10:00</code>", parse_mode="HTML")
        return
    await state.update_data(time_start=t.strftime("%H:%M"))
    await state.set_state(OrganizerEvent.time_end)
    await message.answer("Введите <code>ЧЧ:ММ</code> (например <code>20:00</code>):", parse_mode="HTML")


@router.message(OrganizerEvent.time_end)
async def organizer_time_end(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    try:
        t = _parse_time(text)
    except Exception:
        await message.answer("Неверный формат времени. Пример: <code>20:00</code>", parse_mode="HTML")
        return
    await state.update_data(time_end=t.strftime("%H:%M"))
    await state.set_state(OrganizerEvent.location)
    await message.answer("Введите <b>место проведения</b> (адрес/площадку):", parse_mode="HTML")


@router.message(OrganizerEvent.location)
async def organizer_location(message: Message, state: FSMContext):
    loc = (message.text or "").strip()
    if len(loc) < 3:
        await message.answer("Слишком коротко. Введите адрес/место.")
        return
    await state.update_data(location=loc)
    await state.set_state(OrganizerEvent.contact)
    await message.answer("Введите <b>контакт</b> (телефон/ник/ссылка одним текстом):", parse_mode="HTML")


@router.message(OrganizerEvent.contact)
async def organizer_contact(message: Message, state: FSMContext):
    contact = (message.text or "").strip()
    if len(contact) < 3:
        await message.answer("Слишком коротко. Введите контакт.")
        return
    await state.update_data(contact=contact)

    data = await state.get_data()
    if data.get("category") == "EXHIBITION":
        await state.set_state(OrganizerEvent.admission_price_mode)
        await message.answer("Выбери формат цены для выставки:", reply_markup=price_mode_kb(), parse_mode="HTML")
    else:
        await state.set_state(OrganizerEvent.admission_price)
        await message.answer("Введите цену билета числом (например <code>0</code> если бесплатно):", parse_mode="HTML")


@router.callback_query(F.data.startswith("org_price_mode:"), OrganizerEvent.admission_price_mode)
async def organizer_price_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    if mode not in PRICE_TIER_PRESETS:
        await callback.answer("Неверный вариант", show_alert=True)
        return
    await state.update_data(admission_price_mode=mode)
    await state.set_state(OrganizerEvent.admission_price)

    if mode == "one":
        example = "все=500"
        keys_str = "все"
    elif mode == "child_adult":
        example = "дети=200, взрослые=500"
        keys_str = "дети, взрослые"
    else:
        example = "дети=200, студенты=300, взрослые=500, пенсионеры=250"
        keys_str = "дети, студенты, взрослые, пенсионеры"

    await callback.message.answer(
        f"Введите цены в формате: <code>{h(example)}</code>\n"
        f"Допустимые категории: <b>{h(keys_str)}</b>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(OrganizerEvent.admission_price)
async def organizer_admission_price(message: Message, state: FSMContext):
    data = await state.get_data()
    category = data.get("category")
    text = (message.text or "").strip()

    if category == "EXHIBITION":
        mode = data.get("admission_price_mode", "one")
        keys = PRICE_TIER_PRESETS.get(mode, ["все"])
        try:
            tiers = _parse_tier_prices(text, keys)
        except Exception:
            example = "дети=200, взрослые=500" if mode == "child_adult" else "все=500"
            await message.answer(f"Неверный формат. Пример: <code>{h(example)}</code>", parse_mode="HTML")
            return
        await state.update_data(admission_price=tiers)
    else:
        t = text.replace(",", ".")
        try:
            price = float(t)
            if price < 0:
                raise ValueError
        except Exception:
            await message.answer("Введите число (например <code>0</code> или <code>1500</code>).", parse_mode="HTML")
            return
        await state.update_data(admission_price=price)

    await state.set_state(OrganizerEvent.free_kids_question)
    await message.answer("Есть ли бесплатный вход детям до <code>N</code>?", parse_mode="HTML", reply_markup=yes_no_kb("org_free_kids:yes", "org_free_kids:no"))


@router.callback_query(F.data == "org_free_kids:no", OrganizerEvent.free_kids_question)
async def free_kids_no(callback: CallbackQuery, state: FSMContext):
    await state.update_data(free_kids_upto_age=None)
    await callback.answer()
    await _finish_pricing_and_go_photos(callback.message, state)


@router.callback_query(F.data == "org_free_kids:yes", OrganizerEvent.free_kids_question)
async def free_kids_yes(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrganizerEvent.free_kids_age)
    await callback.message.answer("Введите возраст (0..18), например <code>6</code>:", parse_mode="HTML")
    await callback.answer()


@router.message(OrganizerEvent.free_kids_age)
async def free_kids_age(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        age = int(raw)
        if age < 0 or age > 18:
            raise ValueError
    except Exception:
        await message.answer("Нужно число от 0 до 18. Пример: <code>6</code>", parse_mode="HTML")
        return
    await state.update_data(free_kids_upto_age=age)
    await _finish_pricing_and_go_photos(message, state)


async def _finish_pricing_and_go_photos(message: Message, state: FSMContext):
    data = await state.get_data()

    placement_info = None
    try:
        if data.get("period_start") and data.get("period_end"):
            ps = ddate.fromisoformat(data["period_start"])
            pe = ddate.fromisoformat(data["period_end"])
            placement_info = calculate_price(data["category"], start_date=ps, end_date=pe)
        else:
            placement_info = calculate_price(data["category"], num_posts=1)
    except PricingError as e:
        placement_info = {"error": str(e)}

    await state.update_data(placement=placement_info)
    await state.update_data(photo_file_ids=[])
    await state.set_state(OrganizerEvent.photos)

    await message.answer(
        "🖼 Добавь до <b>5 фото</b> (афиша/логотип/фото мероприятия).\n\n"
        "Отправляй фото сообщениями (по одному или несколько подряд).\n"
        "Когда закончишь — нажми <b>«Готово»</b>.\n"
        "Если фото нет — нажми <b>«Пропустить»</b>.",
        parse_mode="HTML",
        reply_markup=photos_kb(),
    )


@router.message(OrganizerEvent.photos, F.photo)
async def organizer_photos_add(message: Message, state: FSMContext):
    data = await state.get_data()
    photo_ids = list(data.get("photo_file_ids") or [])
    if len(photo_ids) >= 5:
        await message.answer("⚠️ Уже загружено 5 фото. Нажми «Готово» или «Пропустить».", reply_markup=photos_kb())
        return

    file_id = message.photo[-1].file_id
    photo_ids.append(file_id)
    await state.update_data(photo_file_ids=photo_ids)

    await message.answer(f"✅ Фото добавлено ({len(photo_ids)}/5).", reply_markup=photos_kb())


@router.message(OrganizerEvent.photos)
async def organizer_photos_text_guard(message: Message):
    await message.answer("Отправь фото (как изображение) или нажми «Готово/Пропустить».", reply_markup=photos_kb())


@router.callback_query(F.data == "org_photos:skip", OrganizerEvent.photos)
async def organizer_photos_skip(callback: CallbackQuery, state: FSMContext):
    await state.update_data(photo_file_ids=[])
    await state.set_state(OrganizerEvent.confirm)
    await _build_and_send_preview(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "org_photos:done", OrganizerEvent.photos)
async def organizer_photos_done(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrganizerEvent.confirm)
    await _build_and_send_preview(callback.message, state)
    await callback.answer()


async def _build_and_send_preview(message: Message, state: FSMContext):
    data = await state.get_data()

    preview = (
        f"🧾 <b>Черновик события</b>\n\n"
        f"🏙 Город: <b>{h(data.get('city_name'))}</b>\n"
        f"🏷 Категория: <b>{h(_format_category_ru(data.get('category')))}</b>\n"
        f"🎫 Название: <b>{h(data.get('title'))}</b>\n"
        f"📅 Даты: <b>{h(_format_period_or_date(data))}</b>\n"
        f"🕒 Время: <b>{h(data.get('time_start'))}-{h(data.get('time_end'))}</b>\n"
        f"📍 Место: <b>{h(data.get('location'))}</b>\n"
        f"☎️ Контакты: <b>{h(data.get('contact'))}</b>\n"
        f"💳 Цена: <b>{h(_format_admission_price(data))}</b>\n"
        f"🧒 Бесплатно детям: <b>{h(_format_free_kids(data))}</b>\n"
        f"📦 Размещение: <b>{h(_format_placement_short(data.get('placement')))}</b>\n\n"
        f"📝 Описание:\n{h(compact(data.get('description')) or '—')}"
    )

    photo_ids = data.get("photo_file_ids") or []
    if photo_ids:
        await message.answer_photo(
            photo=photo_ids[0],
            caption=preview,
            parse_mode="HTML",
            reply_markup=confirm_kb(),
        )
    else:
        await message.answer(preview, parse_mode="HTML", reply_markup=confirm_kb())


@router.callback_query(F.data.startswith("org_confirm:"), OrganizerEvent.confirm)
async def organizer_confirm(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]

    if action == "no":
        await state.clear()
        await callback.message.answer("❌ Отменено. Можно начать заново: нажми «Организатор».")
        await callback.answer()
        return

    data = await state.get_data()
    tg_user = callback.from_user

    city_slug = data["city_slug"]
    title = data["title"]
    description = data["description"]
    location = data["location"]
    contact = data["contact"]
    category_code = data["category"]
    category_enum = EventCategory(category_code)

    free_kids_upto_age = data.get("free_kids_upto_age")
    admission_price = data.get("admission_price")  # float или dict
    admission_price_json = None
    price_admission = None

    if isinstance(admission_price, dict):
        admission_price_json = json.dumps(admission_price, ensure_ascii=False)
        price_admission = None
    else:
        try:
            price_admission = float(admission_price) if admission_price is not None else None
        except Exception:
            price_admission = None

    event_date = data.get("event_date")
    period_start = data.get("period_start")
    period_end = data.get("period_end")
    time_start = data.get("time_start")
    time_end = data.get("time_end")

    placement = data.get("placement") or {}

    photo_ids = data.get("photo_file_ids") or []

    async with get_db() as db:
        # upsert user
        user = (await db.execute(select(User).where(User.telegram_id == tg_user.id))).scalar_one_or_none()
        if not user:
            user = User(
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                role=UserRole.ORGANIZER,
                city_slug=city_slug,
            )
            db.add(user)
        else:
            user.username = tg_user.username
            user.first_name = tg_user.first_name
            user.last_name = tg_user.last_name
            user.role = UserRole.ORGANIZER
            user.city_slug = city_slug

        ev = Event(
            user_id=tg_user.id,
            city_slug=city_slug,
            title=title,
            category=category_enum,
            description=description,
            contact_phone=contact,  # пока кладём всё сюда (тел/ник/ссылка)
            contact_email=None,
            location=location,
            price_admission=price_admission,
            admission_price_json=admission_price_json,
            free_kids_upto_age=free_kids_upto_age,
            reject_reason=None,
            # daily date/time
            event_date=ddate.fromisoformat(event_date) if event_date else None,
            event_time_start=datetime.strptime(time_start, "%H:%M").time() if time_start else None,
            event_time_end=datetime.strptime(time_end, "%H:%M").time() if time_end else None,
            # period date/time (выставка)
            period_start=ddate.fromisoformat(period_start) if period_start else None,
            period_end=ddate.fromisoformat(period_end) if period_end else None,
            working_hours_start=datetime.strptime(time_start, "%H:%M").time() if time_start else None,
            working_hours_end=datetime.strptime(time_end, "%H:%M").time() if time_end else None,
            status=EventStatus.PENDING_MODERATION,
            payment_status=PaymentStatus.PENDING,
        )

        db.add(ev)
        await db.flush()  # получить ev.id

        # NEW: save photos
        for idx, fid in enumerate(photo_ids[:5], start=1):
            db.add(EventPhoto(event_id=ev.id, file_id=fid, position=idx))

        user_from = f"@{tg_user.username}" if tg_user.username else str(tg_user.id)

        admin_text = (
            f"🛡️ <b>Новая заявка</b> <code>{ev.id}</code>\n"
            f"От: {h(user_from)}\n"
            f"Город: {h(CITIES.get(city_slug, {}).get('name', city_slug))} ({h(city_slug)})\n"
            f"Категория: {h(_format_category_ru(category_code))}\n"
            f"Название: {h(title)}\n"
            f"Дата/период: {h(_format_period_or_date(data))}\n"
            f"Время: {h(time_start)} - {h(time_end)}\n"
            f"Место: {h(location)}\n"
            f"Контакты: {h(contact)}\n"
            f"Цена: {h(_format_admission_price(data))}\n"
            f"Бесплатно детям: {h(_format_free_kids(data))}\n"
            f"Размещение: {h(_format_placement_short(placement))}\n"
            f"Фото: {len(photo_ids)} шт.\n\n"
            f"Описание:\n{h(compact(description) or '—')}"
        )

        for admin_id in ADMIN_IDS:
            try:
                if photo_ids:
                    await callback.bot.send_photo(
                        admin_id,
                        photo=photo_ids[0],
                        caption=admin_text,
                        parse_mode="HTML",
                        reply_markup=moderation_kb(ev.id),
                    )
                else:
                    await callback.bot.send_message(
                        admin_id,
                        admin_text,
                        parse_mode="HTML",
                        reply_markup=moderation_kb(ev.id),
                    )
            except Exception:
                pass

    await state.clear()
    await callback.message.answer("✅ Заявка отправлена на модерацию. Ожидай подтверждения.")
    await callback.answer()
