import html
import json
from datetime import date as ddate, datetime

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import ADMIN_IDS, CITIES, DEFAULT_CITY
from database.session import get_db
from database.models import User, UserRole, Event, EventCategory, EventStatus, PaymentStatus
from services.payment_service import calculate_price, PricingError

router = Router()

# ---------- display maps ----------
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

    admission_price_mode = State()     # for exhibition
    admission_price = State()          # float or dict tiers

    free_kids_question = State()       # yes/no
    free_kids_age = State()            # N

    confirm = State()


# ---------- helpers ----------
def h(text) -> str:
    return html.escape(str(text)) if text is not None else ""


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def moderation_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Разместить", callback_data=f"adm_ok:{event_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm_no:{event_id}")
    kb.adjust(1)
    return kb.as_markup()


def cities_kb_for_organizer() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for slug, info in sorted(CITIES.items(), key=lambda x: x[1]["name"]):
        emoji = "✅" if info.get("status") == "active" else "⏳"
        kb.button(text=f"{emoji} {info['name']}", callback_data=f"org_city:{slug}")
    kb.adjust(1)
    return kb.as_markup()


def categories_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎨 Выставка", callback_data="org_cat:EXHIBITION")
    kb.button(text="🧑‍🏫 Мастер-класс", callback_data="org_cat:MASTERCLASS")
    kb.button(text="🎸 Концерт", callback_data="org_cat:CONCERT")
    kb.button(text="🎭 Выступление", callback_data="org_cat:PERFORMANCE")
    kb.button(text="🎓 Лекция/семинар", callback_data="org_cat:LECTURE")
    kb.button(text="✨ Другое", callback_data="org_cat:OTHER")
    kb.adjust(1)  # по одной кнопке в ряд
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
    kb.button(text="2) Детский / Взрослый", callback_data="org_price_mode:child_adult")
    kb.button(text="3) Дети / Студенты / Взрослые / Пенсионеры", callback_data="org_price_mode:full")
    kb.adjust(1)
    return kb.as_markup()


def _parse_date(s: str):
    return datetime.strptime(s, "%d.%m.%Y").date()


def _parse_time(s: str):
    return datetime.strptime(s, "%H:%M").time()


def _format_period_or_date(data: dict) -> str:
    if data.get("event_date"):
        return data["event_date"]
    if data.get("period_start") and data.get("period_end"):
        return f"{data['period_start']}-{data['period_end']}"
    return "-"


def _format_category_ru(code: str) -> str:
    return CATEGORY_LABELS_RU.get(code, code)


def _format_placement_short(placement: dict) -> str:
    if not placement:
        return "—"
    if placement.get("error"):
        return f"Ошибка расчёта: {placement.get('error')}"

    package = placement.get("package_name") or placement.get("packagename") or placement.get("package") or "—"
    model = placement.get("model") or "—"
    days = placement.get("num_days") or placement.get("numdays")
    posts = placement.get("num_items") or placement.get("num_posts") or placement.get("numitems") or placement.get("numposts")
    total = placement.get("total_price") or placement.get("totalprice") or placement.get("price")

    details = []
    if model == "period" and days:
        details.append(f"дней: {days}")
    if model == "daily" and posts:
        details.append(f"постов: {posts}")

    details_str = (" • " + " • ".join(details)) if details else ""
    return f"Пакет: {package}{details_str} • К оплате: {total}₽"


def _parse_tier_prices(text: str, allowed_keys: list[str]) -> dict:
    raw = text.replace(";", ",").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("empty")

    out = {}
    for p in parts:
        if "=" not in p:
            raise ValueError("no_equals")
        k, v = p.split("=", 1)
        k = k.strip().lower()
        v = v.strip().replace(",", ".")
        if k not in allowed_keys:
            raise ValueError(f"bad_key:{k}")
        price = float(v)
        if price < 0:
            raise ValueError("neg_price")
        out[k] = price

    for k in allowed_keys:
        if k not in out:
            raise ValueError(f"missing:{k}")

    return out


def _format_admission_price(data: dict) -> str:
    ap = data.get("admission_price")
    if ap is None:
        return "-"
    if isinstance(ap, (int, float)):
        v = float(ap)
        return str(int(v)) if v.is_integer() else str(v)
    if isinstance(ap, dict):
        order = ["все", "дети", "студенты", "взрослые", "пенсионеры"]
        parts = []
        for k in order:
            if k in ap:
                parts.append(f"{k}: {ap[k]}")
        return ", ".join(parts) if parts else str(ap)
    return str(ap)


def _format_free_kids(data: dict) -> str:
    age = data.get("free_kids_upto_age")
    if age is None:
        return "—"
    return f"детям до {age} лет"


def _ticket_price_label(data: dict) -> str:
    return "Стоимость билета от" if data.get("category") == "CONCERT" else "Цена билета"


def _ticket_price_value(data: dict) -> str:
    if data.get("category") == "EXHIBITION":
        return _format_admission_price(data)

    ap = data.get("admission_price")
    if ap is None:
        return "-"

    try:
        v = float(ap)
        return str(int(v)) if v.is_integer() else str(v)
    except Exception:
        return str(ap)


async def _build_and_send_preview(message: Message, state: FSMContext):
    data = await state.get_data()

    preview = (
        "🧾 <b>Черновик мероприятия</b>\n\n"
        f"Город: {h(data['city_name'])}\n"
        f"Категория: {h(_format_category_ru(data['category']))}\n"
        f"Название: {h(data['title'])}\n"
        f"Дата/период: {h(_format_period_or_date(data))}\n"
        f"Время: {h(data['time_start'])} - {h(data['time_end'])}\n"
        f"Место: {h(data['location'])}\n"
        f"Контакты: {h(data['contact'])}\n"
        f"{h(_ticket_price_label(data))}: {h(_ticket_price_value(data))}\n"
        f"Бесплатно: {h(_format_free_kids(data))}\n\n"
        f"Стоимость размещения: {h(_format_placement_short(data.get('placement')))}\n\n"
        "Отправить на модерацию админу?"
    )

    await message.answer(preview, parse_mode="HTML", reply_markup=confirm_kb())


# ---------- entry ----------
@router.message(F.text == "🎪 Организатор")
async def organizer_entry(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(OrganizerEvent.city)

    default_city_name = CITIES.get(DEFAULT_CITY, {}).get("name", DEFAULT_CITY)

    await message.answer(
        "🎪 <b>Организатор</b>\n\n"
        f"Выбери город размещения (по умолчанию: {h(default_city_name)}):",
        reply_markup=cities_kb_for_organizer(),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("org_city:"), OrganizerEvent.city)
async def organizer_city(callback: CallbackQuery, state: FSMContext):
    slug = callback.data.split(":")[1]
    info = CITIES.get(slug)
    if not info:
        await callback.answer("Город не найден", show_alert=True)
        return

    await state.update_data(city_slug=slug, city_name=info["name"])
    await state.set_state(OrganizerEvent.category)

    await callback.message.answer(
        f"Город: <b>{h(info['name'])}</b>\n\nВыбери категорию мероприятия:",
        reply_markup=categories_kb(),
        parse_mode="HTML",
    )
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
        await message.answer("Название слишком короткое. Введите ещё раз.")
        return

    await state.update_data(title=title)
    await state.set_state(OrganizerEvent.description)
    await message.answer("Введите <b>описание</b> мероприятия (до 2000 символов):", parse_mode="HTML")


@router.message(OrganizerEvent.description)
async def organizer_description(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if len(desc) < 10:
        await message.answer("Описание слишком короткое. Введите ещё раз.")
        return

    await state.update_data(description=desc)
    await state.set_state(OrganizerEvent.date_or_period)

    await message.answer(
        "Введите дату/период:\n"
        "- Разовое событие: <code>ДД.ММ.ГГГГ</code>\n"
        "- Выставка периодом: <code>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</code>\n\n"
        "Пример: <code>10.01.2026</code> или <code>10.01.2026-17.01.2026</code>",
        parse_mode="HTML",
    )


@router.message(OrganizerEvent.date_or_period)
async def organizer_date_or_period(message: Message, state: FSMContext):
    text = (message.text or "").strip()

    # Период разрешаем только для EXHIBITION
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
    await message.answer("Введите <b>время начала</b> <code>ЧЧ:ММ</code> (например <code>10:00</code>):", parse_mode="HTML")


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
    await message.answer("Введите <b>время окончания</b> <code>ЧЧ:ММ</code> (например <code>20:00</code>):", parse_mode="HTML")


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
    await message.answer("Введите <b>место проведения</b> (адрес/площадка):", parse_mode="HTML")


@router.message(OrganizerEvent.location)
async def organizer_location(message: Message, state: FSMContext):
    loc = (message.text or "").strip()
    if len(loc) < 3:
        await message.answer("Слишком коротко. Введите место ещё раз.")
        return

    await state.update_data(location=loc)
    await state.set_state(OrganizerEvent.contact)
    await message.answer("Введите <b>контакты</b> организатора (телефон/ник/ссылка):", parse_mode="HTML")


@router.message(OrganizerEvent.contact)
async def organizer_contact(message: Message, state: FSMContext):
    contact = (message.text or "").strip()
    if len(contact) < 3:
        await message.answer("Слишком коротко. Введите контакты ещё раз.")
        return

    await state.update_data(contact=contact)

    data = await state.get_data()
    if data.get("category") == "EXHIBITION":
        await state.set_state(OrganizerEvent.admission_price_mode)
        await message.answer(
            "🎟️ Для выставок часто разные цены по возрастам.\n\n"
            "Выбери вариант заполнения цен:",
            reply_markup=price_mode_kb(),
        )
    else:
        await state.set_state(OrganizerEvent.admission_price)
        await message.answer("Введите стоимость билета (число) или <code>0</code> если бесплатно:", parse_mode="HTML")


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
        f"Допустимые категории: {h(keys_str)}",
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
    await message.answer(
        "Есть ли бесплатный вход детям до <b>N</b> лет?",
        parse_mode="HTML",
        reply_markup=yes_no_kb("org_freekids:yes", "org_freekids:no"),
    )


@router.callback_query(F.data == "org_freekids:no", OrganizerEvent.free_kids_question)
async def freekids_no(callback: CallbackQuery, state: FSMContext):
    await state.update_data(free_kids_upto_age=None)
    await callback.answer()
    await _finish_pricing_and_preview(callback.message, state)


@router.callback_query(F.data == "org_freekids:yes", OrganizerEvent.free_kids_question)
async def freekids_yes(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrganizerEvent.free_kids_age)
    await callback.message.answer("Укажи N (возраст), например: <code>6</code>", parse_mode="HTML")
    await callback.answer()


@router.message(OrganizerEvent.free_kids_age)
async def freekids_age(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        age = int(raw)
        if age < 0 or age > 18:
            raise ValueError
    except Exception:
        await message.answer("Нужно число от 0 до 18. Пример: <code>6</code>", parse_mode="HTML")
        return

    await state.update_data(free_kids_upto_age=age)
    await _finish_pricing_and_preview(message, state)


async def _finish_pricing_and_preview(message: Message, state: FSMContext):
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
    await state.set_state(OrganizerEvent.confirm)

    await _build_and_send_preview(message, state)


@router.callback_query(F.data.startswith("org_confirm:"), OrganizerEvent.confirm)
async def organizer_confirm(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]

    if action == "no":
        await state.clear()
        await callback.message.answer("❌ Отменено. Можно начать заново: нажми «Организатор».")
        await callback.answer()
        return

    # action == yes -> сохраняем в БД и отправляем на модерацию
    data = await state.get_data()
    tg_user = callback.from_user

    # подготовка полей
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

    # время/даты
    event_date = data.get("event_date")
    period_start = data.get("period_start")
    period_end = data.get("period_end")

    time_start = data.get("time_start")
    time_end = data.get("time_end")

    # placement
    placement = data.get("placement") or {}
    placement_total = placement.get("total_price") or placement.get("totalprice") or placement.get("price")
    placement_package = placement.get("package_name") or placement.get("packagename") or placement.get("package")

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
            # обновим минимальные данные
            user.username = tg_user.username
            user.first_name = tg_user.first_name
            user.last_name = tg_user.last_name
            user.role = UserRole.ORGANIZER
            user.city_slug = city_slug

        # create event
        ev = Event(
            user_id=tg_user.id,
            city_slug=city_slug,
            title=title,
            category=category_enum,
            description=description,
            contact_phone=contact,      # пока кладём всё сюда (тел/ник/ссылка)
            contact_email=None,
            location=location,
            price_admission=price_admission,

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

        # дополнительные поля (должны быть добавлены тобой в models.py)
        if hasattr(ev, "admission_price_json"):
            ev.admission_price_json = admission_price_json
        if hasattr(ev, "free_kids_upto_age"):
            ev.free_kids_upto_age = free_kids_upto_age
        if hasattr(ev, "reject_reason"):
            ev.reject_reason = None

        # можно также сохранить placement в event, если добавишь поля позже
        # сейчас достаточно показать это админу/организатору из текста

        db.add(ev)
        await db.flush()  # получить ev.id

        # Формируем текст админу
        user_from = f"@{tg_user.username}" if tg_user.username else str(tg_user.id)

        admin_text = (
            "🛡️ <b>МОДЕРАЦИЯ: новая заявка</b>\n\n"
            f"ID заявки: <code>{ev.id}</code>\n"
            f"От: {h(user_from)}\n"
            f"Город: {h(CITIES.get(city_slug, {}).get('name', city_slug))} ({h(city_slug)})\n"
            f"Категория: {h(_format_category_ru(category_code))}\n"
            f"Название: {h(title)}\n"
            f"Описание: {h(description)}\n"
            f"Дата/период: {h(_format_period_or_date(data))}\n"
            f"Время: {h(time_start)} - {h(time_end)}\n"
            f"Место: {h(location)}\n"
            f"Контакты: {h(contact)}\n"
            f"{h(_ticket_price_label(data))}: {h(_ticket_price_value(data))}\n"
            f"Бесплатно: {h(_format_free_kids(data))}\n"
            f"Размещение: {h(_format_placement_short(placement))}\n"
        )

        # Отправляем всем админам
        for admin_id in ADMIN_IDS:
            try:
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
