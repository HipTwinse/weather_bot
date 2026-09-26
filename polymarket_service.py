"""
Сервис взаимодействия с Polymarket Gamma API (Orderbook & Market Finder).
Отвечает за поиск активных контрактов по городам, чтение стакана цен,
сопоставление исходов и расчет торговых протоколов (Sniper Momentum / Momentum Cashout).
"""

import aiohttp
import asyncio
from datetime import datetime, timedelta, timezone
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
    "KJFK": ["new york", "нью-йорк", "jfk", "kjfk", "nyc"],
    "RKSI": ["seoul", "сеул", "incheon", "rksi"],
    "LTAC": ["ankara", "анкара", "ltac"],
}

CITY_POLYMARKET_SLUGS = {
    "EGLC": ["london"],
    "LFPB": ["paris"],
    "LIMC": ["milan"],
    "LEMD": ["madrid"],
    "EDDM": ["munich"],
    "KJFK": ["nyc", "new-york"],
    "RKSI": ["seoul", "incheon"],
    "LTAC": ["ankara"],
}

# Кэш найденных событий: (icao, target_date) -> (timestamp, event_dict)
_MARKET_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
MARKET_CACHE_TTL = 300.0  # 5 минут


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
    Ищет активный погодный маркет Polymarket по ICAO коду города и дате:
    1. Точный опрос по предсказанному слагу (highest-temperature-in-{city}-on-{month}-{day}-{year}).
    2. Поиск по названию и ключевым словам среди всех актуальных погодных маркетов (tag_slug=weather).
    """
    now = datetime.now(timezone.utc).timestamp()
    cache_key = (icao, target_date or "")
    if cache_key in _MARKET_CACHE:
        ts, cached_event = _MARKET_CACHE[cache_key]
        if now - ts < MARKET_CACHE_TTL:
            return cached_event

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # 1. Построение списка возможных слагов на основе даты
    slug_names = CITY_POLYMARKET_SLUGS.get(icao, [icao.lower()])

    dates_to_check: List[datetime] = []
    if target_date:
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d")
            dates_to_check = [dt, dt + timedelta(days=1), dt - timedelta(days=1)]
        except Exception:
            dates_to_check = [datetime.now(timezone.utc)]
    else:
        dt_now = datetime.now(timezone.utc)
        dates_to_check = [dt_now, dt_now + timedelta(days=1)]

    candidate_slugs: List[str] = []
    for d in dates_to_check:
        month = d.strftime("%B").lower()
        day = str(d.day)
        year = str(d.year)
        for c in slug_names:
            candidate_slugs.append(f"highest-temperature-in-{c}-on-{month}-{day}-{year}")
            candidate_slugs.append(f"highest-temperature-in-{c}-on-{month}-{day}")

    try:
        async with aiohttp.ClientSession() as session:
            # Стратегия 1: Прямой поиск по сгенерированному слагу (быстро: 50-100 мс)
            for slug in candidate_slugs:
                url = f"{POLYMARKET_GAMMA_API}?slug={slug}"
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and len(data) > 0:
                                ev = data[0]
                                if ev.get("markets"):
                                    _MARKET_CACHE[cache_key] = (now, ev)
                                    return ev
                except Exception:
                    continue

            # Стратегия 2: Поиск по названию среди актуальных погодных рынков (tag_slug=weather)
            weather_url = f"{POLYMARKET_GAMMA_API}?tag_slug=weather&limit=150&active=true&closed=false"
            try:
                async with session.get(weather_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            keywords = CITY_SEARCH_KEYWORDS.get(icao, [icao.lower()])
                            # Приоритет 1: Совпадение по городу и дате
                            for event in data:
                                title = (event.get("title") or "").lower()
                                slug = (event.get("slug") or "").lower()
                                full_text = f"{title} {slug}"

                                if any(kw in full_text for kw in keywords):
                                    if any(str(d.day) in title and d.strftime("%B").lower() in title for d in dates_to_check):
                                        _MARKET_CACHE[cache_key] = (now, event)
                                        return event

                            # Приоритет 2: Ближайший температурный контракт города
                            for event in data:
                                title = (event.get("title") or "").lower()
                                if any(kw in title for kw in keywords) and "highest temperature" in title:
                                    _MARKET_CACHE[cache_key] = (now, event)
                                    return event
            except Exception as e:
                logger.warning(f"Ошибка поиска по tag_slug=weather: {e}")

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
