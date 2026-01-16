import html
import logging
import asyncio
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from services.yookassa_service import create_payment
from config import PRICING_CONFIG

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, desc, func

from config import ADMIN_IDS, ADMINIDS, PAYMENTS_REAL_ENABLED, PUBLIC_BASE_URL
from config import PUBLIC_BASE_URL, YOOKASSA_RETURN_URL

from services.yookassa_service import create_payment
from services.payment_service import calculate_price, PricingError

from database.session import get_db
from database.models import (
    User,
    Event,
    EventStatus,
    Payment,
    PaymentStatus,
    PricingModel,
    EventCategory
)
from services.stats_service import get_global_user_stats
from services.user_activity import touch_user
from services.notify_service import notify_new_event_published

router = Router()
logger = logging.getLogger("eventsnow")

DESC_PREVIEW_LEN = 120
USERS_PAGE_SIZE = 10


def h(x) -> str:
    """HTML escape"""
    return html.escape(str(x)) if x is not None else ""


def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь админом"""
    return user_id in (ADMIN_IDS or [])


def compact(text: str | None) -> str:
    """Убрать лишние пробелы"""
    if not text:
        return ""
    return " ".join(text.split())


def short(text: str | None, limit: int = DESC_PREVIEW_LEN) -> str:
    """Обрезать текст до N символов"""
    t = compact(text)
    if not t:
        return "—"
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Главное меню"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Житель"), KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="✍️ Обратная связь"), KeyboardButton(text="🔧 Админ")],
        ],
        resize_keyboard=True,
    )


def admin_panel_kb() -> ReplyKeyboardMarkup:
    """Панель админа"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗂 События на модерацию"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="💰 Финансы")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


class AdminState(StatesGroup):
    """Состояния админа"""
    panel = State()


class AdminReject(StatesGroup):
    """Отклонение события"""
    waiting_reason = State()


async def _touch_from_message(message: Message) -> None:
    """Зафиксировать пользователя"""
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )


def fmt_when(e: Event) -> str:
    """Форматировать дату/время события"""
    if getattr(e, "event_date", None):
        ds = e.event_date.strftime("%d.%m.%Y")
        ts = e.event_time_start.strftime("%H:%M") if e.event_time_start else "—"
        te = e.event_time_end.strftime("%H:%M") if e.event_time_end else "—"
        return f"{ds} • {ts}-{te}"

    if getattr(e, "period_start", None) and getattr(e, "period_end", None):
        ps = e.period_start.strftime("%d.%m.%Y")
        pe = e.period_end.strftime("%d.%m.%Y")
        ts = e.working_hours_start.strftime("%H:%M") if e.working_hours_start else "—"
        te = e.working_hours_end.strftime("%H:%M") if e.working_hours_end else "—"
        return f"{ps}-{pe} • {ts}-{te}"

    return "—"


def fmt_price(e: Event) -> str:
    """Форматировать цену события"""
    price = getattr(e, "price_admission", None)
    if price is None:
        return "—"

    try:
        v = float(price)
        s = str(int(v)) if v.is_integer() else str(v)
    except Exception:
        s = str(price)

    return f"{s} ₽"


def fmt_status(e: Event) -> str:
    """Форматировать статус события"""
    mapping = {
        EventStatus.DRAFT: "⚪ draft",
        EventStatus.PENDING_MODERATION: "🟡 на модерации",
        EventStatus.APPROVED_WAITING_PAYMENT: "🟠 одобрено, ждём оплату",
        EventStatus.ACTIVE: "🟢 опубликовано",
        EventStatus.ARCHIVED: "⚫ архив",
        EventStatus.REJECTED: "🔴 отклонено",
    }
    return mapping.get(e.status, str(e.status))


def moderation_kb(event_id: int) -> InlineKeyboardMarkup:
    """Кнопки для модерации события"""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"adm_ok:{event_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm_no:{event_id}")
    kb.button(text="📄 Подробнее", callback_data=f"adm_view:{event_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def pay_test_kb(event_id: int) -> InlineKeyboardMarkup:
    """Кнопки для тестовой оплаты"""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Оплачено (тест)", callback_data=f"pay_test:{event_id}")
    kb.adjust(1)
    return kb.as_markup()


def pay_kb(event_id: int) -> InlineKeyboardMarkup:
    """Кнопка для запуска реальной оплаты (YooKassa)"""
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", callback_data=f"pay_start:{event_id}")
    kb.adjust(1)
    return kb.as_markup()

# ==================== USERS LIST (pagination) ====================

def _fmt_user_row(u: User) -> str:
    """Форматировать строку пользователя"""
    un = f"@{u.username}" if u.username else "—"
    name = " ".join([x for x in [u.first_name, u.last_name] if x]) or "—"
    last_seen = u.last_seen_at.strftime("%Y-%m-%d %H:%M") if u.last_seen_at else "—"
    created = u.created_at.strftime("%Y-%m-%d %H:%M") if u.created_at else "—"
    return f"• {un} | {name} | id={u.telegram_id} | last={last_seen} | reg={created}"


def _users_nav_kb(page: int, has_prev: bool, has_next: bool) -> InlineKeyboardMarkup:
    """Кнопки навигации по пользователям"""
    kb = InlineKeyboardBuilder()
    if has_prev:
        kb.button(text="◀️", callback_data=f"adm_users:{page-1}")
    kb.button(text=f"страница {page+1}", callback_data="adm_users:noop")
    if has_next:
        kb.button(text="▶️", callback_data=f"adm_users:{page+1}")
    kb.adjust(3)
    return kb.as_markup()


async def _send_users_page(message: Message, page: int):
    """Отправить страницу пользователей"""
    page = max(0, int(page))
    offset = page * USERS_PAGE_SIZE

    async with get_db() as db:
        total = (await db.execute(select(func.count()).select_from(User))).scalar_one() or 0

        users = (
            (await db.execute(
                select(User)
                .order_by(desc(User.last_seen_at), desc(User.created_at))
                .offset(offset)
                .limit(USERS_PAGE_SIZE + 1)
            ))
            .scalars()
            .all()
        )

        has_next = len(users) > USERS_PAGE_SIZE
        users = users[:USERS_PAGE_SIZE]
        has_prev = page > 0

        lines = [f"👥 Пользователи: {total}", ""]

        if not users:
            lines.append("Пока пользователей нет.")
            await message.answer("\n".join(lines), reply_markup=admin_panel_kb())
            return

        lines += [_fmt_user_row(u) for u in users]

        text = "\n".join(lines)
        if len(text) > 3900:
            text = text[:3900] + "\n…"

        await message.answer(
            text,
            reply_markup=_users_nav_kb(page=page, has_prev=has_prev, has_next=has_next)
        )


@router.callback_query(F.data.startswith("adm_users:"))
async def admin_users_nav(callback: CallbackQuery):
    """Навигация по пользователям"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    arg = callback.data.split(":", 1)[1]

    if arg == "noop":
        await callback.answer()
        return

    await callback.answer()
    await _send_users_page(callback.message, page=int(arg))


@router.message(F.text == "👥 Пользователи")
async def admin_users_start(message: Message):
    """Список пользователей"""
    await _touch_from_message(message)

    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    await _send_users_page(message, page=0)


# ==================== ENTRY / NAV ====================

@router.message(F.text.in_({"🔧 Админ", "🛡 Админ"}))
async def admin_entry(message: Message, state: FSMContext):
    """Вход в админ-панель"""
    await _touch_from_message(message)

    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    await state.set_state(AdminState.panel)
    await message.answer("🛡 Админ-панель:", reply_markup=admin_panel_kb())


@router.message(AdminState.panel, F.text.startswith("⬅️"))
async def admin_back_message(message: Message, state: FSMContext):
    """Выход из админ-панели"""
    await _touch_from_message(message)

    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


# ==================== STATS ====================

@router.message(AdminState.panel, F.text.startswith("📊"))
async def admin_stats_message(message: Message):
    """Статистика"""
    await _touch_from_message(message)

    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    logger.info("ADMIN_STATS_HIT user_id=%s text=%r", message.from_user.id, message.text)

    s = await get_global_user_stats(limit_users=20)

    def uline(u: dict) -> str:
        tid = u.get("telegram_id")
        un = u.get("username")
        name = " ".join([x for x in [u.get("first_name"), u.get("last_name")] if x]) or "—"
        un_part = f"@{un}" if un else "—"
        return f"• {un_part} | {name} | id={tid}"

    lines = [
        "📊 Статистика",
        "",
        f"👥 Всего пользователей: {s.get('total_users', 0)}",
        f"🆕 Новых за сегодня: {s.get('new_today', 0)}",
        f"✅ Активных за 7 дней: {s.get('active_7d', 0)}",
        f"✅ Активных за 30 дней: {s.get('active_30d', 0)}",
    ]

    recent = s.get("recent_users") or []
    if recent:
        lines += ["", "🕒 Последние активные (топ 10):"]
        lines += [uline(u) for u in recent[:10]]

    new_today_users = s.get("new_users_today") or []
    if new_today_users:
        lines += ["", "🆕 Новые сегодня (топ 10):"]
        lines += [uline(u) for u in new_today_users[:10]]

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n…"

    await message.answer(text, reply_markup=admin_panel_kb())


# ==================== FINANCE ====================

@router.message(AdminState.panel, F.text.startswith("💰"))
async def admin_finance_stub(message: Message):
    """Финансы (заглушка)"""
    await _touch_from_message(message)

    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    await message.answer(
        "💰 Финансы (скоро)\n\nПлан: доход по категориям, по пакетам, средний чек, топ-пакеты.",
        reply_markup=admin_panel_kb(),
    )


# ==================== MODERATION QUEUE ====================

@router.message(AdminState.panel, F.text.startswith("🗂"))
async def admin_moderation_queue(message: Message):
    """Очередь модерации"""
    await _touch_from_message(message)

    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    async with get_db() as db:
        events = (
            (await db.execute(
                select(Event)
                .where(Event.status == EventStatus.PENDING_MODERATION)
                .order_by(desc(Event.created_at))
                .limit(10)
            ))
            .scalars()
            .all()
        )

        if not events:
            await message.answer("Очередь модерации пуста.", reply_markup=admin_panel_kb())
            return

        await message.answer("🛡 Очередь модерации (последние 10):", reply_markup=admin_panel_kb())

        for e in events:
            card = (
                f"📝 {h(e.title)}\n"
                f"🏙 {h(e.city_slug)} • 🏷 {h(e.category)}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📅 Когда: {h(fmt_when(e))}\n"
                f"📍 Где: {h(e.location)}\n"
                f"💳 Цена: {h(fmt_price(e))}\n"
                f"👤 Организатор: {e.user_id}\n"
                f"🧾 Статус: {h(fmt_status(e))}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📝 Описание: {h(short(e.description))}"
            )

            await message.answer(card, parse_mode="HTML", reply_markup=moderation_kb(e.id))


@router.callback_query(F.data.startswith("adm_view:"))
async def admin_view(callback: CallbackQuery):
    """Подробный просмотр события"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    event_id = int(callback.data.split(":", 1)[1])

    async with get_db() as db:
        e = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not e:
            await callback.answer("Заявка не найдена", show_alert=True)
            return

        full = (
            f"📄 {h(e.title)}\n"
            f"🏙 {h(e.city_slug)} • 🏷 {h(e.category)}\n\n"
            f"📅 Когда: {h(fmt_when(e))}\n"
            f"📍 Где: {h(e.location)}\n"
            f"💳 Цена: {h(fmt_price(e))}\n"
            f"📞 Тел: {h(e.contact_phone or '—')}\n"
 #           f"✉️ Email: {h(e.contact_email or '—')}\n"
            f"👤 Организатор: {e.user_id}\n"
            f"🧾 Статус: {h(fmt_status(e))}\n\n"
            f"📝 Описание:\n{h(compact(e.description) or '—')}"
        )

        await callback.message.answer(full, parse_mode="HTML")
        await callback.answer()


@router.callback_query(F.data.startswith("adm_ok:"))
async def admin_approve(callback: CallbackQuery):
    """Одобрить событие"""

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    event_id = int(callback.data.split(":", 1)[1])

    async with get_db() as db:
        event = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not event:
            await callback.answer("Заявка не найдена", show_alert=True)
            return

        # 1) Меняем статус (как и было)
        event.status = EventStatus.APPROVED_WAITING_PAYMENT
        await db.commit()

    # 2) Обновляем сообщение в админке (как и было)
    if callback.message:
        suffix = "\n\n✅ Одобрено. Ожидаем оплату от организатора."
        try:
            if getattr(callback.message, "photo", None):
                current = callback.message.caption or ""
                await callback.message.edit_caption(
                    caption=current + suffix,
                    parse_mode="HTML",
                    reply_markup=None,
                )
            else:
                current = callback.message.text or ""
                await callback.message.edit_text(
                    current + suffix,
                    parse_mode="HTML",
                    reply_markup=None,
                )
        except Exception:
            await callback.message.answer(
                "✅ Одобрено. Ожидаем оплату от организатора.",
                parse_mode="HTML",
            )

    # 3) Уведомляем организатора (логика та же, меняем только кнопку)
    # PAYMENTS_REAL_ENABLED берём из .env через config.py
    try:
        if PAYMENTS_REAL_ENABLED:
            # Реальный режим: показываем кнопку "💳 Оплатить" (pay_start:<id>)
            reply_kb = pay_kb(event_id)
        else:
            # Тестовый режим: оставляем текущую "✅ Оплачено (тест)" (pay_test:<id>)
            reply_kb = pay_test_kb(event_id)

        await callback.bot.send_message(
            event.user_id,
            "✅ Одобрено.\n\nОплатите размещение, после оплаты мероприятие появится в ленте города.",
            parse_mode="HTML",
            reply_markup=reply_kb,
        )
    except Exception:
        # чтобы не ломать модерацию, даже если у юзера закрыты сообщения и т.п.
        pass

    await callback.answer("Одобрено")


@router.callback_query(F.data.startswith("adm_no:"))
async def admin_reject_start(callback: CallbackQuery, state: FSMContext):
    """Начать отклонение события"""
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    event_id = int(callback.data.split(":", 1)[1])

    await state.set_state(AdminReject.waiting_reason)
    await state.update_data(reject_event_id=event_id)

    await callback.message.answer(
        "✍️ Введите причину отказа одним сообщением:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminReject.waiting_reason)
async def admin_reject_reason(message: Message, state: FSMContext):
    """Ввод причины отказа"""
    await _touch_from_message(message)

    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    reason = (message.text or "").strip()

    if len(reason) < 3:
        await message.answer("Причина слишком короткая. Напишите подробнее.")
        return

    data = await state.get_data()
    event_id = int(data["reject_event_id"])

    async with get_db() as db:
        event = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not event:
            await message.answer("Заявка не найдена")
            await state.clear()
            return

        event.status = EventStatus.REJECTED
        event.reject_reason = reason
        await db.commit()

        # Уведомляем организатора
        await message.bot.send_message(
            event.user_id,
            f"❌ Отклонено\n\nПричина отказа: {h(reason)}\n\nИсправьте и отправьте заявку повторно.",
            parse_mode="HTML",
        )

        await message.answer(
            "❌ Заявка отклонена, организатор уведомлён.",
            reply_markup=admin_panel_kb()
        )

    await state.clear()


# ==================== PAYMENT (test) ====================

@router.callback_query(F.data.startswith("pay_start:"))
async def organizer_pay_start(callback: CallbackQuery):
    try:
        event_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    async with get_db() as db:
        event = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
        if not event:
            await callback.answer("Событие не найдено.", show_alert=True)
            return

        # owner-check
        if event.user_id != callback.from_user.id:
            await callback.answer("Это событие принадлежит другому пользователю.", show_alert=True)
            return

        if event.status == EventStatus.ACTIVE:
            await callback.message.answer("⚠️ Уже опубликовано.", parse_mode="HTML")
            await callback.answer()
            return

        if event.status != EventStatus.APPROVED_WAITING_PAYMENT:
            await callback.answer("Оплата будет доступна после модерации.", show_alert=True)
            return

        # один платеж на одно событие (event_id unique=True)
        existing_payment = (
            await db.execute(select(Payment).where(Payment.event_id == event.id))
        ).scalar_one_or_none()

        if existing_payment and existing_payment.status == PaymentStatus.COMPLETED:
            event.payment_status = PaymentStatus.COMPLETED
            event.status = EventStatus.ACTIVE
            await db.commit()

            await callback.message.answer("✅ Оплата уже прошла, событие опубликовано.", parse_mode="HTML")
            await callback.answer()
            return

        # ---------------- ФИКСИРОВАННАЯ ЦЕНА ИЗ CONFIG ----------------
        category_code = event.category.value if isinstance(event.category, EventCategory) else str(event.category)

        cfg = PRICING_CONFIG.get(category_code)
        if not cfg:
            await callback.answer("Не найдена конфигурация цены для категории.", show_alert=True)
            return

        packages = cfg.get("packages") or {}
        if not packages:
            await callback.answer("Для категории не задана цена.", show_alert=True)
            return

        # Берём первый (и по твоей задумке единственный активный) пакет
        package_key, package_price = next(iter(packages.items()))
        try:
            amount = float(package_price)
        except Exception:
            await callback.answer("Цена в конфиге задана некорректно.", show_alert=True)
            return

        model = (cfg.get("model") or "daily").strip().lower()
        if model == "period":
            pricing_model = PricingModel.PERIOD
            package_period = package_key
            num_days = None
            package_daily = None
            num_posts = None
        else:
            pricing_model = PricingModel.DAILY
            package_daily = package_key
            num_posts = None
            package_period = None
            num_days = None

        # return_url
        return_url = (YOOKASSA_RETURN_URL or "").strip()
        if not return_url:
            if not PUBLIC_BASE_URL:
                await callback.answer("PUBLIC_BASE_URL не настроен.", show_alert=True)
                return
            return_url = f"{PUBLIC_BASE_URL}/payment-return"

        description = f"Оплата публикации события #{event.id}"

        # email для чека (в модели User email нет -> fallback)
        user = (await db.execute(select(User).where(User.telegram_id == event.user_id))).scalar_one_or_none()
        customer_email = getattr(user, "email", None) if user else None
        if not customer_email:
            customer_email = "your-ip-email@example.com"

        try:
            yk_payment_id, confirmation_url = await create_payment(
                amount_rub=amount,
                description=description,
                return_url=return_url,
                customer_email=customer_email,
                metadata={"event_id": str(event.id), "user_id": str(event.user_id), "category": category_code},
                idempotence_key=f"event{event.id}-user{event.user_id}",
                capture=True,
                tax_system_code=2,
                vat_code=1,
            )
        except Exception:
            logger.exception("YooKassa create_payment failed event_id=%s", event.id)
            await callback.answer("Не удалось создать оплату. Попробуйте позже.", show_alert=True)
            return

        # Создаем/обновляем Payment
        if not existing_payment:
            p = Payment(
                user_id=event.user_id,
                event_id=event.id,
                category=event.category,
                pricing_model=pricing_model,
                package_daily=package_daily,
                num_posts=num_posts,
                package_period=package_period,
                num_days=num_days,
                amount=amount,
                status=PaymentStatus.PENDING,
                payment_system="yookassa",
                transaction_id=yk_payment_id,
            )
            db.add(p)
        else:
            existing_payment.category = event.category
            existing_payment.pricing_model = pricing_model
            existing_payment.package_daily = package_daily
            existing_payment.num_posts = num_posts
            existing_payment.package_period = package_period
            existing_payment.num_days = num_days
            existing_payment.amount = amount
            existing_payment.status = PaymentStatus.PENDING
            existing_payment.payment_system = "yookassa"
            existing_payment.transaction_id = yk_payment_id

        await db.commit()

    from aiogram.types import InlineKeyboardButton

    pay_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=confirmation_url)]]
    )
    await callback.message.answer(
        f"💳 Сумма к оплате: {int(amount) if amount.is_integer() else amount} ₽\n"
        f"Перейди по ссылке и оплати публикацию.",
        parse_mode="HTML",
        reply_markup=pay_kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_test:"))
async def organizer_pay_test(callback: CallbackQuery):
    """Тестовая оплата события"""
    try:
        event_id = int(callback.data.split(":", 1)[1])
    except Exception:
        await callback.answer("Некорректные данные.", show_alert=True)
        return

    async with get_db() as db:
        event = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
        if not event:
            await callback.answer("Заявка не найдена", show_alert=True)
            return

        if event.user_id != callback.from_user.id:
            await callback.answer("Это не ваша заявка", show_alert=True)
            return

        if event.status == EventStatus.ACTIVE:
            await callback.message.answer("⚠️ Уже опубликовано.", parse_mode="HTML")
            await callback.answer()
            return

        existing_payment = (
            await db.execute(select(Payment).where(Payment.event_id == event.id))
        ).scalar_one_or_none()

        if existing_payment and existing_payment.status == PaymentStatus.COMPLETED:
            event.payment_status = PaymentStatus.COMPLETED
            event.status = EventStatus.ACTIVE
            await db.commit()
        else:
            # Тестовый платеж = COMPLETED
            p = existing_payment or Payment(
                user_id=event.user_id,
                event_id=event.id,
                category=event.category,
                pricing_model=PricingModel.DAILY,
                amount=0.0,
                status=PaymentStatus.COMPLETED,
                payment_system="test",
                completed_at=datetime.utcnow(),
            )
            p.status = PaymentStatus.COMPLETED
            p.payment_system = "test"
            p.completed_at = datetime.utcnow()
            if not existing_payment:
                db.add(p)

            event.payment_status = PaymentStatus.COMPLETED
            event.status = EventStatus.ACTIVE
            await db.commit()

        eid = event.id
        city = event.city_slug

    await callback.message.answer(
        "✅ Оплата подтверждена (тест).\nМероприятие опубликовано в ленте города.",
        parse_mode="HTML",
    )

    # Уведомляем жителей (как и было)
    try:
        logger.warning("NOTIFY: TRY event_id=%s city=%s", eid, city)
        await asyncio.sleep(0.2)
        res = await notify_new_event_published(callback.bot, eid)
        logger.warning("NOTIFY: RESULT event_id=%s res=%s", eid, res)
    except Exception as e:
        logger.exception("NOTIFY: ERROR event_id=%s error=%r", eid, e)

    await callback.answer()


@router.message(AdminState.panel)
async def admin_panel_fallback(message: Message):
    """Fallback для любого непредусмотренного текста в админке"""
    if not is_admin(message.from_user.id):
        return

    await message.answer("Выберите действие кнопками ниже.", reply_markup=admin_panel_kb())
