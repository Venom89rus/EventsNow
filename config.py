import os
from dotenv import load_dotenv

load_dotenv()

# --------------------
# BOT CONFIG
# --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS: list[int] = [
    x for x in map(int, os.getenv("ADMIN_IDS", "").split(",")) if str(x).strip()
]
ADMINIDS = ADMIN_IDS  # алиас для обратной совместимости

# --------------------
# DATABASE
# --------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./eventsnow.db")

# --------------------
# CITIES
# --------------------
CITIES = {
    "nojabrsk": {"name": "Ноябрьск", "status": "active"},
    # "muravlenko": {"name": "Муравленко", "status": "coming_soon"},
    # "gubkinskiy": {"name": "Губкинский", "status": "coming_soon"},
    # "novy_urengoy": {"name": "Новый Уренгой", "status": "coming_soon"},
}

DEFAULT_CITY = os.getenv("DEFAULT_CITY", "nojabrsk")

# --------------------
# PRICING CONFIG
# --------------------
PRICING_CONFIG = {
    "EXHIBITION": {
        "name": "Выставка",
        "model": "period",
        "base_price_per_day": 150,
        "packages": {
            "1_day": 1799,
            # "3_days": 499,
            # "7_days": 999,
            # "14_days": 1699,
            # "30_days": 3299,
        },
    },
    "MASTERCLASS": {
        "name": "Мастер-класс",
        "model": "daily",
        "packages": {
            "1_post": 699,
            # "3_posts": 499,
            # "5_posts": 699,
            # "10_posts": 1099,
        },
    },
    "CONCERT": {
        "name": "Концерт",
        "model": "daily",
        "packages": {
            "1_post": 1499,
            # "3_posts": 3499,
            # "5_posts": 6099,
            # "10_posts": 10999,
        },
    },
    "PERFORMANCE": {
        "name": "Выступление",
        "model": "daily",
        "packages": {
            "1_day": 499,
            # "3_days": 499,
            # "7_days": 999,
            # "14_days": 1699,
            # "30_days": 3299,
        },
    },
    "LECTURE": {
        "name": "Лекция/Семинар",
        "model": "daily",
        "packages": {
            "1_post": 499,
            # "3_posts": 199,
            # "5_posts": 299,
            # "10_posts": 549,
        },
    },
    "OTHER": {
        "name": "Другое",
        "model": "daily",
        "packages": {
            "1_day": 599,
            # "3_days": 499,
            # "7_days": 999,
            # "14_days": 1699,
            # "30_days": 3299,
        },
    },
}

# TEXT PREVIEW (для коллапса)
PREVIEW_LENGTH = 150  # символов для превью описания

# --------------------
# PAYMENTS (switch + YooKassa)
# --------------------
PAYMENTS_REAL_ENABLED = os.getenv("PAYMENTS_REAL_ENABLED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "").strip()

# Куда вернуть пользователя после оплаты (если не задано — используем PUBLIC_BASE_URL)
YOOKASSA_RETURN_URL = os.getenv("YOOKASSA_RETURN_URL", "").strip()

# Внутренний порт вебхука (FastAPI) — удобно для Timeweb + nginx proxy_pass
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8000"))
