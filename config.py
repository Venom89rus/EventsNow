import os
from dotenv import load_dotenv
from enum import Enum

load_dotenv()

# BOT CONFIG
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(",")))

# DATABASE
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./eventsnow.db")

# CITIES
CITIES = {
    "nojabrsk": {"name": "Ноябрьск", "status": "active"},
    "muravlenko": {"name": "Муравленко", "status": "coming_soon"},
    "gubkinskiy": {"name": "Губкинский", "status": "coming_soon"},
    "novy_urengoy": {"name": "Новый Уренгой", "status": "coming_soon"},
}

DEFAULT_CITY = os.getenv("DEFAULT_CITY", "nojabrsk")

# PRICING CONFIG
PRICING_CONFIG = {
    "EXHIBITION": {
        "name": "Выставка",
        "model": "period",
        "base_price_per_day": 150,
        "packages": {
            "1_day": 199,
            "3_days": 499,
            "7_days": 999,
            "14_days": 1699,
            "30_days": 3299,
        }
    },
    "MASTERCLASS": {
        "name": "Мастер-класс",
        "model": "daily",
        "packages": {
            "1_post": 199,
            "3_posts": 499,
            "5_posts": 699,
            "10_posts": 1099,
        }
    },
    "CONCERT": {
        "name": "Концерт",
        "model": "daily",
        "packages": {
            "1_post": 1499,
            "3_posts": 3499,
            "5_posts": 6099,
            "10_posts": 10999,
        }
    },
    "PERFORMANCE": {
        "name": "Выступление",
        "model": "daily",
        "packages": {
            "1_day": 199,
            "3_days": 499,
            "7_days": 999,
            "14_days": 1699,
            "30_days": 3299,
        }
    },
    "LECTURE": {
        "name": "Лекция/Семинар",
        "model": "daily",
        "packages": {
            "1_post": 79,
            "3_posts": 199,
            "5_posts": 299,
            "10_posts": 549,
        }
    },
    "OTHER": {
        "name": "Другое",
        "model": "daily",
        "packages": {
            "1_day": 199,
            "3_days": 499,
            "7_days": 999,
            "14_days": 1699,
            "30_days": 3299,
        }
    }
}

# TEXT PREVIEW (для коллапса)
PREVIEW_LENGTH = 150  # символов для превью описания
