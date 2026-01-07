from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from config import ADMIN_IDS

router = Router()


def roles_keyboard(user_id: int):
    kb = ReplyKeyboardBuilder()
    kb.button(text="🏠 Житель")
    kb.button(text="🎪 Организатор")
    kb.button(text="📞 Обратная связь")
    if user_id in ADMIN_IDS:
        kb.button(text="🔧 Админ")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎉 **EventsNow — Добро пожаловать!**\n\n"
        "*Все события твоего города в одном месте*\n\n"
        "👇 Выбери роль:",
        reply_markup=roles_keyboard(message.from_user.id),
        parse_mode="Markdown",
    )
