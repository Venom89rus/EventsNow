from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Router, F
from aiogram.filters import Command, CommandObject
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

# pending confirm for /cleanup
_PENDING: dict[int, dict] = {}  # user_id -> {"mode": "2h|24h|all", "hours": int|None}


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
    # локальная копия, чтобы не импортить admin_handler.py и не ловить циклы
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
    dt_from = datetime.utcnow() - timedelta(hours=hours)

    async with get_db() as db:
        events_cnt = (
            await db.execute(select(func.count()).select_from(Event).where(Event.created_at >= dt_from))
        ).scalar_one() or 0

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


async def _delete_all(confirm: bool) -> tuple[int, int]:
    async with get_db() as db:
        events_cnt = (await db.execute(select(func.count()).select_from(Event))).scalar_one() or 0
        photos_cnt = (await db.execute(select(func.count()).select_from(EventPhoto))).scalar_one() or 0
        if confirm:
            await db.execute(delete(Event))
        return int(events_cnt), int(photos_cnt)


async def _show_tools_menu(message: Message) -> None:
    await message.answer("🧹 Инструменты очистки. Выбери действие:", reply_markup=tools_kb())


# -------------------------
# /cleanup command
# -------------------------
@router.message(Command("cleanup"))
async def cmd_cleanup(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return

    args = (command.args or "").strip().lower()
    uid = message.from_user.id

    # /cleanup -> меню
    if not args:
        await _show_tools_menu(message)
        return

    # cancel
    if args in {"cancel", "no", "отмена"}:
        _PENDING.pop(uid, None)
        await message.answer("Ок, отменено.", reply_markup=tools_kb())
        return

    # confirm
    if args in {"confirm", "yes", "да"}:
        pending = _PENDING.get(uid)
        if not pending:
            await message.answer(
                "Нет действия для подтверждения. Сначала: /cleanup 2h | /cleanup 24h | /cleanup all",
                reply_markup=tools_kb(),
            )
            return

        mode = pending["mode"]
        if mode in {"2h", "24h"}:
            hours = int(pending["hours"])
            n_events, n_photos, filt = await _cleanup_by_hours(hours=hours, confirm=True)
            _PENDING.pop(uid, None)
            await message.answer(
                f"✅ Удалено событий: {n_events}\n"
                f"✅ Удалено фото (каскад): {n_photos}\n"
                f"Фильтр: {filt}",
                reply_markup=tools_kb(),
            )
            return

        if mode == "all":
            n_events, n_photos = await _delete_all(confirm=True)
            _PENDING.pop(uid, None)
            await message.answer(
                f"✅ Удалено событий: {n_events}\n"
                f"✅ Удалено фото (каскад): {n_photos}",
                reply_markup=tools_kb(),
            )
            return

        await message.answer("Неизвестный режим подтверждения.", reply_markup=tools_kb())
        return

    # request delete by period / all (dry-run + require confirm)
    if args in {"2h", "2", "2ч"}:
        n_events, n_photos, filt = await _cleanup_by_hours(hours=2, confirm=False)
        _PENDING[uid] = {"mode": "2h", "hours": 2}
        await message.answer(
            "⚠️ Подтверди удаление\n\n"
            f"Удалится событий: {n_events}\n"
            f"Удалится фото (каскад): {n_photos}\n"
            f"Фильтр: {filt}\n\n"
            "Подтвердить: /cleanup confirm\n"
            "Отмена: /cleanup cancel",
            reply_markup=tools_kb(),
        )
        return

    if args in {"24h", "24", "24ч"}:
        n_events, n_photos, filt = await _cleanup_by_hours(hours=24, confirm=False)
        _PENDING[uid] = {"mode": "24h", "hours": 24}
        await message.answer(
            "⚠️ Подтверди удаление\n\n"
            f"Удалится событий: {n_events}\n"
            f"Удалится фото (каскад): {n_photos}\n"
            f"Фильтр: {filt}\n\n"
            "Подтвердить: /cleanup confirm\n"
            "Отмена: /cleanup cancel",
            reply_markup=tools_kb(),
        )
        return

    if args in {"all", "все", "all_events"}:
        events_cnt, photos_cnt = await _delete_all(confirm=False)
        _PENDING[uid] = {"mode": "all", "hours": None}
        await message.answer(
            "⚠️ ОПАСНО: удаление ВСЕХ событий\n\n"
            f"Событий в базе: {events_cnt}\n"
            f"Фото событий: {photos_cnt}\n\n"
            "Подтвердить: /cleanup confirm\n"
            "Отмена: /cleanup cancel",
            reply_markup=tools_kb(),
        )
        return

    await message.answer(
        "Не понял аргумент. Используй: /cleanup, /cleanup 2h, /cleanup 24h, /cleanup all.",
        reply_markup=tools_kb(),
    )


# -------------------------
# Existing button handlers
# -------------------------

@router.message(F.text == BTN_TOOLS)
async def tools_entry_button(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    await _show_tools_menu(message)


@router.message(F.text == BTN_BACK_ADMIN)
async def tools_back_to_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Нет доступа")
        return
    await message.answer("🛡 Админ-панель:", reply_markup=admin_panel_kb_local())


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
    n_events, n_photos = await _delete_all(confirm=True)
    await message.answer(
        f"✅ Удалено событий: {n_events}\n"
        f"✅ Удалено фото (каскад): {n_photos}",
        reply_markup=tools_kb(),
    )
