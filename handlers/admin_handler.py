import html
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from config import ADMIN_IDS
from database.session import get_db
from database.models import Event, EventStatus, Payment, PaymentStatus, PricingModel

router = Router()


def h(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def moderation_kb(event_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Разместить", callback_data=f"adm_ok:{event_id}")
    kb.button(text="❌ Отклонить", callback_data=f"adm_no:{event_id}")
    kb.adjust(1)
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


class AdminReject(StatesGroup):
    waiting_reason = State()


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

        event.status = EventStatus.APPROVED_WAITING_PAYMENT
        event.reject_reason = None

        await callback.message.edit_text(
            callback.message.text + "\n\n✅ <b>Статус:</b> Одобрено. Ожидаем оплату.",
            parse_mode="HTML",
            reply_markup=None,
        )

        # сообщение организатору + кнопка оплатить
        await callback.bot.send_message(
            event.user_id,
            "✅ <b>Одобрено.</b>\n\nОплатите размещение, после оплаты мероприятие появится в ленте города.",
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

    await callback.message.answer(
        "✍️ Введите причину отказа одним сообщением:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AdminReject.waiting_reason)
async def admin_reject_reason(message: Message, state: FSMContext):
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
        event.reject_reason = reason

        # уведомление организатору
        await message.bot.send_message(
            event.user_id,
            "❌ <b>Отклонено</b>\n\n"
            f"<b>Причина отказа:</b> {h(reason)}\n\n"
            "Устраните замечания администрации и направьте заявку на повторную модерацию.",
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
        if event.status != EventStatus.APPROVED_WAITING_PAYMENT:
            await callback.answer("Оплата недоступна для текущего статуса", show_alert=True)
            return

        await callback.message.answer(
            "💳 <b>Оплата размещения</b>\n\n"
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
        if event.status != EventStatus.APPROVED_WAITING_PAYMENT:
            await callback.answer("Оплата недоступна для текущего статуса", show_alert=True)
            return

        # создаём Payment (тест)
        p = Payment(
            user_id=event.user_id,
            event_id=event.id,
            category=event.category,
            pricing_model=PricingModel.PERIOD if event.period_start and event.period_end else PricingModel.DAILY,
            amount=0.0,
            status=PaymentStatus.COMPLETED,
            payment_system="test",
        )
        db.add(p)
        await db.flush()  # чтобы p.id появился

        event.payment_status = PaymentStatus.COMPLETED
        event.payment_id = p.id
        event.status = EventStatus.ACTIVE

        await callback.message.answer(
            "✅ <b>Оплата подтверждена (тест).</b>\nМероприятие опубликовано в ленте города.",
            parse_mode="HTML",
        )

    await callback.answer()
