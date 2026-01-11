from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from sqlalchemy import select, delete, func

from database.session import get_db
from database.models import Event, EventPhoto

# --- admin ids: поддерживаем оба варианта имени ---
try:
    from config import ADMIN_IDS as _ADMIN_IDS  # type: ignore
except Exception:
    _ADMIN_IDS = None

try:
    from config import ADMINIDS as _ADMINIDS  # type: ignore
except Exception:
    _ADMINIDS = None

ADMIN_IDS = list(_ADMIN_IDS or _ADMINIDS or [])

router = Router()

# --- UI texts ---
BTN_TOOLS = "🧹 Очистка теста"
BTN_DRYRUN_2H = "🔎 Проверить (2ч)"
BTN_DELETE_2H = "🗑 Удалить (2ч)"
BTN_DRYRUN_24H = "🔎 Проверить (24ч)"
BTN_DELETE_24H = "🗑 Удалить (24ч)"

BTN_DELETE_ALL = "🧨 Удалить ВСЕ события"
BTN_CONFIRM_DELETE_ALL = "✅ Да, удалить ВСЕ события"
BTN_CANCEL_DELETE_ALL = "❎ Отмена"

BTN_BACK_ADMIN = "⬅️ В админ-панель"


def is_admin(user_id: int) -> bool:
    return user_id in (ADMIN_IDS or [])


def tools_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DRYRUN_2H), KeyboardButton(text=BTN_DELETE_2H)],
            [KeyboardButton(text=BTN_DRYRUN_24H), KeyboardButton(text=BTN_DELETE_24H)],
            [KeyboardButton(text=BTN_DELETE_ALL)],
            [KeyboardButton(text=BTN_BACK_ADMIN)],
        ],
        resize_keyboard=True,
    )


def confirm_delete_all_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CONFIRM_DELETE_ALL)],
            [KeyboardButton(text=BTN_CANCEL_DELETE_ALL)],
        ],
        resize_keyboard=True,
    )


def admin_panel_kb_local() -> ReplyKeyboardMarkup:
    """
    Локальная копия клавиатуры админки (чтобы не импортировать admin_handler.py и не ловить циклы).
    Под твой скрин: События на модерацию / Статистика / Пользователи / Финансы / Назад.
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🗂 События на модерацию"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="👥 Пользователи"), KeyboardButton(text="💰 Финансы")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


async def _count_all() -> tuple[int, int]:
    async with get_db() as db:
        events_cnt = (await db.execute(select(func.count()).select_from(Event))).scalar_one() or 0
        photos_cnt = (await db.execute(select(func.count()).select_from(EventPhoto))).scalar_one() or 0
    return int(events_cnt), int(photos_cnt)


async def _cleanup_by_hours(hours: int, confirm: bool) -> tuple[int, int, str]:
    """
    Удаляет события за последние N часов.
    Возвращает: (events_deleted, photos_deleted_estimate, filter_text)
    """
    dt_from = datetime.utcnow() - timedelta(hours=hours)

    async with get_db() as db:
        events_cnt = (
            await db.execute(
                select(func.count()).select_from(Event).where(Event.created_at >= dt_from)
            )
        ).scalar_one() or 0

        # Оценка по фото: считаем фото у событий, попадающих под условие.
        photos_cnt = (
            await db.execute(
                select(func.count())
                .select_from(EventPhoto)
                .join(Event, Event.id == EventPhoto.event_id)
                .where(Event.created_at >= dt_from)
            )
        ).scalar_one() or 0

        if confirm:
            await db.execute(delete(Event).where(Event.created_at >= dt_from))

    filt = f"created_at >= now_utc - {hours}h"
    return int(events_cnt), int(photos_cnt), filt


# --- entry point ---
@router.message(F.text.in_({BTN_TOOLS, "/cleanup"}))
async def tools_entry(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    await message.answer("🧹 Инструменты очистки. Выбери действие:", reply_markup=tools_kb())


@router.message(F.text == BTN_BACK_ADMIN)
async def tools_back_to_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    await message.answer("🛡 Админ-панель:", reply_markup=admin_panel_kb_local())


# --- 2h / 24h actions ---
@router.message(F.text == BTN_DRYRUN_2H)
async def dryrun_2h(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    n_events, n_photos, filt = await _cleanup_by_hours(hours=2, confirm=False)
    await message.answer(
        "DRY-RUN (ничего не удалено)\n\n"
        f"Удалится событий: {n_events}\n"
        f"Удалится фото (каскад): {n_photos}\n"
        f"Фильтр: {filt}",
        reply_markup=tools_kb(),
    )


@router.message(F.text == BTN_DELETE_2H)
async def delete_2h(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    n_events, n_photos, filt = await _cleanup_by_hours(hours=2, confirm=True)
    await message.answer(
        f"✅ Удалено событий: {n_events}\n"
        f"✅ Удалено фото (каскад): {n_photos}\n"
        f"Фильтр: {filt}",
        reply_markup=tools_kb(),
    )


@router.message(F.text == BTN_DRYRUN_24H)
async def dryrun_24h(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    n_events, n_photos, filt = await _cleanup_by_hours(hours=24, confirm=False)
    await message.answer(
        "DRY-RUN (ничего не удалено)\n\n"
        f"Удалится событий: {n_events}\n"
        f"Удалится фото (каскад): {n_photos}\n"
        f"Фильтр: {filt}",
        reply_markup=tools_kb(),
    )


@router.message(F.text == BTN_DELETE_24H)
async def delete_24h(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    n_events, n_photos, filt = await _cleanup_by_hours(hours=24, confirm=True)
    await message.answer(
        f"✅ Удалено событий: {n_events}\n"
        f"✅ Удалено фото (каскад): {n_photos}\n"
        f"Фильтр: {filt}",
        reply_markup=tools_kb(),
    )


# --- delete all: start/confirm/cancel ---
@router.message(F.text == BTN_DELETE_ALL)
async def delete_all_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    events_cnt, photos_cnt = await _count_all()
    await message.answer(
        "⚠️ ОПАСНО: удаление ВСЕХ событий\n\n"
        f"Событий в базе: {events_cnt}\n"
        f"Фото событий: {photos_cnt}\n\n"
        "Это действие необратимо.\n"
        "Подтвердите удаление:",
        reply_markup=confirm_delete_all_kb(),
    )


@router.message(F.text == BTN_CANCEL_DELETE_ALL)
async def delete_all_cancel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    await message.answer("Ок, отменено.", reply_markup=tools_kb())


@router.message(F.text == BTN_CONFIRM_DELETE_ALL)
async def delete_all_confirm(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    async with get_db() as db:
        events_cnt = (await db.execute(select(func.count()).select_from(Event))).scalar_one() or 0
        photos_cnt = (
            await db.execute(select(func.count()).select_from(EventPhoto))
        ).scalar_one() or 0

        # Удаляем только events — фото уйдут каскадно (FK ondelete + relationship cascade)
        await db.execute(delete(Event))

    await message.answer(
        f"✅ Удалено событий: {int(events_cnt)}\n"
        f"✅ Удалено фото (каскад): {int(photos_cnt)}",
        reply_markup=tools_kb(),
    )
