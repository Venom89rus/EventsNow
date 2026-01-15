# handlers/yookassa_webhook.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from database.session import get_db
from database.models import Payment, PaymentStatus, Event, EventStatus
from services.yookassa_service import parse_webhook
from services.notify_service import notify_new_event_published

router = APIRouter()


def _as_float_amount(payment_obj: dict[str, Any]) -> Optional[float]:
    try:
        amt = (payment_obj.get("amount") or {}).get("value")
        if amt is None:
            return None
        return float(amt)
    except Exception:
        return None


@router.post("/yookassa/webhook")
async def yookassa_webhook(request: Request):
    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)

    try:
        event_type, payment_obj = parse_webhook(payload)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    yk_payment_id = str(payment_obj.get("id") or "").strip()
    yk_status = str(payment_obj.get("status") or "").strip()

    if not yk_payment_id:
        return JSONResponse({"ok": False, "error": "missing_payment_id"}, status_code=400)

    async with get_db() as db:
        payment = (
            (await db.execute(select(Payment).where(Payment.transaction_id == yk_payment_id)))
            .scalar_one_or_none()
        )

        # Если не нашли — отвечаем 200, чтобы YooKassa не ретраила 24 часа.
        if not payment:
            return JSONResponse({"ok": True, "ignored": True})

        # Уточним сумму (полезно для сверки в логах/будущей аналитики)
        amount = _as_float_amount(payment_obj)
        if amount is not None and (payment.amount is None or payment.amount == 0):
            payment.amount = amount

        # SUCCESS
        if event_type == "payment.succeeded" or yk_status == "succeeded":
            # Идемпотентность: если уже completed — просто 200 OK
            if payment.status != PaymentStatus.COMPLETED:
                payment.status = PaymentStatus.COMPLETED
                payment.completed_at = datetime.utcnow()

            if payment.event_id:
                ev = (
                    (await db.execute(select(Event).where(Event.id == payment.event_id)))
                    .scalar_one_or_none()
                )
                if ev:
                    # Если уже ACTIVE — не трогаем и не шлём повторно
                    if ev.status != EventStatus.ACTIVE:
                        ev.payment_status = PaymentStatus.COMPLETED
                        ev.status = EventStatus.ACTIVE

                        # Здесь нужен доступ к bot (см. ниже “что дописать в app.py”)
                        bot = getattr(request.app.state, "bot", None)
                        if bot is not None:
                            try:
                                await notify_new_event_published(bot, ev.id)
                            except Exception:
                                pass

        # CANCELED / FAILED
        elif event_type == "payment.canceled" or yk_status == "canceled":
            payment.status = PaymentStatus.CANCELLED

        return JSONResponse({"ok": True})
