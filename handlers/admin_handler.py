import html
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    CallbackQuery,
    Message,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, desc

from config import ADMIN_IDS
from database.session import get_db
from database.models import Event, EventStatus, Payment, PaymentStatus, PricingModel

from services.stats_service import get_global_user_stats
from services.user_activity import touch_user

router = Router()

DESC_PREVIEW_LEN = 120


def h(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def compact(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(text.split())


def short(text: str | None, limit: int = DESC_PREVIEW_LEN) -> str:
    t = compact(text)
    if not t:
        return "—"
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


def main_menu_kb() -> ReplyKeyboardMarkup:
    # Главное меню без импортов из start/resident/organizer
    # (избегаем циклических импортов).
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Житель"), KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="📞 Обратная связь"), KeyboardButton(text="🔧 Админ")],
        ],
        resize_keyboard=True,
    )


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
    return f"{s} ₽"


def fmt_status(e: Event) -> str:
    mapping = {
        EventStatus.DRAFT: "⚪ draft",
        EventStatus.PENDING_MODERATION: "🟡 на модерации",
        EventStatus.ACTIVE: "🟢 опубликовано",
        EventStatus.ARCHIVED: "⚫ архив",
        EventStatus.REJECTED: "🔴 отклонено",
    }
    return mapping.get(e.status, str(e.status))


def moderation_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить", callback_data=f"adm_ok:{event_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm_no:{event_id}")
    kb.button(text="📄 Подробнее", callback_data=f"adm_view:{event_id}")
    kb.adjust(2, 1)
    return kb.as_markup()


def pay_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💳 Оплатить", callback_data=f"pay_start:{event_id}")
    kb.adjust(1)
    return kb.as_markup()


def pay_test_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Оплачено (тест)", callback_data=f"pay_test:{event_id}")
    kb.adjust(1)
    return kb.as_markup()


def admin_panel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗂 События на модерацию"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="💰 Финансы"), KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


class AdminReject(StatesGroup):
    waiting_reason = State()


async def _touch_from_message(message: Message) -> None:
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )


@router.message(F.text == "🔧 Админ")
async def admin_entry(message: Message):
    await _touch_from_message(message)
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    await message.answer("🛡 Админ-панель:", reply_markup=admin_panel_kb())


@router.message(F.text == "⬅️ Назад")
async def admin_back(message: Message, state: FSMContext):
    await _touch_from_message(message)
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    await _touch_from_message(message)
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    s = await get_global_user_stats()
    text = (
        "<b>📊 Статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{s['total_users']}</b>\n"
        f"🆕 Новых за сегодня: <b>{s['new_today']}</b>\n"
        f"✅ Активных за 7 дней: <b>{s['active_7d']}</b>\n"
        f"✅ Активных за 30 дней: <b>{s['active_30d']}</b>\n"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=admin_panel_kb())


@router.message(F.text == "💰 Финансы")
async def admin_finance_stub(message: Message):
    await _touch_from_message(message)
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    await message.answer(
        "💰 Финансы (скоро)\n\n"
        "План: доход по категориям, по пакетам, средний чек, топ-пакеты.",
        reply_markup=admin_panel_kb(),
    )


@router.message(F.text == "🗂 События на модерацию")
async def admin_moderation_queue(message: Message):
    await _touch_from_message(message)
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    async with get_db() as db:
        events = (
            await db.execute(
                select(Event)
                .where(Event.status == EventStatus.PENDING_MODERATION)
                .order_by(desc(Event.created_at))
                .limit(10)
            )
        ).scalars().all()

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
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()

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
        f"✉️ Email: {h(e.contact_email or '—')}\n"
        f"👤 Организатор: {e.user_id}\n"
        f"🧾 Статус: {h(fmt_status(e))}\n\n"
        f"📝 Описание:\n{h(compact(e.description) or '—')}"
    )
    await callback.message.answer(full, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.startswith("adm_ok:"))
async def admin_approve(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        event = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
        if not event:
            await callback.answer("Заявка не найдена", show_alert=True)
            return

        if hasattr(EventStatus, "APPROVED_WAITING_PAYMENT"):
            event.status = EventStatus.APPROVED_WAITING_PAYMENT
        else:
            event.status = EventStatus.PENDING_MODERATION

    if callback.message:
        suffix = "\n\n✅ Одобрено. Ожидаем оплату от организатора."
        try:
            if callback.message.photo:
                current = callback.message.caption or ""
                await callback.message.edit_caption(caption=current + suffix, parse_mode="HTML", reply_markup=None)
            else:
                current = callback.message.text or ""
                await callback.message.edit_text(current + suffix, parse_mode="HTML", reply_markup=None)
        except Exception:
            await callback.message.answer("✅ Одобрено. Ожидаем оплату от организатора.", parse_mode="HTML")

    await callback.bot.send_message(
        event.user_id,
        "✅ Одобрено.\n\nОплатите размещение, после оплаты мероприятие появится в ленте города.",
        parse_mode="HTML",
        reply_markup=pay_kb(event.id),
    )

    await state.clear()
    await callback.answer("Одобрено")


@router.callback_query(F.data.startswith("adm_no:"))
async def admin_reject_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    event_id = int(callback.data.split(":")[1])
    await state.set_state(AdminReject.waiting_reason)
    await state.update_data(reject_event_id=event_id)
    await callback.message.answer("✍️ Введите причину отказа одним сообщением:", parse_mode="HTML")
    await callback.answer()


@router.message(AdminReject.waiting_reason)
async def admin_reject_reason(message: Message, state: FSMContext):
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
        event = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()
        if not event:
            await message.answer("Заявка не найдена")
            await state.clear()
            return

        event.status = EventStatus.REJECTED

    await message.bot.send_message(
        event.user_id,
        "❌ Отклонено\n\n"
        f"Причина отказа: {h(reason)}\n\n"
        "Исправьте и отправьте заявку повторно.",
        parse_mode="HTML",
    )

    await message.answer("❌ Заявка отклонена, организатор уведомлён.")
    await state.clear()


@router.callback_query(F.data.startswith("pay_start:"))
async def organizer_pay_start(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        event = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()

    if not event:
        await callback.answer("Заявка не найдена", show_alert=True)
        return

    if event.user_id != callback.from_user.id:
        await callback.answer("Это не ваша заявка", show_alert=True)
        return

    if event.status == EventStatus.ACTIVE:
        await callback.answer("Уже опубликовано", show_alert=True)
        return

    await callback.message.answer(
        "💳 Оплата размещения\n\n"
        "Пока включён тестовый режим.\n"
        "Нажмите «Оплачено (тест)» для продолжения.",
        parse_mode="HTML",
        reply_markup=pay_test_kb(event_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("pay_test:"))
async def organizer_pay_test(callback: CallbackQuery):
    event_id = int(callback.data.split(":")[1])

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

        existing_payment = (await db.execute(select(Payment).where(Payment.event_id == event.id))).scalar_one_or_none()
        if existing_payment and existing_payment.status == PaymentStatus.COMPLETED:
            event.payment_status = PaymentStatus.COMPLETED
            event.status = EventStatus.ACTIVE
            await callback.message.answer("⚠️ Уже оплачено ранее, мероприятие опубликовано.", parse_mode="HTML")
            await callback.answer()
            return

        p = Payment(
            user_id=event.user_id,
            event_id=event.id,
            category=event.category,
            pricing_model=PricingModel.PERIOD if (event.period_start and event.period_end) else PricingModel.DAILY,
            amount=0.0,
            status=PaymentStatus.COMPLETED,
            payment_system="test",
            completed_at=datetime.utcnow(),
        )
        db.add(p)

        event.payment_status = PaymentStatus.COMPLETED
        event.status = EventStatus.ACTIVE

    await callback.message.answer(
        "✅ Оплата подтверждена (тест).\nМероприятие опубликовано в ленте города.",
        parse_mode="HTML",
    )
    await callback.answer()
