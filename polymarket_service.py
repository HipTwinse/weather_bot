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
    "KMIA": ["miami", "майами", "kmia", "mia"],
    "KORD": ["chicago", "чикаго", "kord", "ord"],
    "RKSI": ["seoul", "сеул", "incheon", "rksi"],
    "RJTT": ["tokyo", "токио", "rjtt", "haneda"],
    "UHHH": ["khabarovsk", "хабаровск", "uhhh"],
    "LTAC": ["ankara", "анкара", "ltac"],
}

CITY_POLYMARKET_SLUGS = {
    "EGLC": ["london"],
    "LFPB": ["paris"],
    "LIMC": ["milan"],
    "LEMD": ["madrid"],
    "EDDM": ["munich"],
    "KJFK": ["nyc", "new-york"],
    "KMIA": ["miami"],
    "KORD": ["chicago"],
    "RKSI": ["seoul", "incheon"],
    "RJTT": ["tokyo"],
    "UHHH": ["khabarovsk"],
    "LTAC": ["ankara"],
}

# Кэш найденных событий: (icao, target_date) -> (timestamp, event_dict)
_MARKET_CACHE: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
MARKET_CACHE_TTL = 300.0  # 5 минут

# Глобальный пакетный кэш всех активных погодных маркетов: (timestamp, events_list)
_GLOBAL_WEATHER_EVENTS_CACHE: Tuple[float, List[Dict[str, Any]]] = (0.0, [])


def get_proxy_connector() -> Optional[Any]:
    """Возвращает aiohttp connector с SOCKS5-прокси Мадрида (защита от гео-блокировок)."""
    import config
    proxy_url = getattr(config, "PROXY_URL", None) or "socks5://ukaylydq:qatbdzk3txyb@64.137.96.74:6641"
    try:
        from aiohttp_socks import ProxyConnector
        return ProxyConnector.from_url(proxy_url)
    except Exception as e:
        logger.debug(f"Не удалось инициализировать ProxyConnector: {e}")
        return None


async def get_all_active_weather_events(max_age_seconds: float = 90.0) -> List[Dict[str, Any]]:
    """
    Пакетно загружает все активные погодные контракты Polymarket с глобальным кэшированием (90 сек).
    Позволяет всем городам находить стаканы котировок мгновенно за 1 общий запрос без 429 лимитов.
    """
    global _GLOBAL_WEATHER_EVENTS_CACHE
    now = datetime.now(timezone.utc).timestamp()
    ts, cached_list = _GLOBAL_WEATHER_EVENTS_CACHE
    if cached_list and (now - ts < max_age_seconds):
        return cached_list

    url = f"{POLYMARKET_GAMMA_API}?tag_slug=weather&limit=100&active=true&closed=false"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    # 1. Сначала пробуем через мадридский прокси, при сетевом сбое — напрямую
    for use_proxy in (True, False):
        connector = get_proxy_connector() if use_proxy else None
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list) and len(data) > 0:
                            _GLOBAL_WEATHER_EVENTS_CACHE = (now, data)
                            logger.debug(f"Успешно загружено {len(data)} погодных маркетов Polymarket (proxy={use_proxy}).")
                            return data
        except Exception as e:
            logger.debug(f"Опрос каталога Polymarket tag_slug=weather (proxy={use_proxy}) не удался: {e}")

    return cached_list or []


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
    connector = get_proxy_connector()

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
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
    1. Проверка локального кэша города.
    2. Мгновенный поиск в пакетном каталоге tag_slug=weather (без спама сетевыми запросами).
    3. Точечный опрос по 1-2 сгенерированным слагам только в случае отсутствия в общем списке.
    """
    now = datetime.now(timezone.utc).timestamp()
    cache_key = (icao, target_date or "")
    if cache_key in _MARKET_CACHE:
        ts, cached_event = _MARKET_CACHE[cache_key]
        if now - ts < MARKET_CACHE_TTL:
            return cached_event

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

    # 1. Поиск в пакетном каталоге актуальных погодных событий (быстро: 0-1 мс)
    try:
        all_weather_events = await get_all_active_weather_events()
        if all_weather_events:
            keywords = CITY_SEARCH_KEYWORDS.get(icao, [icao.lower()])
            
            # Приоритет 1: Совпадение по городу и дате (строго температурные маркеты)
            for event in all_weather_events:
                title = (event.get("title") or "").lower()
                slug = (event.get("slug") or "").lower()
                if "temperature" not in title and "temperature" not in slug and "temp" not in slug:
                    continue
                full_text = f"{title} {slug}"
                if any(kw in full_text for kw in keywords):
                    if any(str(d.day) in title and d.strftime("%B").lower() in title for d in dates_to_check):
                        if event.get("markets"):
                            _MARKET_CACHE[cache_key] = (now, event)
                            return event

            # Приоритет 2: Ближайший температурный контракт города
            for event in all_weather_events:
                title = (event.get("title") or "").lower()
                slug = (event.get("slug") or "").lower()
                if "temperature" not in title and "temperature" not in slug and "temp" not in slug:
                    continue
                full_text = f"{title} {slug}"
                if any(kw in full_text for kw in keywords):
                    if event.get("markets"):
                        _MARKET_CACHE[cache_key] = (now, event)
                        return event
    except Exception as e_cat:
        logger.debug(f"Поиск в каталоге для {icao} завершился с предупреждением: {e_cat}")

    # 2. Точечный fallback по наиболее вероятным слагам (максимум 2 запроса с таймаутом 2.5 сек)
    slug_names = CITY_POLYMARKET_SLUGS.get(icao, [icao.lower()])
    primary_date = dates_to_check[0] if dates_to_check else datetime.now(timezone.utc)
    month = primary_date.strftime("%B").lower()
    day = str(primary_date.day)
    year = str(primary_date.year)
    candidate_slugs = [
        f"highest-temperature-in-{slug_names[0]}-on-{month}-{day}-{year}",
        f"highest-temperature-in-{slug_names[0]}-on-{month}-{day}",
    ]

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    connector = get_proxy_connector()
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            for slug in candidate_slugs:
                url = f"{POLYMARKET_GAMMA_API}?slug={slug}"
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if isinstance(data, list) and len(data) > 0:
                                ev = data[0]
                                if ev.get("markets"):
                                    _MARKET_CACHE[cache_key] = (now, ev)
                                    return ev
                except Exception:
                    continue
    except Exception as e:
        logger.debug(f"Точечный опрос слагов Polymarket для {icao}: {e}")

    return None


def parse_us_temp_c(title: str) -> Optional[float]:
    """
    Конвертирует страйк в градусах Фаренгейта (°F) американских маркетов Polymarket
    в эквивалентное значение в градусах Цельсия (°C).
    Примеры:
    '62-63°F' -> 16.9°C
    '51°F or below' -> 10.6°C
    '70°F or higher' -> 21.1°C
    """
    m_range = re.search(r"(\d+)\s*[-–—]\s*(\d+)\s*°?F", title, re.IGNORECASE)
    if m_range:
        low_f, high_f = float(m_range.group(1)), float(m_range.group(2))
        mid_f = (low_f + high_f) / 2.0
        return round((mid_f - 32.0) * 5.0 / 9.0, 1)

    m_below = re.search(r"(\d+)\s*°?F\s+or\s+below", title, re.IGNORECASE)
    if m_below:
        val_f = float(m_below.group(1))
        return round((val_f - 32.0) * 5.0 / 9.0, 1)

    m_higher = re.search(r"(\d+)\s*°?F\s+or\s+(?:higher|above)", title, re.IGNORECASE)
    if m_higher:
        val_f = float(m_higher.group(1))
        return round((val_f - 32.0) * 5.0 / 9.0, 1)

    return None


def parse_markets_orderbook(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Разбирает массив рынков/исходов из события Polymarket в структурированный стакан.
    Корректно поддерживает европейские рынки (°C) и американские (°F).
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

        raw_temp_val = extract_temp_value(title)
        is_fahrenheit = bool(re.search(r"°?F\b", title, re.IGNORECASE))
        if is_fahrenheit:
            c_val = parse_us_temp_c(title)
            temp_val = c_val if c_val is not None else raw_temp_val
        else:
            temp_val = raw_temp_val

        price_cents = round(yes_price * 100, 1)

        clob_ids_raw = item.get("clobTokenIds")
        yes_token_id = ""
        if clob_ids_raw:
            try:
                clob_ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
                if clob_ids and len(clob_ids) > 0:
                    yes_token_id = str(clob_ids[0])
            except Exception:
                pass

        parsed.append({
            "title": title,
            "temp": temp_val,
            "raw_temp": raw_temp_val,
            "is_fahrenheit": is_fahrenheit,
            "yes_price": yes_price,
            "price_cents": price_cents,
            "market_id": item.get("id"),
            "token_id": yes_token_id,
        })

    # Сортируем по возрастанию температуры, если есть числовые значения
    parsed.sort(key=lambda x: x["temp"] if x["temp"] is not None else 999.0)
    return parsed


def get_current_outcome_price(orderbook: List[Dict[str, Any]], target_outcome_str: str) -> Optional[float]:
    """
    Ищет текущую цену в стакане для заданного пользователем исхода.
    Сначала ищет по точному совпадению названия, затем по температуре (°C и °F).
    """
    clean_target = target_outcome_str.strip().lower()
    for item in orderbook:
        if item.get("title", "").strip().lower() == clean_target:
            return item["price_cents"]

    target_num = extract_temp_value(target_outcome_str)
    if target_num is None:
        return None

    for item in orderbook:
        for t_key in ("temp", "raw_temp"):
            val = item.get(t_key)
            if val is not None and abs(val - target_num) < 0.6:
                return item["price_cents"]

    return None


def get_outcome_token_id(orderbook: List[Dict[str, Any]], target_outcome_str: str) -> Optional[str]:
    """
    Ищет token_id в разобранном стакане для заданного исхода.
    Сначала ищет по точному совпадению названия, затем по температуре (°C и °F).
    """
    clean_target = target_outcome_str.strip().lower()
    for item in orderbook:
        if item.get("title", "").strip().lower() == clean_target:
            return item.get("token_id")

    target_num = extract_temp_value(target_outcome_str)
    if target_num is None:
        return None

    for item in orderbook:
        for t_key in ("temp", "raw_temp"):
            val = item.get(t_key)
            if val is not None and abs(val - target_num) < 0.6:
                return item.get("token_id")

    return None


async def fetch_clob_token_price(token_id: str) -> Optional[float]:
    """
    Запрашивает актуальную цену продажи (Best Bid / Sell Price) токена напрямую с Polymarket CLOB.
    Возвращает цену в центах (0.0..100.0) или None при недоступности.
    """
    if not token_id:
        return None
    url = f"https://clob.polymarket.com/price?token_id={token_id}&side=sell"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw_price = data.get("price")
                    if raw_price is not None:
                        return round(float(raw_price) * 100.0, 1)
    except Exception as e:
        logger.debug(f"Ошибка получения прямой цены CLOB для {token_id}: {e}")
    return None

