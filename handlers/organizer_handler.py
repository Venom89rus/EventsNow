import html
import json
from datetime import datetime, date as ddate
from sqlalchemy import select, delete

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

from config import ADMIN_IDS, CITIES, DEFAULT_CITY, PRICING_CONFIG
from services.payment_service import calculate_price, PricingError
from services.stats_service import get_global_user_stats
from services.user_activity import touch_user

from database.session import get_db
from database.models import User, UserRole, Event, EventCategory, EventStatus, PaymentStatus
from database.models import EventPhoto  # +++

router = Router()

DESC_PREVIEW_LEN = 140


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

def _get_any(obj, *names, default=None):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default

def _set_any(obj, value, *names):
    for n in names:
        if hasattr(obj, n):
            setattr(obj, n, value)
            return True
    return False

def _col_name(model_cls, *names):
    for n in names:
        if hasattr(model_cls, n):
            return n
    return None


CATEGORY_LABELS_RU = {
    "EXHIBITION": "🖼 Выставка",
    "MASTERCLASS": "🧑🏫 Мастер-класс",
    "CONCERT": "🎤 Концерт",
    "PERFORMANCE": "🎭 Спектакль",
    "LECTURE": "🎓 Лекция/семинар",
    "OTHER": "✨ Другое",
}

PRICE_TIER_PRESETS = {
    "one": ["все"],
    "child_adult": ["дети", "взрослые"],
    "full": ["дети", "студенты", "взрослые", "пенсионеры"],
}


def _format_category_ru(code: str) -> str:
    return CATEGORY_LABELS_RU.get(code, code)

def build_pricing_text() -> str:
    lines = [
        "<b>Прайс на размещение</b>",
        "Минимальная стоимость по категориям:",
        "",
    ]

    order = ["EXHIBITION", "MASTERCLASS", "CONCERT", "PERFORMANCE", "LECTURE", "OTHER"]

    for code in order:
        cfg = PRICING_CONFIG.get(code)
        if not cfg:
            continue

        name = cfg.get("name") or _format_category_ru(code)
        model = (cfg.get("model") or "").lower()
        packages = cfg.get("packages") or {}

        if not packages:
            lines.append(f"• <b>{name}</b> — цены уточняются")
            continue

        min_price = min(packages.values())
        unit = "за пост" if model == "daily" else "за период"
        lines.append(f"• <b>{name}</b> — от {int(min_price)} ₽ ({unit})")

    lines += [
        "",
        "Оплата появляется после модерации: событие одобрят → появится кнопка оплаты публикации.",
    ]
    return "\n".join(lines)


def _format_period_or_date(data: dict) -> str:
    if data.get("event_date"):
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
        return "—"
    return f"до {age} лет"


def _format_admission_price(data: dict) -> str:
    ap = data.get("admission_price")
    if ap is None:
        return "—"

    if isinstance(ap, (int, float)):
        v = float(ap)
        s = str(int(v)) if v.is_integer() else str(v)
        if data.get("category") == "CONCERT":
            return f"от {s} ₽"
        return f"{s} ₽"

    if isinstance(ap, dict):
        parts = []
        for k, v in ap.items():
            try:
                fv = float(v)
                sv = str(int(fv)) if fv.is_integer() else str(fv)
            except Exception:
                sv = str(v)
            parts.append(f"{k}={sv}")
        return ", ".join(parts) if parts else "—"

    return str(ap)


def _format_placement_short(placement: dict | None) -> str:
    if not placement:
        return "—"
    if placement.get("error"):
        return f"⚠️ {placement['error']}"
    package = placement.get("package_name") or placement.get("packagename") or placement.get("package") or "—"
    model = placement.get("model") or "—"
    total = placement.get("total_price") or placement.get("totalprice") or placement.get("price") or "—"
    return f"{package} • {model} • {total} ₽"


def _parse_tier_prices(text: str, allowed_keys: list[str]) -> dict:
    raw = text.replace(";", ",").strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("empty")

    out = {}
    for p in parts:
        if "=" not in p:
            raise ValueError("no_eq")
        k, v = p.split("=", 1)
        k = k.strip().lower()
        v = v.strip().replace(",", ".")
        if k not in allowed_keys:
            raise ValueError("bad_key")
        price = float(v)
        if price < 0:
            raise ValueError("neg")
        out[k] = price

    for k in allowed_keys:
        if k not in out:
            raise ValueError("missing")
    return out


# -------- Keyboards --------
def organizer_city_choice_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Ноябрьск"), KeyboardButton(text="🏙 Муравленко")],
            [KeyboardButton(text="🏙 Губкинский"), KeyboardButton(text="🏙 Новый Уренгой")],
            [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="Прайс")],
        ],
        resize_keyboard=True,
    )


def main_menu_kb() -> ReplyKeyboardMarkup:
    # Главное меню (без импорта из start_handler/resident_handler -> нет циклических импортов)
    # Кнопка "🔧 Админ" будет видна всем, но доступ отфильтруется в admin_handler по ADMIN_IDS.
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Житель"), KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="📞 Обратная связь"), KeyboardButton(text="🔧 Админ")],
        ],
        resize_keyboard=True,
    )


def organizer_menu_kb() -> ReplyKeyboardMarkup:
    # Требование: "⬅️ Назад" и "📊 Статистика" в одной строке, статистика справа
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="⬅️ Назад"), KeyboardButton(text="📈 Активность")],
        ],
        resize_keyboard=True,
    )

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
    kb.button(text="🧑🏫 Мастер-класс", callback_data="org_cat:MASTERCLASS")
    kb.button(text="🎤 Концерт", callback_data="org_cat:CONCERT")
    kb.button(text="🎭 Спектакль", callback_data="org_cat:PERFORMANCE")
    kb.button(text="🎓 Лекция/семинар", callback_data="org_cat:LECTURE")
    kb.button(text="✨ Другое", callback_data="org_cat:OTHER")
    kb.adjust(2)
    return kb.as_markup()

def organizer_categories_choice_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🖼 Выставка"), KeyboardButton(text="🧑‍🏫🏛 Мастер-класс")],
            [KeyboardButton(text="🎤 Концерт"), KeyboardButton(text="🎭 Спектакль")],
            [KeyboardButton(text="🎓 Лекция/семинар"), KeyboardButton(text="✨ Другое")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )

def yes_no_kb(yes_cb: str, no_cb: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Да", callback_data=yes_cb)
    kb.button(text="❌ Нет", callback_data=no_cb)
    kb.adjust(2)
    return kb.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить", callback_data="org_confirm:yes")
    kb.button(text="❌ Отмена", callback_data="org_confirm:no")
    kb.adjust(2)
    return kb.as_markup()


def price_mode_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="1) Одна цена", callback_data="org_price_mode:one")
    kb.button(text="2) Дети/взрослые", callback_data="org_price_mode:child_adult")
    kb.button(text="3) 4 категории", callback_data="org_price_mode:full")
    kb.adjust(1)
    return kb.as_markup()


def moderation_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"adm_ok:{event_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm_no:{event_id}")
    kb.button(text="📄 Подробнее", callback_data=f"adm_view:{event_id}")
    kb.adjust(2, 1)
    return kb.as_markup()

@router.callback_query(F.data.startswith("org_fix:"))
async def organizer_fix_and_resubmit(callback: CallbackQuery):
    # --- helpers (если они уже есть в файле в другом месте — оставь только одну копию) ---
    def _get_any(obj, *names, default=None):
        for n in names:
            if hasattr(obj, n):
                return getattr(obj, n)
        return default

    def _set_any(obj, value, *names):
        for n in names:
            if hasattr(obj, n):
                setattr(obj, n, value)
                return True
        return False

    def _col_name(model_cls, *names):
        for n in names:
            if hasattr(model_cls, n):
                return n
        return None

    try:
        old_event_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    tg_user = callback.from_user
    if not tg_user:
        await callback.answer("Не удалось определить пользователя.", show_alert=True)
        return

    # --- динамически определяем реальные имена колонок ---
    event_user_field = _col_name(Event, "user_id", "userid")
    event_status_field = _col_name(Event, "status")
    event_reject_field = _col_name(Event, "reject_reason", "rejectreason")
    event_payment_field = _col_name(Event, "payment_status", "paymentstatus")

    photo_event_field = _col_name(EventPhoto, "event_id", "eventid")
    photo_file_field = _col_name(EventPhoto, "file_id", "fileid")
    photo_pos_field = _col_name(EventPhoto, "position")

    if not all([event_user_field, event_status_field, event_reject_field, event_payment_field]):
        await callback.answer("Модель Event не соответствует ожиданиям.", show_alert=True)
        return
    if not all([photo_event_field, photo_file_field, photo_pos_field]):
        await callback.answer("Модель EventPhoto не соответствует ожиданиям.", show_alert=True)
        return

    async with get_db() as db:
        old_event = (await db.execute(select(Event).where(Event.id == old_event_id))).scalar_one_or_none()
        if not old_event:
            await callback.answer("Заявка не найдена.", show_alert=True)
            return

        if getattr(old_event, event_user_field, None) != tg_user.id:
            await callback.answer("Это событие принадлежит другому пользователю.", show_alert=True)
            return

        if getattr(old_event, event_status_field) != EventStatus.REJECTED:
            await callback.answer("Эту заявку нельзя переотправить (статус не REJECTED).", show_alert=True)
            return

        # --- 1) новая заявка-копия ---
        new_event = Event()

        # копируем поля безопасно (оба варианта нейминга)
        for (src_names, dst_names) in [
            (("user_id", "userid"), ("user_id", "userid")),
            (("city_slug", "cityslug"), ("city_slug", "cityslug")),
            (("title",), ("title",)),
            (("category",), ("category",)),
            (("description",), ("description",)),
            (("contact_phone", "contactphone"), ("contact_phone", "contactphone")),
            (("contact_email", "contactemail"), ("contact_email", "contactemail")),
            (("location",), ("location",)),

            (("price_admission", "priceadmission"), ("price_admission", "priceadmission")),
            (("admission_price_json", "admissionpricejson"), ("admission_price_json", "admissionpricejson")),
            (("free_kids_upto_age", "freekidsuptoage"), ("free_kids_upto_age", "freekidsuptoage")),

            (("event_date", "eventdate"), ("event_date", "eventdate")),
            (("event_time_start", "eventtimestart"), ("event_time_start", "eventtimestart")),
            (("event_time_end", "eventtimeend"), ("event_time_end", "eventtimeend")),

            (("period_start", "periodstart"), ("period_start", "periodstart")),
            (("period_end", "periodend"), ("period_end", "periodend")),
            (("working_hours_start", "workinghoursstart"), ("working_hours_start", "workinghoursstart")),
            (("working_hours_end", "workinghoursend"), ("working_hours_end", "workinghoursend")),
        ]:
            _set_any(new_event, _get_any(old_event, *src_names, default=None), *dst_names)

        setattr(new_event, event_status_field, EventStatus.PENDING_MODERATION)
        setattr(new_event, event_payment_field, PaymentStatus.PENDING)
        setattr(new_event, event_reject_field, None)

        db.add(new_event)
        await db.flush()
        new_event_id = int(new_event.id)

        # --- 2) фото ---
        photo_event_col = getattr(EventPhoto, photo_event_field)
        photo_pos_col = getattr(EventPhoto, photo_pos_field)

        old_photos = (
            await db.execute(
                select(EventPhoto)
                .where(photo_event_col == old_event_id)
                .order_by(photo_pos_col.asc())
            )
        ).scalars().all()

        await db.execute(delete(EventPhoto).where(photo_event_col == new_event_id))
        await db.flush()

        for idx, p in enumerate(old_photos[:5], start=1):
            np = EventPhoto()
            setattr(np, photo_event_field, new_event_id)
            setattr(np, photo_file_field, getattr(p, photo_file_field))
            setattr(np, photo_pos_field, idx)
            db.add(np)

    # --- 3) уведомления ---
    old_reason = _get_any(old_event, "reject_reason", "rejectreason", default=None)
    if old_reason:
        await callback.message.answer(
            f"✅ Создана копия заявки (ID: {new_event_id}) и отправлена на модерацию.\n"
            f"Причина прошлого отказа: {h(old_reason)}",
            parse_mode="HTML",
            reply_markup=organizer_menu_kb(),
        )
    else:
        await callback.message.answer(
            f"✅ Создана копия заявки (ID: {new_event_id}) и отправлена на модерацию.",
            parse_mode="HTML",
            reply_markup=organizer_menu_kb(),
        )

    async with get_db() as db:
        first_photo = (
            await db.execute(
                select(EventPhoto)
                .where(getattr(EventPhoto, photo_event_field) == new_event_id)
                .order_by(getattr(EventPhoto, photo_pos_field).asc())
            )
        ).scalars().first()

    admin_text = f"🆕 Повторная заявка (копия отклонённой)\nID: {new_event_id}"
    for admin_id in ADMIN_IDS:
        try:
            if first_photo:
                await callback.bot.send_photo(
                    admin_id,
                    photo=getattr(first_photo, photo_file_field),
                    caption=admin_text,
                    reply_markup=moderation_kb(new_event_id),
                )
            else:
                await callback.bot.send_message(
                    admin_id,
                    admin_text,
                    reply_markup=moderation_kb(new_event_id),
                )
        except Exception:
            pass

    await callback.answer()



def photos_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Готово", callback_data="org_photos:done")
    kb.button(text="↩️ Удалить последнюю", callback_data="org_photos:pop")
    kb.button(text="❌ Пропустить", callback_data="org_photos:skip")
    kb.adjust(1, 1, 1)
    return kb.as_markup()

# -------- States --------

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
    photos = State()
    confirm = State()


# -------- Menu actions --------
def build_pricing_text() -> str:
    lines = [
        "<b>Прайс на размещение</b>",
        "Минимальная стоимость по категориям:",
        "",
    ]
    order = ["EXHIBITION", "MASTERCLASS", "CONCERT", "PERFORMANCE", "LECTURE", "OTHER"]

    for code in order:
        cfg = PRICING_CONFIG.get(code) or {}
        name = cfg.get("name") or _format_category_ru(code)
        packages = cfg.get("packages") or {}
        if not packages:
            continue
        min_price = min(packages.values())
        lines.append(f"• <b>{name}</b> — от {int(min_price)} ₽")

    lines.append("")
    lines.append("Чтобы продолжить, выбери город и создай событие.")
    return "\n".join(lines)


@router.message(F.text == "⬅️ Назад")
async def organizer_back_message(message: Message, state: FSMContext):
    # --- GUARD: не перехватываем админский "Назад" ---
    st = await state.get_state()
    if message.from_user and (message.from_user.id in ADMIN_IDS) and st and ("AdminState" in st):
        return

    await state.clear()
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(F.text == "📈 Активность")
async def organizer_activity_message(message: Message, state: FSMContext):
    # --- GUARD: не перехватываем админскую статистику ---
    st = await state.get_state()
    if message.from_user and (message.from_user.id in ADMIN_IDS) and st and ("AdminState" in st):
        return

    # --- дальше твоя текущая фича статистики организатора (оставляем смысл) ---
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    s = await get_global_user_stats()
    text = (
        "<b>📈 Активность</b>\n\n"
        f"👥 Всего пользователей: <b>{s.get('total_users', 0)}</b>\n"
        f"🆕 Новых за сегодня: <b>{s.get('new_today', 0)}</b>\n"
        f"✅ Активных за 7 дней: <b>{s.get('active_7d', 0)}</b>\n"
        f"✅ Активных за 30 дней: <b>{s.get('active_30d', 0)}</b>\n"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=organizer_menu_kb())

@router.message(F.text == "🎪 Организатор")
async def organizer_entry(message: Message, state: FSMContext):
    await state.clear()

    # честная активность: фиксируем вход в ключевой экран
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    await state.set_state(OrganizerEvent.city)
    default_city_name = CITIES.get(DEFAULT_CITY, {}).get("name", DEFAULT_CITY)

    await message.answer(
        f"Город по умолчанию: <b>{h(default_city_name)}</b>\n\n"
        "Выберите город для подачи заявки:",
        reply_markup=organizer_menu_kb(),
        parse_mode="HTML",
    )
    await message.answer(
        "Выбери город:",
        reply_markup=organizer_city_choice_kb(),
        parse_mode="HTML",
    )

@router.message(F.text == "Прайс")
async def organizer_price_message(message: Message, state: FSMContext):
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )
    await message.answer(build_pricing_text(), parse_mode="HTML", reply_markup=organizer_menu_kb())


# -------- Flow --------
ORG_CITY_TEXT_TO_SLUG = {
    "✅ Ноябрьск": "nojabrsk",
    "🏙 Муравленко": "muravlenko",
    "🏙 Губкинский": "gubkinskiy",
    "🏙 Новый Уренгой": "novy_urengoy",
}

@router.message(OrganizerEvent.city, F.text.in_(set(ORG_CITY_TEXT_TO_SLUG.keys())))
async def organizer_choose_city_from_bottom(message: Message, state: FSMContext):
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    slug = ORG_CITY_TEXT_TO_SLUG.get(message.text)
    if not slug:
        await message.answer("Выбери город кнопками ниже.", reply_markup=organizer_city_choice_kb())
        return

    info = CITIES.get(slug)
    if not info:
        await message.answer("Город не найден.", reply_markup=organizer_city_choice_kb())
        return

    # Ноябрьск — рабочий, идём дальше как в callback
    if slug == "nojabrsk":
        await state.update_data(city_slug=slug, city_name=info.get("name"))
        await state.set_state(OrganizerEvent.category)
        await message.answer(
            f"<b>{h(info.get('name'))}</b> выбран!\n"
            f"Выберите вид мероприятия!",
        reply_markup=organizer_categories_choice_kb(),
            parse_mode="HTML",
        )
        return

    # Остальные — заглушка
    await message.answer(
        f"{h(info.get('name'))} — раздел в разработке.",
        reply_markup=organizer_city_choice_kb(),
        parse_mode="HTML",
    )


ORG_CATEGORY_TEXT_TO_CODE = {
    "🖼 Выставка": "EXHIBITION",
    "🧑‍🏫🏛 Мастер-класс": "MASTERCLASS",
    "🎤 Концерт": "CONCERT",
    "🎭 Спектакль": "PERFORMANCE",
    "🎓 Лекция/семинар": "LECTURE",
    "✨ Другое": "OTHER",
}

@router.message(OrganizerEvent.category, F.text.in_(set(ORG_CATEGORY_TEXT_TO_CODE.keys())))
async def organizer_choose_category_from_bottom(message: Message, state: FSMContext):
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    code = ORG_CATEGORY_TEXT_TO_CODE.get(message.text)
    if not code:
        await message.answer("Выбери категорию кнопками ниже.", reply_markup=organizer_categories_choice_kb())
        return

    await state.update_data(category=code)
    await state.set_state(OrganizerEvent.title)
    await message.answer("<b>Название события</b>:", parse_mode="HTML")


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
        await message.answer("Название слишком короткое. Минимум 3 символа.")
        return
    await state.update_data(title=title)
    await state.set_state(OrganizerEvent.description)
    await message.answer("Введите <b>описание</b> (минимум 10 символов):", parse_mode="HTML")


@router.message(OrganizerEvent.description)
async def organizer_description(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if len(desc) < 10:
        await message.answer("Описание слишком короткое. Минимум 10 символов.")
        return
    await state.update_data(description=desc)
    await state.set_state(OrganizerEvent.date_or_period)
    await message.answer(
        "Введите дату/период:\n\n"
        "- Разовое мероприятие: <code>ДД.ММ.ГГГГ</code>\n"
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
            "Неверный формат. Повтори:\n\n<code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</code>",
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
    await message.answer("Введите место проведения (адрес/площадка):", parse_mode="HTML")


@router.message(OrganizerEvent.location)
async def organizer_location(message: Message, state: FSMContext):
    loc = (message.text or "").strip()
    if len(loc) < 3:
        await message.answer("Слишком коротко. Укажи адрес/площадку.")
        return
    await state.update_data(location=loc)
    await state.set_state(OrganizerEvent.contact)
    await message.answer("Контакты (телефон/ник/ссылка текстом):", parse_mode="HTML")


@router.message(OrganizerEvent.contact)
async def organizer_contact(message: Message, state: FSMContext):
    contact = (message.text or "").strip()
    if len(contact) < 3:
        await message.answer("Слишком коротко. Укажи контакты.")
        return

    await state.update_data(contact=contact)

    # ВОЗВРАЩАЕМ ФИЧУ: выбор режима цен (все / дети-взрослые / дети-студенты-взрослые-пенсионеры)
    # Теперь показываем для ЛЮБОЙ категории, а не только EXHIBITION.
    await state.set_state(OrganizerEvent.admission_price_mode)
    await message.answer(
        "Выбери режим цен билетов:",
        reply_markup=price_mode_kb(),
        parse_mode="HTML",
    )


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
        f"Введите цены в формате:\n\n<code>{h(example)}</code>\n"
        f"Допустимые категории: <b>{h(keys_str)}</b>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(OrganizerEvent.admission_price)
async def organizer_admission_price(message: Message, state: FSMContext):
    data = await state.get_data()
    text = (message.text or "").strip()

    # Если был выбор режима цен (1/2/3) — значит ждём tier-цены словарём
    mode = data.get("admission_price_mode")
    if mode in PRICE_TIER_PRESETS:
        keys = PRICE_TIER_PRESETS.get(mode, ["все"])
        try:
            tiers = _parse_tier_prices(text, keys)
        except Exception:
            if mode == "one":
                example = "все=500"
            elif mode == "child_adult":
                example = "дети=200, взрослые=500"
            else:
                example = "дети=200, студенты=300, взрослые=500, пенсионеры=250"

            await message.answer(
                f"Неверный формат. Пример: <code>{h(example)}</code>",
                parse_mode="HTML",
            )
            return

        await state.update_data(admission_price=tiers)

    else:
        # fallback: если режим не выбирали — считаем, что ввели одно число
        t = text.replace(",", ".")
        try:
            price = float(t)
            if price < 0:
                raise ValueError
        except Exception:
            await message.answer(
                "Введите число (например <code>0</code> или <code>1500</code>).",
                parse_mode="HTML",
            )
            return

        # если хочешь сохранить старое правило концертов — оставляем как было
        category = data.get("category")
        if category == "CONCERT" and price != 0 and price < 1000:
            await message.answer(
                "Для концертов действует правило: минимум <code>1000</code>.\n"
                "Если концерт бесплатный — введи <code>0</code>.",
                parse_mode="HTML",
            )
            return

        await state.update_data(admission_price=price)

    await state.set_state(OrganizerEvent.free_kids_question)
    await message.answer(
        "Есть ли бесплатный вход детям до <code>N</code>?",
        parse_mode="HTML",
        reply_markup=yes_no_kb("org_free_kids:yes", "org_free_kids:no"),
    )



@router.callback_query(F.data == "org_free_kids:no", OrganizerEvent.free_kids_question)
async def free_kids_no(callback: CallbackQuery, state: FSMContext):
    await state.update_data(free_kids_upto_age=None)
    await callback.answer()
    await _finish_pricing_and_preview(callback.message, state)


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
    await _finish_pricing_and_preview(message, state)

@router.message(OrganizerEvent.photos)
async def organizer_photos_collect(message: Message, state: FSMContext):
    # принимаем только фото
    if not message.photo:
        await message.answer("Пришли именно фото (как картинку), или нажми «✅ Готово».", reply_markup=photos_kb())
        return

    data = await state.get_data()
    photo_ids: list[str] = list(data.get("photo_file_ids") or [])

    if len(photo_ids) >= 5:
        await message.answer("Уже загружено 5 фото — нажми «✅ Готово».", reply_markup=photos_kb())
        return

    file_id = message.photo[-1].file_id
    photo_ids.append(file_id)
    await state.update_data(photo_file_ids=photo_ids)

    await message.answer(f"✅ Фото добавлено ({len(photo_ids)}/5).", reply_markup=photos_kb())


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
    await state.update_data(photo_file_ids=[])  # +++
    await state.set_state(OrganizerEvent.photos)  # +++
    await message.answer(
        "🖼 Добавь до <b>5</b> фото/афиш/логотипов.\n\n"
        "Отправляй фотки сообщениями (по одной).\n"
        "Когда закончишь — нажми «✅ Готово».\n\n"
        "Можно нажать «❌ Пропустить», если фото нет.",
        parse_mode="HTML",
        reply_markup=photos_kb(),
    )

async def _build_and_send_preview(message: Message, state: FSMContext):
    data = await state.get_data()

    city_slug = data.get("city_slug")
    city_name = data.get("city_name") or CITIES.get(city_slug, {}).get("name", city_slug)

    photo_ids = data.get("photo_file_ids") or []

    preview = (
        "<b>🧾 Черновик заявки</b>\n\n"
        f"🏙 Город: <b>{h(city_name)}</b>\n"
        f"🏷 Категория: <b>{h(_format_category_ru(data.get('category')))}</b>\n"
        f"📝 Название: <b>{h(data.get('title'))}</b>\n"
        f"📅 Дата/период: <b>{h(_format_period_or_date(data))}</b>\n"
        f"⏰ Время: <b>{h(data.get('time_start'))} - {h(data.get('time_end'))}</b>\n"
        f"📍 Место: <b>{h(data.get('location'))}</b>\n"
        f"📞 Контакты: <b>{h(data.get('contact'))}</b>\n"
        f"💳 Цена: <b>{h(_format_admission_price(data))}</b>\n"
        f"🧒 Бесплатно детям: <b>{h(_format_free_kids(data))}</b>\n"
        f"📦 Размещение: <b>{h(_format_placement_short(data.get('placement') or {}))}</b>\n"
        f"🖼 Фото: <b>{len(photo_ids)} шт.</b>\n\n"
        f"📝 Описание:\n{h(compact(data.get('description')) or '—')}"
    )

    # если есть фото — можно показать превью с первой картинкой
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
        await callback.message.answer(
            "❌ Отменено. Можно начать заново: нажми «🎪 Организатор».",
            reply_markup=organizer_menu_kb(),
        )
        await callback.answer()
        return

    data = await state.get_data()
    if data.get("_confirm_in_progress"):
        await callback.answer("Уже отправляется…", show_alert=True)
        return
    await state.update_data(_confirm_in_progress=True)

    tg_user = callback.from_user

    city_slug = data["city_slug"]
    title = data["title"]
    description = data["description"]
    location = data["location"]
    contact = data["contact"]

    category_code = data["category"]
    category_enum = EventCategory(category_code)

    free_kids_upto_age = data.get("free_kids_upto_age")
    admission_price = data.get("admission_price")  # float or dict

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

    photo_ids: list[str] = list(data.get("photo_file_ids") or [])

    # 1) создаём юзера/ивент + сохраняем фото в БД
    async with get_db() as db:
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
            contact_phone=contact,
            contact_email=None,
            location=location,
            price_admission=price_admission,
            event_date=ddate.fromisoformat(event_date) if event_date else None,
            event_time_start=datetime.strptime(time_start, "%H:%M").time() if time_start else None,
            event_time_end=datetime.strptime(time_end, "%H:%M").time() if time_end else None,
            period_start=ddate.fromisoformat(period_start) if period_start else None,
            period_end=ddate.fromisoformat(period_end) if period_end else None,
            working_hours_start=datetime.strptime(time_start, "%H:%M").time() if time_start else None,
            working_hours_end=datetime.strptime(time_end, "%H:%M").time() if time_end else None,
            status=EventStatus.PENDING_MODERATION,
            payment_status=PaymentStatus.PENDING,
        )

        # оставляем совместимость как было (через hasattr)
        if hasattr(ev, "admission_price_json"):
            ev.admission_price_json = admission_price_json
        if hasattr(ev, "free_kids_upto_age"):
            ev.free_kids_upto_age = free_kids_upto_age
        if hasattr(ev, "reject_reason"):
            ev.reject_reason = None

        db.add(ev)
        await db.flush()  # получить ev.id
        event_id = ev.id

        # FIX: делаем вставку фото идемпотентной (не меняя фичи)
        # Если по какой-то причине фотки на этот event_id уже есть — удаляем и вставляем заново.
        await db.execute(delete(EventPhoto).where(EventPhoto.event_id == event_id))
        await db.flush()  # важно: применить DELETE до INSERT-ов

        # сохраняем фото (до 5)
        for i, fid in enumerate(photo_ids[:5], start=1):
            db.add(EventPhoto(event_id=event_id, file_id=fid, position=i))

    # 2) готовим текст админам (вне сессии)
    user_from = f"@{tg_user.username}" if tg_user.username else str(tg_user.id)
    admin_text = (
        f"🛡 <b>На модерацию</b> • <code>{event_id}</code>\n"
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

    # 3) отправляем админам: если есть фото — первой фоткой (caption), иначе текстом
    for admin_id in ADMIN_IDS:
        try:
            if photo_ids:
                await callback.bot.send_photo(
                    admin_id,
                    photo=photo_ids[0],
                    caption=admin_text,
                    parse_mode="HTML",
                    reply_markup=moderation_kb(event_id),
                )
            else:
                await callback.bot.send_message(
                    admin_id,
                    admin_text,
                    parse_mode="HTML",
                    reply_markup=moderation_kb(event_id),
                )
        except Exception:
            pass

    await state.clear()
    await callback.message.answer(
        "✅ Заявка отправлена на модерацию. Ожидай подтверждения.",
        reply_markup=organizer_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "org_photos:pop", OrganizerEvent.photos)
async def organizer_photos_pop(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    photo_ids: list[str] = list(data.get("photo_file_ids") or [])

    if not photo_ids:
        await callback.answer("Фото ещё нет", show_alert=True)
        return

    photo_ids.pop()
    await state.update_data(photo_file_ids=photo_ids)

    await callback.message.answer(f"↩️ Удалено. Сейчас {len(photo_ids)}/5.", reply_markup=photos_kb())
    await callback.answer()


@router.callback_query(F.data.in_({"org_photos:done", "org_photos:skip"}), OrganizerEvent.photos)
async def organizer_photos_done(callback: CallbackQuery, state: FSMContext):
    # показываем превью и переходим в confirm
    await state.set_state(OrganizerEvent.confirm)
    await _build_and_send_preview(callback.message, state)
    await callback.answer()
