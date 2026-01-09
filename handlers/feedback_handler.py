import html
from datetime import datetime

from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from config import ADMIN_IDS
from database.session import get_db
from database.models import Feedback

router = Router()


def h(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🏠 Житель"), KeyboardButton(text="🎪 Организатор")],
            [KeyboardButton(text="📞 Обратная связь"), KeyboardButton(text="🛡 Админ")],
        ],
        resize_keyboard=True,
    )


class FeedbackState(StatesGroup):
    waiting_message = State()


@router.message(F.text.contains("Обратная связь"))
async def feedback_entry(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(FeedbackState.waiting_message)

    await message.answer(
        "📞 <b>Обратная связь</b>\n\n"
        "Напиши сообщение одним текстом — оно будет отправлено администратору.\n"
        "Чтобы отменить — отправь <code>Отмена</code>.",
        parse_mode="HTML",
    )


@router.message(FeedbackState.waiting_message, F.text.casefold() == "отмена")
async def feedback_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено. Главное меню:", reply_markup=main_menu_kb())


@router.message(FeedbackState.waiting_message)
async def feedback_save(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if len(text) < 3:
        await message.answer("Сообщение слишком короткое. Напиши подробнее или отправь <code>Отмена</code>.", parse_mode="HTML")
        return
    if len(text) > 4000:
        await message.answer("Сообщение слишком длинное (лимит 4000 символов). Сократи, пожалуйста.")
        return

    async with get_db() as db:
        fb = Feedback(
            user_id=message.from_user.id,
            message=text,
            created_at=datetime.utcnow(),
        )
        db.add(fb)

    # уведомляем админов
    admin_text = (
        "📩 <b>Новое обращение</b>\n\n"
        f"👤 Пользователь: <code>{message.from_user.id}</code>\n"
        f"🧾 Username: @{h(message.from_user.username) if message.from_user.username else '—'}\n\n"
        f"💬 Сообщение:\n{h(text)}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, admin_text, parse_mode="HTML")
        except Exception:
            pass

    await state.clear()
    await message.answer("✅ Сообщение отправлено. Спасибо!", reply_markup=main_menu_kb())
