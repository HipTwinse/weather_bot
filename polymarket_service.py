"""
Сервис взаимодействия с Polymarket Gamma API (Orderbook & Market Finder).
Отвечает за поиск активных контрактов по городам, чтение стакана цен,
сопоставление исходов и расчет торговых протоколов (Sniper Momentum / Momentum Cashout).
"""

import aiohttp
import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("PolymarketService")

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com/events"

CITY_SEARCH_KEYWORDS = {
    "EGLC": ["london", "лондон", "eglc", "city airport"],
    "LFPB": ["paris", "париж", "lfpb", "bourget"],
    "LIMC": ["milan", "милан", "limc", "malpensa"],
    "LEMD": ["madrid", "мадрид", "lemd", "barajas"],
    "EDDM": ["munich", "мюнхен", "eddm"],
    "KJFK": ["new york", "нью-йорк", "jfk", "kjfk"],
    "RKSI": ["seoul", "сеул", "incheon", "rksi"],
}


def extract_temp_value(text: str) -> Optional[float]:
    """Извлекает числовое значение температуры из названия исхода (например: '23°C', '82F')."""
    match = re.search(r"(-?\d+(?:\.\d+)?)", text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


async def fetch_event_by_id_or_slug(param_type: str, param_val: str) -> Optional[Dict[str, Any]]:
    """Запрашивает конкретное событие по ID или Slug."""
    url = f"{POLYMARKET_GAMMA_API}?{param_type}={param_val}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if isinstance(data, list) and len(data) > 0:
                    return data[0]
                elif isinstance(data, dict) and "markets" in data:
                    return data
                return None
    except Exception as e:
        logger.warning(f"Ошибка получения события {param_val}: {e}")
        return None


async def find_city_weather_event(icao: str, target_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Ищет активный погодный маркет Polymarket по ICAO коду или ключевым словам города.
    """
    keywords = CITY_SEARCH_KEYWORDS.get(icao, [icao.lower()])
    url = f"{POLYMARKET_GAMMA_API}?limit=50&active=true&closed=false"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if not isinstance(data, list):
                    return None

                for event in data:
                    title = (event.get("title") or "").lower()
                    slug = (event.get("slug") or "").lower()
                    desc = (event.get("description") or "").lower()
                    full_text = f"{title} {slug} {desc}"

                    # Проверяем совпадение по городу
                    if any(kw in full_text for kw in keywords):
                        # Если задана дата, проверяем совпадение даты
                        if target_date and target_date not in full_text:
                            continue
                        return event

                # Вторая попытка: без фильтра даты, если с датой не найдено
                if target_date:
                    for event in data:
                        title = (event.get("title") or "").lower()
                        slug = (event.get("slug") or "").lower()
                        full_text = f"{title} {slug}"
                        if any(kw in full_text for kw in keywords):
                            return event

                return None
    except Exception as e:
        logger.warning(f"Сбой поиска маркета для {icao}: {e}")
        return None


def parse_markets_orderbook(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Разбирает массив рынков/исходов из события Polymarket в структурированный стакан.
    """
    parsed = []
    for item in markets:
        title = item.get("groupItemTitle") or item.get("question", "Исход")
        prices_raw = item.get("outcomePrices", '["0", "0"]')

        try:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            yes_price = float(prices[0]) if len(prices) > 0 else 0.0
        except Exception:
            yes_price = 0.0

        temp_val = extract_temp_value(title)
        price_cents = round(yes_price * 100, 1)

        parsed.append({
            "title": title,
            "temp": temp_val,
            "yes_price": yes_price,
            "price_cents": price_cents,
            "market_id": item.get("id"),
        })

    # Сортируем по возрастанию температуры, если есть числовые значения
    parsed.sort(key=lambda x: x["temp"] if x["temp"] is not None else 999.0)
    return parsed


def get_current_outcome_price(orderbook: List[Dict[str, Any]], target_outcome_str: str) -> Optional[float]:
    """
    Ищет текущую цену в стакане для заданного пользователем исхода.
    """
    target_num = extract_temp_value(target_outcome_str)
    if target_num is None:
        return None

    for item in orderbook:
        if item["temp"] is not None and abs(item["temp"] - target_num) < 0.3:
            return item["price_cents"]

    return None
