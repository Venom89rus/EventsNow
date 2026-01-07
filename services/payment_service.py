"""
Сервис расчёта цен для EventsNow
Запуск: python services/payment_service.py
"""

import os
import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from datetime import date
from typing import Dict, Any
from config import PRICING_CONFIG
import re

class PricingError(Exception):
    """Ошибка ценообразования"""
    pass


def calculate_price(category: str, num_posts: int = None, start_date: date = None, end_date: date = None) -> Dict[
    str, Any]:
    """
    Главная функция расчёта цены размещения события

    Args:
        category: EXHIBITION, MASTERCLASS, CONCERT и т.д.
        num_posts: для daily событий (кол-во постов)
        start_date/end_date: для period событий (выставки)

    Returns:
        Словарь с package_name, price, num_items/num_days
    """
    config = PRICING_CONFIG.get(category)
    if not config:
        raise PricingError(f"Категория '{category}' не найдена в PRICING_CONFIG")

    model = config["model"]

    if model == "daily":
        if num_posts is None or num_posts < 1:
            raise PricingError("Для 'daily' модели (концерт, мастер-класс) нужен num_posts >= 1")
        return _calculate_daily_price(config, num_posts)

    elif model == "period":
        if start_date is None or end_date is None:
            raise PricingError("Для 'period' модели (выставка) нужны start_date и end_date")
        if start_date > end_date:
            raise PricingError("start_date не может быть позже end_date")
        return _calculate_period_price(config, start_date, end_date)

    raise PricingError(f"Неизвестная модель ценообразования: {model}")


def _extract_int_prefix(key: str) -> int:
    """
    Понимает ключи типа:
      '1_post', '10_posts', '30_posts'
      '1', '10', '30'
    """
    m = re.match(r"^\s*(\d+)", str(key))
    if not m:
        raise ValueError(f"Bad package key: {key}")
    return int(m.group(1))


def _calculate_daily_price(config: Dict[str, Any], num_posts: int) -> Dict[str, Any]:
    if num_posts <= 0:
        raise ValueError("num_posts must be > 0")

    packages = config.get("packages") or {}
    if not packages:
        raise ValueError("No daily packages configured")

    limits = []
    for key in packages.keys():
        n = _extract_int_prefix(key)   # 1 / 10 / 30 ...
        limits.append((n, key))
    limits.sort(key=lambda x: x[0])    # ascending

    for limit_posts, key in limits:
        if num_posts <= limit_posts:
            return {
                "package_name": key,
                "price": packages[key],
                "num_items": num_posts,
                "model": "daily",
                "total_price": packages[key],
            }

    # если постов больше максимального пакета — считаем по базовой цене (опционально)
    base_price = config.get("base_price_per_item")
    if base_price is None:
        # fallback: умножаем цену самого большого пакета пропорционально
        max_limit, max_key = limits[-1]
        unit = packages[max_key] / max_limit
        total = round(unit * num_posts)
    else:
        total = round(base_price * num_posts)

    return {
        "package_name": f"custom_{num_posts}_posts",
        "price": total,
        "num_items": num_posts,
        "model": "daily",
        "total_price": total,
    }

def _calculate_period_price(config: Dict, start_date: date, end_date: date) -> Dict[str, Any]:
    num_days = (end_date - start_date).days + 1
    packages = config["packages"]

    # packages keys: "1_day", "7_days", "15_days", "30_days"
    limits = []
    for key in packages.keys():
        n = int(key.split("_")[0])   # 1 / 7 / 15 / 30
        limits.append((n, key))
    limits.sort(key=lambda x: x[0])  # ascending

    for limit_days, key in limits:
        if num_days <= limit_days:
            return {
                "package_name": key,
                "price": packages[key],
                "num_days": num_days,
                "model": "period",
                "total_price": packages[key]
            }

    # если больше максимального пакета — кастом по базе
    base_price = config.get("base_price_per_day", 150)
    total_price = base_price * num_days * 0.85
    return {
        "package_name": f"custom_{num_days}d",
        "price": total_price,
        "num_days": num_days,
        "model": "period",
        "total_price": total_price
    }

if __name__ == "__main__":
    print("🧮 EventsNow — Тестируем расчёт цен:")
    print("=" * 50)

    # 1. Концерт на 3 дня
    try:
        concert = calculate_price("CONCERT", num_posts=3)
        print("🎸 Концерт (3 поста):", concert)
    except PricingError as e:
        print(f"❌ Ошибка: {e}")

    print()

    # 2. Выставка на 8 дней
    try:
        exhibition = calculate_price("EXHIBITION",
                                     start_date=date(2026, 1, 15),
                                     end_date=date(2026, 1, 22))
        print("🎨 Выставка (8 дней):", exhibition)
    except PricingError as e:
        print(f"❌ Ошибка: {e}")

    print("\n✅ Тестирование завершено!")
