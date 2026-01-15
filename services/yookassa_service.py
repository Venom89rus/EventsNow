from __future__ import annotations

import base64
import json
import os
import ssl
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Tuple

import aiohttp
import certifi


class YooKassaError(RuntimeError):
    pass


@dataclass(frozen=True)
class YooKassaConfig:
    shop_id: str
    secret_key: str
    api_base: str = "https://api.yookassa.ru/v3"


def basic_auth_header(shop_id: str, secret_key: str) -> str:
    token = f"{shop_id}:{secret_key}".encode("utf-8")
    return "Basic " + base64.b64encode(token).decode("ascii")


def load_yookassa_config_from_env() -> YooKassaConfig:
    shop_id = (os.getenv("YOOKASSA_SHOP_ID") or "").strip()
    secret_key = (os.getenv("YOOKASSA_SECRET_KEY") or "").strip()
    if not shop_id or not secret_key:
        raise YooKassaError("YOOKASSA_SHOP_ID/YOOKASSA_SECRET_KEY are not set")
    return YooKassaConfig(shop_id=shop_id, secret_key=secret_key)


async def create_payment(
    *,
    amount_rub: float,
    description: str,
    return_url: str,
    metadata: Optional[dict[str, Any]] = None,
    idempotence_key: Optional[str] = None,
    capture: bool = True,
) -> Tuple[str, str]:
    """
    Returns: (payment_id, confirmation_url).
    Uses confirmation.type=redirect -> confirmation_url.
    Note: Use Idempotence-Key to avoid duplicates on retries.
    """
    cfg = load_yookassa_config_from_env()

    value = f"{float(amount_rub):.2f}"
    payload: dict[str, Any] = {
        "amount": {"value": value, "currency": "RUB"},
        "confirmation": {"type": "redirect", "return_url": return_url},
        "capture": bool(capture),
        "description": (description or "")[:128],
    }
    if metadata:
        payload["metadata"] = metadata

    if not idempotence_key:
        idempotence_key = str(uuid.uuid4())

    headers = {
        "Authorization": basic_auth_header(cfg.shop_id, cfg.secret_key),
        "Idempotence-Key": idempotence_key,
        "Content-Type": "application/json",
    }

    url = f"{cfg.api_base}/payments"

    # FIX: force aiohttp to use certifi CA bundle on macOS/python.org builds
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers=headers,
            data=json.dumps(payload),
            ssl=ssl_context,
        ) as resp:
            raw = await resp.text()
            if resp.status not in (200, 201):
                raise YooKassaError(f"create_payment failed {resp.status}: {raw}")

            data = json.loads(raw)
            payment_id = data.get("id")
            confirmation_url = (
                (data.get("confirmation") or {}).get("confirmation_url")
                or data.get("confirmation_url")
            )

            if not payment_id or not confirmation_url:
                raise YooKassaError(f"Bad YooKassa response: {data}")

            return str(payment_id), str(confirmation_url)


def parse_webhook_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    Webhook payload contains:
    - event: payment.succeeded / payment.canceled / ...
    - object: payment object (id, status, metadata, ...)
    """
    event = payload.get("event")
    obj = payload.get("object") or {}

    if not event or not isinstance(obj, dict):
        raise YooKassaError("Invalid webhook payload: missing event/object")

    return str(event), obj
