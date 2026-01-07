from aiogram import Router, F
from aiogram.types import Message

router = Router()

@router.message(F.text == "📞 Обратная связь")
async def feedback_entry(message: Message):
    await message.answer(
        "📞 Напиши сообщение — оно будет обработано администратором.\n\n"
        "Функционал логирования обращений подключим следующим шагом."
    )
