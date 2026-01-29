import html
import json
import re
import urllib.parse
import logging
from typing import Any

from aiogram import Router, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message, InlineKeyboardMarkup, CallbackQuery
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.utils.deep_linking import decode_payload
from sqlalchemy import select

from config import ADMIN_IDS
from database.session import get_db
from database.models import Event, EventStatus, EventCategory, EventPhoto, Favorite
from services.user_activity import touch_user

router = Router()
logger = logging.getLogger("eventsnow")

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


def main_menu_kb(user_id: int):
    """Главное меню. Админ-кнопка только для ADMIN_IDS."""
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
        "PERFORMANCE": "Спектакль",
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
    """1) Если admission_price_json — красивая цена. 2) Иначе price_admission."""
    raw_json = getattr(e, "admission_price_json", None)
    if raw_json:
        try:
            data = json.loads(raw_json)
            if isinstance(data, dict):
                items: list[tuple[str, float]] = []
                for k, v in data.items():
                    if k is None:
                        continue
                    key = str(k).strip()
                    if not key:
                        continue
                    try:
                        val = float(v)
                    except Exception:
                        continue
                    if val <= 0:
                        continue
                    items.append((key, val))

                if items:
                    preferred = ["вход", "взрослый", "детский", "льготный", "vip"]
                    order = {name: i for i, name in enumerate(preferred)}
                    items.sort(key=lambda kv: (order.get(kv[0].lower(), 999), kv[0].lower()))

                    def fmtnum(x: float) -> str:
                        return str(int(x)) if float(x).is_integer() else str(x)

                    if len(items) == 1 and items[0][0].lower() in ("вход", "входной", "входной билет"):
                        s = fmtnum(items[0][1])
                        return f"от {s} ₽" if e.category == EventCategory.CONCERT else f"{s} ₽"

                    parts = [f"{k}: {fmtnum(v)} ₽" for k, v in items]
                    return " / ".join(parts)
        except Exception:
            pass

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


def event_card_text_short(e: Event) -> str:
    """Сокращённая карточка события (preview)"""
    cat = f"{category_emoji(e.category)} {category_ru(e.category)}"
    return (
        f"🎫 <b>{h(e.title)}</b>\n"
        f"🏷 {h(cat)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 Когда: {h(fmt_when(e))}\n"
        f"📍 Где: {h(e.location)}\n"
        f"💳 Цена: {h(fmt_price(e))}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 Описание: {h(short(e.description))}"
    )


def event_card_text_full(e: Event) -> str:
    """Полная карточка события (после ПОДРОБНЕЕ)"""
    cat = f"{category_emoji(e.category)} {category_ru(e.category)}"
    return (
        f"🎫 <b>{h(e.title)}</b>\n"
        f"🏷 {h(cat)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 Когда: {h(fmt_when(e))}\n"
        f"📍 Где: {h(e.location)}\n"
        f"💳 Цена: {h(fmt_price(e))}\n"
        f"📞 Тел: {h(e.contact_phone or '—')}\n"
 #       f"✉️ Email: {h(e.contact_email or '—')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📝 <b>ПОЛНОЕ ОПИСАНИЕ:</b>\n{h(compact(e.description) or '—')}"
    )


async def fetch_event_photos(event_id: int) -> list[EventPhoto]:
    """Получить все фото события"""
    async with get_db() as db:
        photos = (
            await db.execute(
                select(EventPhoto)
                .where(EventPhoto.event_id == event_id)
                .order_by(EventPhoto.position.asc())
            )
        ).scalars().all()
        return photos


async def is_favorite(user_id: int, event_id: int) -> bool:
    """Проверить в избранном ли событие"""
    async with get_db() as db:
        fav = (
            await db.execute(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.event_id == event_id
                )
            )
        ).scalar_one_or_none()
        return fav is not None


async def set_favorite(user_id: int, event_id: int, value: bool) -> bool:
    """Добавить/убрать в избранное"""
    async with get_db() as db:
        fav = (
            await db.execute(
                select(Favorite).where(
                    Favorite.user_id == user_id,
                    Favorite.event_id == event_id
                )
            )
        ).scalar_one_or_none()

        if value:
            if fav:
                return True
            db.add(Favorite(user_id=user_id, event_id=event_id))
            await db.commit()
            return True
        else:
            if fav:
                await db.delete(fav)
                await db.commit()
            return False


async def build_share_url(bot, event_id: int, title: str | None = None) -> str:
    """Делимся deep-link'ом"""
    bot_info = await bot.get_me()
    deeplink = f"https://t.me/{bot_info.username}?start=app_event_{event_id}"
    text = "EventsNow" if not title else title
    return "https://t.me/share/url?" + urllib.parse.urlencode({
        "url": deeplink,
        "text": text
    })


def event_card_kb_preview(event_id: int, fav: bool, total_photos: int = 0) -> InlineKeyboardMarkup:
    """Кнопки для preview (с кнопкой ПОДРОБНЕЕ)"""
    kb = InlineKeyboardBuilder()

    # ПОДРОБНЕЕ - главная кнопка
    kb.button(text="📋 ПОДРОБНЕЕ", callback_data=f"event_full:{event_id}")

    # Избранное + Организатор
    fav_text = "⭐ В избранном" if fav else "⭐ В избранное"
    kb.button(text=fav_text, callback_data=f"event_fav:{event_id}")
    kb.button(text="👤 Организатор", callback_data=f"event_org:{event_id}")

    # Комментарии
    kb.button(text="💬 Комментарии", callback_data=f"event_comments:{event_id}")

    # Навигация по фото (если есть)
    if total_photos > 1:
        kb.button(text=f"1/{total_photos} ➡️", callback_data=f"event_photo:1:{event_id}")

    kb.adjust(1, 2, 1, 1)
    return kb.as_markup()


def event_card_kb_full(event_id: int, fav: bool) -> InlineKeyboardMarkup:
    """Кнопки для full карточки (из ПОДРОБНЕЕ)"""
    kb = InlineKeyboardBuilder()

    # Вернуться назад
    kb.button(text="🔙 К превью", callback_data=f"event_back:{event_id}")

    # Избранное
    fav_text = "⭐ В избранном" if fav else "⭐ В избранное"
    kb.button(text=fav_text, callback_data=f"event_fav:{event_id}")

    # Поделиться
    kb.button(text="📤 Поделиться", callback_data=f"event_share:{event_id}")

    # Комментарии
    kb.button(text="💬 Комментарии", callback_data=f"event_comments:{event_id}")

    kb.adjust(1, 2, 1)
    return kb.as_markup()


async def open_event_preview(message: Message, event_id: int) -> bool:
    """Открыть preview события по deep-link"""
    async with get_db() as db:
        e = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not e or e.status != EventStatus.ACTIVE:
            await message.answer(
                "❌ Событие по ссылке не найдено или уже недоступно.\n\n👇 Выбери роль:",
                reply_markup=main_menu_kb(message.from_user.id),
                parse_mode="HTML",
            )
            return False

        # Получаем фото
        photos = await fetch_event_photos(event_id)
        fav = await is_favorite(message.from_user.id, event_id)

        # Кнопки
        kb = event_card_kb_preview(event_id, fav, len(photos))

        # Текст карточки (сокращённый)
        text = event_card_text_short(e)

        # Показываем первое фото (если есть)
        if photos:
            first_photo = photos[0].file_id
            await message.answer_photo(
                photo=first_photo,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
        else:
            await message.answer(
                text + "\n\n(Фото отсутствуют)",
                parse_mode="HTML",
                reply_markup=kb,
            )

        return True


def _extract_event_id_from_args(args_raw: str) -> int | None:
    """Извлечение event_id из параметров /start"""
    if not args_raw:
        return None

    args = args_raw.strip()

    # Пытаемся decode_payload
    if not args.lower().startswith("e") and all(ch.isalnum() or ch in "-_" for ch in args):
        try:
            args = decode_payload(args)
        except Exception:
            pass

    low = args.lower().strip()

    # Формат: app_event_123
    m = re.match(r"^app_event_(\d+)$", low)
    if m:
        return int(m.group(1))

    # Формат: e123
    if low.startswith("e"):
        raw_id = low[1:].strip()
        if raw_id.isdigit():
            return int(raw_id)

    return None


# ==================== CALLBACKS ====================

@router.callback_query(F.data.startswith("event_full:"))
async def event_show_full(callback: CallbackQuery):
    """ПОДРОБНЕЕ - показать полный текст"""
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not e:
            await callback.answer("❌ Событие удалено", show_alert=True)
            return

        fav = await is_favorite(callback.from_user.id, event_id)
        kb = event_card_kb_full(event_id, fav)
        text = event_card_text_full(e)

        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as ex:
            logger.warning(f"edit_text failed: {ex}, trying edit_caption")
            try:
                await callback.message.edit_caption(
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception as ex2:
                logger.error(f"edit_caption also failed: {ex2}")
                await callback.message.answer(
                    text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )

        await callback.answer("📖 Показаны полные детали")


@router.callback_query(F.data.startswith("event_back:"))
async def event_back_to_preview(callback: CallbackQuery):
    """Вернуться к preview"""
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not e:
            await callback.answer("❌ Событие удалено", show_alert=True)
            return

        photos = await fetch_event_photos(event_id)
        fav = await is_favorite(callback.from_user.id, event_id)
        kb = event_card_kb_preview(event_id, fav, len(photos))
        text = event_card_text_short(e)

        try:
            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as ex:
            logger.warning(f"edit_text failed: {ex}, trying edit_caption")
            try:
                await callback.message.edit_caption(
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception as ex2:
                logger.error(f"edit_caption also failed: {ex2}")
                await callback.message.answer(
                    text,
                    parse_mode="HTML",
                    reply_markup=kb,
                )

        await callback.answer("🔙 Вернулись к превью")


@router.callback_query(F.data.startswith("event_fav:"))
async def event_toggle_favorite(callback: CallbackQuery):
    """Добавить/убрать в избранное"""
    event_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id

    fav_before = await is_favorite(user_id, event_id)
    fav_after = await set_favorite(user_id, event_id, not fav_before)

    await callback.answer(
        "✅ Добавлено в избранное" if fav_after else "✅ Убрано из избранного"
    )

    # Пересобираем кнопки
    async with get_db() as db:
        e = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not e:
            return

        # Определяем, какой сейчас текст (full или preview)
        is_full = "ПОЛНОЕ ОПИСАНИЕ" in (callback.message.text or "")

        if is_full:
            kb = event_card_kb_full(event_id, fav_after)
        else:
            photos = await fetch_event_photos(event_id)
            kb = event_card_kb_preview(event_id, fav_after, len(photos))

        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass


@router.callback_query(F.data.startswith("event_share:"))
async def event_share(callback: CallbackQuery):
    """Поделиться событием"""
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not e:
            await callback.answer("❌ Событие удалено", show_alert=True)
            return

        share_url = await build_share_url(callback.bot, event_id, e.title)

        # Отправляем ссылку для шеринга
        await callback.answer(f"🔗 Ссылка: {share_url}", show_alert=True)


@router.callback_query(F.data.startswith("event_comments:"))
async def event_comments(callback: CallbackQuery):
    """Комментарии (заглушка)"""
    await callback.answer("💬 Комментарии (скоро)", show_alert=True)


@router.callback_query(F.data.startswith("event_org:"))
async def event_organizer(callback: CallbackQuery):
    """Информация об организаторе"""
    event_id = int(callback.data.split(":")[1])

    async with get_db() as db:
        e = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not e:
            await callback.answer("❌ Событие удалено", show_alert=True)
            return

        text = f"👤 <b>Организатор</b>\n\nID: {e.user_id}\n📞 {h(e.contact_phone or '—')}\n✉️ {h(e.contact_email or '—')}"
        await callback.answer(text, show_alert=True)


@router.callback_query(F.data.startswith("event_photo:"))
async def event_next_photo(callback: CallbackQuery):
    """Навигация по фото"""
    parts = callback.data.split(":")
    current = int(parts[1])
    event_id = int(parts[2])

    photos = await fetch_event_photos(event_id)

    if not photos:
        await callback.answer("Нет фото", show_alert=True)
        return

    total = len(photos)
    next_idx = (current % total) + 1  # 1 -> 2, 2 -> 3, total -> 1

    photo = photos[next_idx - 1]

    async with get_db() as db:
        e = (
            await db.execute(
                select(Event).where(Event.id == event_id)
            )
        ).scalar_one_or_none()

        if not e:
            return

        fav = await is_favorite(callback.from_user.id, event_id)
        kb = event_card_kb_preview(event_id, fav, total)

        text = event_card_text_short(e)

        try:
            await callback.message.edit_media(
                media=photo,
                reply_markup=kb,
            )
            # Обновляем кнопку навигации
            kb_new = event_card_kb_preview(event_id, fav, total)
            await callback.message.edit_reply_markup(reply_markup=kb_new)
        except Exception:
            pass

        await callback.answer(f"{next_idx}/{total}")


# ==================== MESSAGES ====================

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    """Обработка /start с параметрами"""
    # Фиксируем пользователя
    await touch_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    args_raw = (command.args or "").strip()
    event_id = _extract_event_id_from_args(args_raw)

    # Если есть event_id в параметрах - открываем события
    if event_id is not None:
        if await open_event_preview(message, event_id):
            return

    # Иначе показываем главное меню
    await message.answer(
        "🎉 <b>EventsNow</b> — Добро пожаловать!\n\n"
        "Все события твоего города в одном месте\n\n"
        "👇 Выбери роль:",
        reply_markup=main_menu_kb(message.from_user.id),
        parse_mode="HTML",
    )
