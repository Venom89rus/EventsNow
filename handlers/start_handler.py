import html

from aiogram import Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.utils.deep_linking import decode_payload

from sqlalchemy import select

from config import ADMIN_IDS
from database.session import get_db
from database.models import Event, EventStatus, EventCategory, EventPhoto

from services.user_activity import touch_user

router = Router()

DESC_PREVIEW_LEN = 120

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

def roles_keyboard(user_id: int):
    kb = ReplyKeyboardBuilder()
    kb.button(text="🏠 Житель")
    kb.button(text="🎪 Организатор")
    kb.button(text="📞 Обратная связь")
    if user_id in ADMIN_IDS:
        kb.button(text="🔧 Админ")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def category_ru(cat: EventCategory | str) -> str:
    code = cat.value if hasattr(cat, "value") else str(cat)
    mapping = {
        "EXHIBITION": "Выставка",
        "MASTERCLASS": "Мастер-класс",
        "CONCERT": "Концерт",
        "PERFORMANCE": "Выступление",
        "LECTURE": "Лекция/семинар",
        "OTHER": "Другое",
    }
    return mapping.get(code, code)

def category_emoji(cat: EventCategory | str) -> str:
    code = cat.value if hasattr(cat, "value") else str(cat)
    mapping = {
        "EXHIBITION": "🖼",
        "MASTERCLASS": "🧑🏫",
        "CONCERT": "🎤",
        "PERFORMANCE": "🎭",
        "LECTURE": "🎓",
        "OTHER": "✨",
    }
    return mapping.get(code, "✨")

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
    if e.category == EventCategory.CONCERT:
        return f"от {s} ₽"
    return f"{s} ₽"

def event_deeplink_text(e: Event) -> str:
    cat = f"{category_emoji(e.category)} {category_ru(e.category)}"
    return (
        f"🎫 {h(e.title)}\n"
        f"🏷 {h(cat)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 Когда: {h(fmt_when(e))}\n"
        f"📍 Где: {h(e.location)}\n"
        f"💳 Цена: {h(fmt_price(e))}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 Описание: {h(short(e.description))}"
    )

async def fetch_event_first_photo(event_id: int) -> str | None:
    async with get_db() as db:
        p = (
            await db.execute(
                select(EventPhoto)
                .where(EventPhoto.event_id == event_id)
                .order_by(EventPhoto.position.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return p.file_id if p else None

async def open_event_by_deeplink(message: Message, event_id: int) -> bool:
    async with get_db() as db:
        e = (await db.execute(select(Event).where(Event.id == event_id))).scalar_one_or_none()

    if not e or e.status != EventStatus.ACTIVE:
        await message.answer(
            "Событие по ссылке не найдено или уже недоступно.\n\n👇 Выбери роль:",
            reply_markup=roles_keyboard(message.from_user.id),
            parse_mode="HTML",
        )
        return False

    file_id = await fetch_event_first_photo(event_id)
    if file_id:
        await message.answer_photo(
            photo=file_id,
            caption=event_deeplink_text(e),
            parse_mode="HTML",
            reply_markup=roles_keyboard(message.from_user.id),
        )
    else:
        await message.answer(
            event_deeplink_text(e),
            parse_mode="HTML",
            reply_markup=roles_keyboard(message.from_user.id),
        )
    return True

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    # фиксируем пользователя + last_seen_at
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    args_raw = (command.args or "").strip()

    if args_raw:
        args = args_raw
        if not args_raw.lower().startswith("e") and all(ch.isalnum() or ch in "-_" for ch in args_raw):
            try:
                args = decode_payload(args_raw)
            except Exception:
                args = args_raw

        low = args.lower()
        if low.startswith("e"):
            raw_id = low[1:].strip()
            if raw_id.isdigit():
                if await open_event_by_deeplink(message, int(raw_id)):
                    return

    await message.answer(
        "🎉 EventsNow — Добро пожаловать!\n\n"
        "Все события твоего города в одном месте\n\n"
        "👇 Выбери роль:",
        reply_markup=roles_keyboard(message.from_user.id),
        parse_mode="HTML",
    )
