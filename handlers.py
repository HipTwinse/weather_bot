"""
Модуль обработчиков событий Telegram-бота Weather Alpha Engine v7.1.

Реализует:
1. Команды /start, /help, /positions, /scan, /cities.
2. Инлайн-кнопки экспресс-анализа рынков Polymarket (EGLC, LFPB, LIMC, LEMD).
3. Интерактивный разбор котировок стакана, сайзинг банкролла и расчет коридоров.
4. Добавление и закрытие сделок с поддержкой цены входа (positions.db).
5. Обработку ручных ICAO-кодов и географических координат.
"""

import asyncio
from datetime import datetime
import html
import json
import time
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
import zoneinfo

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from timezonefinder import TimezoneFinder

import config
from airport_resolver import resolve_airport
from database import (
    add_position,
    delete_position,
    get_user_positions,
    save_user_wallet,
    save_user_public_wallet,
    get_user_wallet,
    delete_user_wallet,
    register_subscriber,
)
from clob_trader import (
    validate_private_key,
    get_wallet_collateral_balance,
    resolve_polymarket_proxy,
)
from noaa_service import get_noaa_package
from openmeteo_service import fetch_openmeteo_forecast
from polymarket_service import (
    find_city_weather_event,
    parse_markets_orderbook,
    extract_temp_value,
    get_current_outcome_price,
    get_outcome_token_id,
    fetch_event_by_id_or_slug,
    POLYMARKET_GAMMA_API,
)
from weather_synthesizer import (
    build_raw_data_package_dict,
    build_summary_caption,
    synthesize_forecast,
)
from gemini_analyzer import (
    analyze_city_weather_ai,
    is_gemini_configured,
    get_gemini_status,
    daily_tracker,
    compute_weather_delta,
)
from auto_scanner import (
    get_priority_target,
    _calculate_dynamics,
    has_real_low_cloud,
    is_blocking_rain_and_clouds,
    get_strike_for_temp,
    get_seasonal_heating_cutoff,
)

logger = logging.getLogger(__name__)
router = Router()

tf = TimezoneFinder()

# База отслеживаемых городов
ALL_RADAR_CITIES = {
    "EGLC": "🇬🇧 Лондон (EGLC)",
    "LFPB": "🇫🇷 Париж (LFPB)",
    "LIMC": "🇮🇹 Милан (LIMC)",
    "LEMD": "🇪🇸 Мадрид (LEMD)",
    "EDDM": "🇩🇪 Мюнхен (EDDM)",
    "KJFK": "🇺🇸 Нью-Йорк (KJFK)",
    "RJTT": "🇯🇵 Токио (RJTT)",
    "RKSI": "🇰🇷 Сеул (RKSI)",
    "ZSPD": "🇨🇳 Шанхай (ZSPD)",
    "LTAC": "🇹🇷 Анкара (LTAC)",
    "NZWN": "🇳🇿 Веллингтон (NZWN)",
    "UHHH": "🇷🇺 Хабаровск (UHHH)",
}


class MarketScanStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_balance = State()


class AddPositionStates(StatesGroup):
    waiting_for_city = State()
    waiting_for_outcomes = State()


main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔍 Сканировать маркет"),
            KeyboardButton(text="🤖 AI Аналитик"),
        ],
        [
            KeyboardButton(text="📌 Мои позиции"),
            KeyboardButton(text="🌍 Избранные города"),
        ],
        [
            KeyboardButton(text="📖 Справка / Команды"),
        ],
    ],
    resize_keyboard=True,
)

# Инлайн-клавиатура для команды /ai — запускает полный синоптический анализ
ai_cities_inline_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 Лондон (EGLC)", callback_data="icao:EGLC"),
            InlineKeyboardButton(text="🇫🇷 Париж (LFPB)", callback_data="icao:LFPB"),
        ],
        [
            InlineKeyboardButton(text="🇮🇹 Милан (LIMC)", callback_data="icao:LIMC"),
            InlineKeyboardButton(text="🇪🇸 Мадрид (LEMD)", callback_data="icao:LEMD"),
        ],
        [
            InlineKeyboardButton(text="🇩🇪 Мюнхен (EDDM)", callback_data="icao:EDDM"),
            InlineKeyboardButton(text="🇺🇸 Нью-Йорк (KJFK)", callback_data="icao:KJFK"),
        ],
        [
            InlineKeyboardButton(text="🇯🇵 Токио (RJTT)", callback_data="icao:RJTT"),
            InlineKeyboardButton(text="🇰🇷 Сеул (RKSI)", callback_data="icao:RKSI"),
        ],
        [
            InlineKeyboardButton(text="🇷🇺 Хабаровск (UHHH)", callback_data="icao:UHHH"),
        ],
    ]
)

cities_inline_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 Лондон (EGLC)", callback_data="icao:EGLC"),
            InlineKeyboardButton(text="🇫🇷 Париж (LFPB)", callback_data="icao:LFPB"),
        ],
        [
            InlineKeyboardButton(text="🇮🇹 Милан (LIMC)", callback_data="icao:LIMC"),
            InlineKeyboardButton(text="🇪🇸 Мадрид (LEMD)", callback_data="icao:LEMD"),
        ],
        [
            InlineKeyboardButton(text="🇩🇪 Мюнхен (EDDM)", callback_data="icao:EDDM"),
            InlineKeyboardButton(text="🇺🇸 Нью-Йорк (KJFK)", callback_data="icao:KJFK"),
        ],
        [
            InlineKeyboardButton(text="🇯🇵 Токио (RJTT)", callback_data="icao:RJTT"),
            InlineKeyboardButton(text="🇰🇷 Сеул (RKSI)", callback_data="icao:RKSI"),
        ],
        [
            InlineKeyboardButton(text="🇷🇺 Хабаровск (UHHH)", callback_data="icao:UHHH"),
        ],
    ]
)

position_city_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 Лондон (EGLC)", callback_data="pos_city:EGLC"),
            InlineKeyboardButton(text="🇫🇷 Париж (LFPB)", callback_data="pos_city:LFPB"),
        ],
        [
            InlineKeyboardButton(text="🇮🇹 Милан (LIMC)", callback_data="pos_city:LIMC"),
            InlineKeyboardButton(text="🇪🇸 Мадрид (LEMD)", callback_data="pos_city:LEMD"),
        ],
        [
            InlineKeyboardButton(text="🇩🇪 Мюнхен (EDDM)", callback_data="pos_city:EDDM"),
            InlineKeyboardButton(text="🇺🇸 Нью-Йорк (KJFK)", callback_data="pos_city:KJFK"),
        ],
    ]
)

balance_quick_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="$20 (Tier 1)", callback_data="set_bal:20"),
            InlineKeyboardButton(text="$40 (Tier 2)", callback_data="set_bal:40"),
            InlineKeyboardButton(text="$80 (Tier 3)", callback_data="set_bal:80"),
        ],
        [
            InlineKeyboardButton(text="$150 (Tier 4)", callback_data="set_bal:150"),
            InlineKeyboardButton(text="$500 (Tier 5)", callback_data="set_bal:500"),
            InlineKeyboardButton(text="$1,500 (Tier 6)", callback_data="set_bal:1500"),
        ],
    ]
)

CITY_KEYWORD_MAP = {
    "UHHH": ["khabarovsk", "хабаровск", "uhhh"],
    "EGLC": ["london", "лондон", "eglc", "city airport"],
    "LFPB": ["paris", "париж", "lfpb", "bourget"],
    "RJTT": ["tokyo", "токио", "rjtt", "haneda"],
    "RKSI": ["seoul", "сеул", "rksi", "incheon"],
    "ZSPD": ["shanghai", "шанхай", "zspd", "pudong"],
    "EDDM": ["munich", "мюнхен", "eddm"],
    "LEMD": ["madrid", "мадрид", "lemd", "barajas"],
    "LIMC": ["milan", "милан", "limc", "malpensa"],
    "KJFK": ["new york", "нью-йорк", "jfk", "kjfk"],
}

MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def calculate_tier_sizing(balance: float) -> Tuple[str, float, str]:
    """Рассчитывает сайзинг строго по Прогрессивной сетке ставок (Roadmap to $3,000)."""
    if balance < 25.0:
        return "Tier 1 ($5 – $25)", 1.00, "$2.00 – $3.00 ($1.00 на исход)"
    elif balance < 50.0:
        return "Tier 2 ($25 – $50)", 2.50, "$3.00 – $5.00"
    elif balance < 100.0:
        return "Tier 3 ($50 – $100)", 5.00, "$6.00 – $10.00"
    elif balance < 300.0:
        return "Tier 4 ($100 – $300)", 10.00, "$12.00 – $25.00"
    elif balance < 1000.0:
        return "Tier 5 ($300 – $1,000)", 30.00, "$35.00 – $80.00"
    else:
        return "Tier 6 ($1,000 – $3,000)", 100.00, "$100.00 – $250.00"


def _parse_coordinates(text: str) -> Optional[Tuple[float, float]]:
    pattern = r"^(-?\d+(?:\.\d+)?)[,\s]+(-?\d+(?:\.\d+)?)$"
    match = re.match(pattern, text.strip())
    if match:
        try:
            lat = float(match.group(1))
            lon = float(match.group(2))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return lat, lon
        except ValueError:
            return None
    return None


def _parse_market_identifier(url_or_text: str) -> Tuple[Optional[str], Optional[str]]:
    text = url_or_text.strip()
    preddy_id_match = re.search(r"preddy\.trade/event/[^/]+/(\d+)", text)
    if preddy_id_match:
        return "id", preddy_id_match.group(1)

    tma_match = re.search(r"startapp=([a-zA-Z0-9_-]+)", text)
    if tma_match:
        val = tma_match.group(1)
        return ("id" if val.isdigit() else "slug"), val

    poly_match = re.search(r"polymarket\.com/event/([a-zA-Z0-9_-]+)", text)
    if poly_match:
        return "slug", poly_match.group(1)

    clean_val = text.split("?")[0].rstrip("/").split("/")[-1]
    if clean_val.isdigit():
        return "id", clean_val
    elif clean_val:
        return "slug", clean_val

    return None, None


def _detect_city_icao(text_context: str) -> Optional[str]:
    normalized = text_context.lower()
    for icao, keywords in CITY_KEYWORD_MAP.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", normalized):
                return icao
    return None


def _detect_market_target_date(text_context: str) -> Optional[str]:
    normalized = text_context.lower()
    pattern = r"\b(january|jan|february|feb|march|mar|april|apr|may|june|jun|july|jul|august|aug|september|sep|sept|october|oct|november|nov|december|dec)\s+(\d{1,2})\b"
    match = re.search(pattern, normalized)
    if match:
        month_str = match.group(1)
        day_str = match.group(2)
        month_num = MONTH_NAMES.get(month_str)
        if month_num:
            day_num = int(day_str)
            current_year = datetime.now().year
            return f"{current_year}-{month_num:02d}-{day_num:02d}"

    iso_match = re.search(r"\b(202\d-\d{2}-\d{2})\b", normalized)
    if iso_match:
        return iso_match.group(1)

    return None


async def _collect_weather_data(user_query: str, explicit_date: Optional[str] = None):
    airport_data = await asyncio.to_thread(resolve_airport, user_query)
    if not airport_data:
        return False, None, None, f"❌ Аэропорт с кодом <code>{user_query}</code> не найден в базе данных."

    lat = airport_data.get("lat")
    lon = airport_data.get("lon")
    tz_name = airport_data.get("timezone", "UTC")

    try:
        local_tz = zoneinfo.ZoneInfo(tz_name)
        target_date_local = explicit_date if explicit_date else datetime.now(local_tz).strftime("%Y-%m-%d")
    except Exception as e:
        logger.error(f"Недопустимый часовой пояс '{tz_name}' для {user_query}: {e}")
        return False, None, None, f"⚠️ Недопустимый часовой пояс <code>{tz_name}</code>."

    async def _get_openmeteo():
        return await asyncio.to_thread(fetch_openmeteo_forecast, lat, lon, tz_name, target_date_local)

    async def _get_noaa():
        return await asyncio.to_thread(get_noaa_package, user_query)

    raw_forecast_payload, noaa_payload = await asyncio.gather(_get_openmeteo(), _get_noaa(), return_exceptions=True)

    if isinstance(raw_forecast_payload, Exception) or not isinstance(raw_forecast_payload, dict):
        raw_forecast_payload = {}
    if isinstance(noaa_payload, Exception) or not isinstance(noaa_payload, dict):
        noaa_payload = {}

    synth_result = (
        await asyncio.to_thread(synthesize_forecast, raw_forecast_payload)
        if raw_forecast_payload
        else {"success": False, "error": "Нет данных"}
    )

    summary_text = build_summary_caption(airport_data, synth_result, noaa_payload, target_date_local)
    package_dict = build_raw_data_package_dict(airport_data, raw_forecast_payload, synth_result, noaa_payload)

    json_bytes = json.dumps(package_dict, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"weather_package_{user_query}_{target_date_local}.json"
    document_file = BufferedInputFile(file=json_bytes, filename=filename)

    return True, summary_text, document_file, None


# -------------------------------------------------------------
# КНОПКИ ЭКСПРЕСС-АНАЛИЗА (EXPRESS SCAN ИЗ ДАЙДЖЕСТА)
# -------------------------------------------------------------

@router.callback_query(F.data.startswith("express_scan:"))
async def process_express_scan_callback(callback: CallbackQuery):
    icao = callback.data.split(":")[1]
    await callback.answer(f"Запрос стакана и физики: {icao}...")

    airport = resolve_airport(icao)
    if not airport:
        await callback.message.reply(f"❌ Данные для аэропорта {icao} не найдены.")
        return

    city_label = ALL_RADAR_CITIES.get(icao, icao)
    tz_name = airport.get("timezone", "UTC")
    try:
        local_tz = zoneinfo.ZoneInfo(tz_name)
        local_dt = datetime.now(local_tz)
    except Exception:
        local_tz = zoneinfo.ZoneInfo("UTC")
        local_dt = datetime.now(local_tz)

    target_date = local_dt.strftime("%Y-%m-%d")

    try:
        status_msg = await callback.message.reply(
            f"⚡ <i>Считываю маркет Polymarket и метеомодели для {city_label}...</i>",
            parse_mode="HTML"
        )
    except Exception:
        status_msg = await callback.message.answer(
            f"⚡ <i>Считываю маркет Polymarket и метеомодели для {city_label}...</i>",
            parse_mode="HTML"
        )

    # 1. Запрашиваем модели и METAR
    lat, lon = airport["lat"], airport["lon"]
    forecast_task = asyncio.to_thread(fetch_openmeteo_forecast, lat, lon, tz_name, target_date)
    noaa_task = asyncio.to_thread(get_noaa_package, icao)
    market_task = find_city_weather_event(icao, target_date)

    forecast_data, noaa_data, event_data = await asyncio.gather(
        forecast_task, noaa_task, market_task, return_exceptions=True
    )

    if isinstance(noaa_data, Exception) or not isinstance(noaa_data, dict):
        noaa_data = {}
    if isinstance(forecast_data, Exception) or not isinstance(forecast_data, dict):
        forecast_data = {}
    if isinstance(event_data, Exception) or not isinstance(event_data, dict):
        event_data = None

    metar = noaa_data.get("metar", {})
    temp_c = metar.get("temp_c")
    raw_metar = metar.get("raw", "")

    # Считываем суточные пики моделей
    models_max: Dict[str, float] = {}
    for m_key, m_val in {**forecast_data.get("primary_models", {}), **forecast_data.get("secondary_models", {})}.items():
        if m_val.get("status", {}).get("available"):
            t_max = (m_val.get("derived_metrics") or {}).get("max_temp_c")
            if t_max is not None:
                models_max[m_key] = float(t_max)

    peaks = list(models_max.values())
    avg_peak = round(sum(peaks) / len(peaks), 1) if peaks else (temp_c or 20.0)

    # Региональный приоритет моделей и расчет целевой температуры (KB v8.0)
    target_val, priority_model = get_priority_target(icao, models_max, avg_peak, raw_metar, local_dt=local_dt)
    target_strike = get_strike_for_temp(target_val)

    # Расчет темпа прогрева и остатка инсоляции по сезонному окну
    current_ts = time.time()
    rate_str, rem_hours_str, rate_val = _calculate_dynamics(icao, temp_c, local_dt, current_ts)
    local_hour = local_dt.hour + local_dt.minute / 60.0
    heating_cutoff, season_label = get_seasonal_heating_cutoff(local_dt.month, icao=icao)
    rem_hours = max(0.0, heating_cutoff - local_hour)

    # 2. Разбор стакана котировок Polymarket
    orderbook = parse_markets_orderbook(event_data.get("markets", [])) if event_data else []

    orderbook_lines = []
    favorite_candidate = None
    best_diff = 999.0
    basket_sum = 0.0
    is_overheated = False

    if orderbook:
        orderbook_lines.append("📊 <b>Текущие котировки (Стакан Polymarket):</b>")
        for item in orderbook:
            t_val = item["temp"]
            p_cents = item["price_cents"]
            title = item["title"]

            # Ищем совпадение с расчетным страйком
            is_target = (t_val is not None and t_val == target_strike)
            tag = " 🎯 <b>(ЦЕЛЬ)</b>" if is_target else ""

            orderbook_lines.append(f"• <code>{title}</code>: <b>{p_cents:.0f}¢</b> (${item['yes_price']:.2f}){tag}")

            if is_target:
                favorite_candidate = item
                basket_sum += p_cents
                best_diff = 0.0
            elif t_val is not None and favorite_candidate is None:
                diff = abs(t_val - target_val)
                if diff < best_diff and diff <= 0.7:
                    best_diff = diff
                    favorite_candidate = item

        if basket_sum >= 80.0 or (favorite_candidate and favorite_candidate["price_cents"] >= 80.0):
            is_overheated = True
    else:
        orderbook_lines.append("⚠️ <i>Активный контракт на Polymarket для этой даты пока не опубликован или закрыт.</i>")

    # 2.1. Проверка наличия открытой сделки пользователя (positions.db)
    user_id = callback.from_user.id if callback.from_user else None
    user_position = None
    if user_id:
        try:
            user_positions = await asyncio.to_thread(get_user_positions, user_id)
            user_position = next((p for p in user_positions if p.get("icao") == icao and p.get("target_date") == target_date), None)
            if not user_position:
                user_position = next((p for p in user_positions if p.get("icao") == icao), None)
        except Exception as e:
            logger.warning(f"Ошибка получения позиций для пользователя {user_id}: {e}")

    pos_info = None
    pos_header = ""
    if user_position:
        target_outcomes = user_position.get("outcomes", "Н/Д")
        entry_price = float(user_position.get("entry_price") or 0.0)

        # Пытаемся получить актуальную цену из стакана Polymarket
        cur_price = get_current_outcome_price(orderbook, target_outcomes)
        if cur_price is None:
            cur_price = entry_price if entry_price > 0 else 35.0

        if entry_price > 0:
            pnl_val = round(((cur_price - entry_price) / entry_price) * 100, 1)
            pnl_str = f"{'+' if pnl_val >= 0 else ''}{pnl_val:.0f}%"
        else:
            pnl_val = 0.0
            pnl_str = "+0%"
            entry_price = cur_price

        target_temp = extract_temp_value(target_outcomes)
        safe_outcomes = html.escape(str(target_outcomes))
        local_time_val = local_dt.hour + local_dt.minute / 60.0

        # Триггеры вердикта по открытой позиции:
        # 1. Тейк-Профит: PnL >= +35% или цена токена в стакане >= 60¢
        if (entry_price > 0 and (cur_price - entry_price) / entry_price >= 0.35) or cur_price >= 60.0:
            pos_verdict = (
                f"🚨 <b>ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ)</b> — Цель импульса закрыта ({pnl_str}). "
                f"До экспирации не сидеть! Сбрасывай страйк по лимитке в стакан прямо сейчас!"
            )
        # 2. Тайм-Стоп: 13:30 LT
        elif (local_time_val >= 13.5) and (target_temp and temp_c and temp_c < target_temp):
            pos_verdict = (
                f"⏱️ <b>ТАЙМ-СТОП (13:30 LT)</b> — Сброс в рынок для спасения остаточной стоимости! До полудня цель не пробита."
            )
        # 3. Физический слом:
        # До 12:30 LT утреннее замедление или высокая облачность НЕ инвалидируют позицию!
        elif local_time_val < 12.5:
            if is_blocking_rain_and_clouds(raw_metar):
                pos_verdict = (
                    f"🛑 <b>ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)</b> — На станцию вышли обложные осадки / плотный низкий Stratus!"
                )
            elif rate_val < 0.4 and local_dt.hour >= 9:
                pos_verdict = (
                    f"🟡 <b>ДЕРЖАТЬ ПОЗИЦИЮ (УТРЕННЯЯ ПАУЗА)</b> — Прогрев отстает, но солнечный полдень впереди (12:30–14:00 LT). Критической блокировки нет."
                )
            elif local_dt.hour < 9:
                pos_verdict = "🟢 <b>ДЕРЖАТЬ ПОЗИЦИЮ (УТРЕННИЙ ПОЛ)</b> — До 09:00 LT идет предрассветное выхолаживание, старт инсоляции впереди."
            else:
                pos_verdict = "🟢 <b>ДЕРЖАТЬ ПОЗИЦИЮ</b> — Темп прогрева в норме, инсоляция работает по плану."
        elif 12.5 <= local_time_val <= 15.0 and (rate_val < 0.4 and (has_real_low_cloud(raw_metar) or is_blocking_rain_and_clouds(raw_metar)) and (target_temp and temp_c and (target_temp - temp_c) >= 1.5)):
            pos_verdict = (
                f"🛑 <b>ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)</b> — После полудня темп затух под низкой облачностью, отставание от цели {target_temp - temp_c:.1f}°C критично!"
            )
        else:
            pos_verdict = "🟢 <b>ДЕРЖАТЬ ПОЗИЦИЮ</b> — Темп прогрева в норме, инсоляция работает по плану."

        pos_header = (
            f"💼 <b>ВАША ПОЗИЦИЯ:</b> <code>{safe_outcomes}</code> "
            f"(вход: <code>{entry_price:.0f}¢</code> | сейчас в стакане: <code>{cur_price:.0f}¢</code> | PnL: <b>{pnl_str}</b>)\n"
            f"👉 <b>ВЕРДИКТ ПОЗИЦИИ:</b> {pos_verdict}\n\n"
        )

        pos_info = {
            "outcomes": target_outcomes,
            "entry_price": entry_price,
            "cur_price": cur_price,
            "pnl_str": pnl_str,
            "pnl_val": pnl_val,
            "target_temp": target_temp,
            "target_date": user_position.get("target_date"),
            "calculated_verdict": pos_verdict,
        }

    # 3. Выработка стратегии по правилам KB v8.0
    is_rain = is_blocking_rain_and_clouds(raw_metar)
    is_afternoon_cutoff = (local_hour >= heating_cutoff)

    if is_rain or is_overheated or is_afternoon_cutoff:
        if is_afternoon_cutoff:
            cutoff_m = int((heating_cutoff % 1) * 60)
            strategy_block = (
                "⛔ <b>ВЕРДИКТ: СКИП МАРКЕТА (ОКНО ПРОГРЕВА ЗАКРЫТО)</b>\n"
                f"⚠️ <b>Время {local_dt.strftime('%H:%M')} LT: Активная дневная инсоляция угасла ({season_label}, закрытие в {int(heating_cutoff)}:{cutoff_m:02d} LT).</b> "
                f"Покупка страйков выше текущего факта ({temp_c if temp_c is not None else 'Н/Д'}°C) — гарантированный слив депозита."
            )
        else:
            strategy_block = (
                "⛔ <b>ВЕРДИКТ: СКИП МАРКЕТА</b>\n"
                "⚠️ <b>ПОКУПКА ОДИНОЧНОГО СТРАЙКА ЗДЕСЬ = СЛИВ ДЕПОЗИТА.</b> Рынок перегрет маркетмейкером, сиди на заборе."
            )
    elif favorite_candidate and 25.0 <= favorite_candidate["price_cents"] <= 48.0 and rem_hours >= 2.0:
        fav_title = html.escape(str(favorite_candidate['title']))
        entry_hint = "Сразу по рынку / в упор к Best Ask (днем просадка не высиживается)" if local_hour >= 9.5 else "Утренняя лимитка в спред"
        strategy_block = (
            f"🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b>\n"
            f"• <b>Рекомендуемый исход:</b> <code>{fav_title}</code> (цена <b>{favorite_candidate['price_cents']:.0f}¢</b>)\n"
            f"• <b>Вход:</b> {entry_hint}\n"
            f"• <b>Запас инсоляции:</b> {rem_hours:.1f} ч ({season_label}) | Приоритет: {priority_model}\n"
            f"• <b>Цель:</b> продажа токена толпе на дневном разгоне (+25%...+40% или 60¢–70¢), а не удержание до ночи!"
        )
    else:
        # Корзинный вход
        base_t = target_strike
        strategy_block = (
            f"🟢 <b>СИГНАЛ: СВЯЗКА КОРЗИНОЙ (MOMENTUM CASHOUT)</b>\n"
            f"• <b>Базовый страйк:</b> <code>{base_t}°C</code> + опцион <code>{base_t+1}°C</code> (сумма связки ≤ 75¢)\n"
            f"• <b>Тейк-профит:</b> Сброс корзины лимитными ордерами при росте на +25%...+40% до 13:30 LT."
        )

    # 2.2. Проверка истории анализов за сегодня (Сценарий Г)
    prev_snapshot = daily_tracker.get_latest(user_id or 0, icao, target_date)
    current_snapshot = {
        "timestamp": time.time(),
        "time_str": local_dt.strftime("%H:%M LT"),
        "target_date": target_date,
        "temp_c": temp_c,
        "raw_metar": raw_metar,
        "rate_str": rate_str,
        "rate_val": rate_val,
        "rem_hours_str": rem_hours_str,
        "orderbook": orderbook,
        "user_position": pos_info,
    }

    analysis_delta = None
    update_banner = ""
    if prev_snapshot:
        analysis_delta = compute_weather_delta(prev_snapshot, current_snapshot)
        elapsed_min_val = analysis_delta.get("elapsed_min", 0)
        elapsed_str = f"{elapsed_min_val} мин" if elapsed_min_val >= 1 else "менее 1 мин"
        pos_delta_line = f"\n💼 <b>Сделка:</b> {analysis_delta['pos_delta_str']}" if analysis_delta.get("pos_delta_str") else ""
        update_banner = (
            f"🔄 <b>ОБНОВЛЕНИЕ АНАЛИЗА: {city_label}</b>\n"
            f"🕒 <i>Срез {analysis_delta['time_curr']} относительно {analysis_delta['time_prev']} (прошло {elapsed_str})</i>\n"
            f"📊 <b>Динамика:</b> {analysis_delta['temp_prev']}°C ➔ {analysis_delta['temp_curr']}°C ({analysis_delta['temp_diff_str']}) | Темп: {analysis_delta['interval_rate_str']}\n"
            f"📈 <b>Стакан:</b> {analysis_delta['orderbook_shifts_str']}{pos_delta_line}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )

    response_text = (
        update_banner
        + (pos_header if pos_header else "")
        + f"⚡ <b>ЭКСПРЕСС-АНАЛИЗ: {city_label}</b>\n"
        + f"🕒 <i>Время: {local_dt.strftime('%H:%M')} LT | Дата: {target_date}</i>\n\n"
        + f"🌡️ <b>Факт METAR:</b> <code>{temp_c if temp_c is not None else 'Н/Д'}°C</code>\n"
        + f"📊 <b>Модели:</b> ECMWF: {models_max.get('ecmwf_hres', 'Н/Д')}°C | GFS: {models_max.get('gfs_global', 'Н/Д')}°C | ICON: {models_max.get('icon_global', 'Н/Д')}°C\n"
        + f"🎯 <b>Расчетный пик:</b> <b>{avg_peak}°C</b> (Опора: <i>{priority_model}</i>)\n\n"
        + "\n".join(orderbook_lines) + "\n\n"
        + "━━━━━━━━━━━━━━━━━━━━\n"
        + strategy_block
    )

    # 4. Формируем 1-Click кнопки перехода на Preddy и Polymarket (Вариант А)
    trade_buttons = []
    trade_row = []
    if event_data and event_data.get("slug"):
        slug = event_data["slug"]
        event_id = event_data.get("id")
        preddy_link = f"https://preddy.trade/event/{slug}/{event_id}" if event_id else f"https://preddy.trade/event/{slug}"
        poly_link = f"https://polymarket.com/event/{slug}"
        trade_row.append(InlineKeyboardButton(text="⚡ Открыть в Preddy", url=preddy_link))
        trade_row.append(InlineKeyboardButton(text="📊 Polymarket", url=poly_link))
        trade_buttons.append(trade_row)

    # 1-Click фиксация позиции или закрытие сделки прямо из анализа
    if user_position:
        pos_id = user_position.get("id")
        trade_buttons.append([
            InlineKeyboardButton(
                text=f"🛑 Закрыть сделку в боте ({user_position.get('outcomes', '')})",
                callback_data=f"quick_pos_close:{pos_id}:{icao}"
            )
        ])
    else:
        trade_buttons.append([
            InlineKeyboardButton(
                text="💼 Я вошел в сделку",
                callback_data=f"quick_pos_select:{icao}:{target_date}"
            )
        ])

    trade_buttons.append([
        InlineKeyboardButton(text=f"🔄 Обновить ({icao})", callback_data=f"express_scan:{icao}")
    ])
    trade_markup = InlineKeyboardMarkup(inline_keyboard=trade_buttons)

    # 5. Интеграция с квант-синоптиком Gemini AI (Вариант 1)
    if is_gemini_configured():
        city_pack = {
            "icao": icao,
            "city_name": city_label,
            "local_dt": local_dt,
            "temp_c": temp_c,
            "raw_metar": raw_metar,
            "models_max": models_max,
            "rate_str": rate_str,
            "rate_val": rate_val,
            "rem_hours_str": rem_hours_str,
            "orderbook": orderbook,
            "user_position": pos_info,
            "user_id": user_id or 0,
            "target_date": target_date,
            "season_label": season_label,
            "heating_cutoff": heating_cutoff,
            "previous_analysis": prev_snapshot,
            "analysis_delta": analysis_delta,
        }
        try:
            scenario_code = "G" if prev_snapshot else ("B" if pos_info else "A")
            ai_verdict = await asyncio.wait_for(
                analyze_city_weather_ai(city_pack, scenario=scenario_code),
                timeout=18.0
            )
            if ai_verdict:
                response_text = ai_verdict
        except Exception as e:
            logger.warning(f"Ошибка вызова Gemini AI: {e}")

    # Фиксируем актуальный срез в трекере дня
    daily_tracker.record(user_id or 0, icao, target_date, current_snapshot)

    # Защита от лимита длины сообщения Telegram (4096 символов)
    if len(response_text) > 4000:
        split_pos = response_text.rfind("\n\n3. ", 0, 3900)
        if split_pos == -1:
            split_pos = response_text.rfind("\n\n", 0, 3900)
        if split_pos != -1:
            part1 = response_text[:split_pos].strip()
            part2 = response_text[split_pos:].strip()
            try:
                await status_msg.edit_text(part1, parse_mode="HTML")
                await status_msg.answer(part2, parse_mode="HTML", reply_markup=trade_markup)
            except Exception as err:
                logger.error(f"Ошибка при edit_text разметки (part1/part2): {err}")
                await status_msg.edit_text(part1, parse_mode=None)
                await status_msg.answer(part2, parse_mode=None, reply_markup=trade_markup)
            return
        else:
            response_text = response_text[:3990] + "..."

    try:
        await status_msg.edit_text(response_text, parse_mode="HTML", reply_markup=trade_markup)
    except Exception as err:
        logger.error(f"Ошибка при edit_text: {err}")
        try:
            # Fallback 1: Отправка без парсинга HTML, если разметка Telegram сбилась
            await status_msg.edit_text(response_text, parse_mode=None, reply_markup=trade_markup)
        except Exception as err2:
            logger.error(f"Ошибка при edit_text fallback: {err2}")
            # Fallback 2: Отправка новым сообщением
            await callback.message.answer(response_text, parse_mode=None, reply_markup=trade_markup)


# -------------------------------------------------------------
# БАЗОВЫЕ КОМАНДЫ БОТА
# -------------------------------------------------------------

@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    register_subscriber(message.from_user.id, message.from_user.username or "")
    welcome_text = (
        "👋 <b>Weather Alpha Engine v8.0 активен!</b>\n\n"
        "🤖 <b>AI-Квант-Синоптик:</b> Нажми <b>«🤖 AI Аналитик»</b> или отправь /ai для полного нейросетевого разбора (Gemini).\n"
        "🔍 <b>Сканер маркетов:</b> Нажми <b>«🔍 Сканировать маркет»</b> и отправь ссылку с Preddy / Polymarket.\n"
        "📌 <b>Мои позиции:</b> Нажми <b>«📌 Мои позиции»</b> для контроля открытых сделок и PnL.\n"
        "🌍 <b>Города:</b> Нажми <b>«🌍 Избранные города»</b> или отправь 4-значный ICAO-код (<code>EGLC</code>, <code>KJFK</code>)."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(Command("help"), StateFilter("*"))
@router.message(F.text.in_(["📖 Справка / Команды", "📖 Справка / Регламент v7.1", "📖 Справка", "/help"]), StateFilter("*"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = (
        "📖 <b>Справка и регламент Weather Alpha Engine v8.0:</b>\n\n"
        "1. <b>🤖 AI Квант-Синоптик (/ai):</b> Мгновенный глубокий нейросетевой анализ (Gemini v8.0) по любому городу с разбором микрофизики и цен стакана.\n"
        "2. <b>🔍 Сканер маркета (/scan):</b> Отправь ссылку с Preddy/Polymarket — расчет сайзинга банкролла, безопасного коридора и генерация RAW Data Package.\n"
        "3. <b>📌 Мои позиции (/positions):</b> Мониторинг PnL открытых сделок, расчет темпа прогрева, триггеры Тейк-Профита (+35% / 60¢) и Тайм-Стопа (13:30 LT).\n"
        "4. <b>🔄 Консолидированный автосканер:</b> Каждые полчаса (:02 и :32) с 10:00 до 00:00 ХБР автоматическая рассылка единой сводки.\n"
        "5. <b>⚡ Ввод ICAO или координат:</b> Отправь <code>EGLC</code> или координаты (<code>48.52, 135.18</code>) для моментального отчета 4 моделей (ECMWF, GFS, ICON, GEM)."
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(F.text.in_(["🤖 AI Аналитик", "🤖 AI Анализ", "🤖 Gemini AI", "/ai", "/gemini"]), StateFilter("*"))
@router.message(Command("ai", "gemini"), StateFilter("*"))
async def cmd_ai_status(message: Message, state: FSMContext):
    await state.clear()
    status_info = get_gemini_status()
    if status_info["configured"]:
        active_model = status_info["active_model"]
        cascade_str = " ➔ ".join(status_info["cascade"][:4])
        text = (
            "🤖 <b>AI Квант-Синоптик Weather Alpha (Gemini)</b>\n\n"
            f"• <b>Статус AI-агента:</b> {status_info['status_label']}\n"
            f"• <b>Активная модель в запросе:</b> <code>{active_model}</code>\n"
            f"• <b>Отказоустойчивый каскад:</b> <code>{cascade_str}</code>\n"
            "• <b>Синоптическая база:</b> 5 законов микрофизики KB v8.1 + полный стакан котировок\n"
            "• <b>Принцип расчета:</b> Строгий приоритет физической истины (вероятность не подгоняется под стакан!)\n\n"
            "👇 <b>Выбери город для мгновенного AI-анализа:</b>\n"
            "<i>(Сразу формируется полный квант-разбор с синоптикой, стаканом цен и кнопками 1-Click перехода в Preddy)</i>"
        )
    else:
        text = (
            "🤖 <b>AI Квант-Синоптик Weather Alpha (Gemini)</b>\n\n"
            f"• <b>Статус AI-агента:</b> {status_info['status_label']}\n\n"
            "Для активации нейросетевого квант-синоптика прямо в Telegram добавь бесплатный ключ в файл <code>.env</code>:\n"
            "<code>GEMINI_API_KEY=ваш_ключ</code>\n\n"
            "🔑 Получить ключ бесплатно за 1 минуту в Google AI Studio:\n"
            "https://aistudio.google.com/app/apikey\n\n"
            "<i>(Пока ключ не задан, бот использует надежный локальный алгоритм синтеза физических законов KB v8.0).</i>\n\n"
            "👇 Выбери город для экспресс-скана:"
        )
    await message.answer(text, parse_mode="HTML", reply_markup=ai_cities_inline_keyboard)


async def sync_user_polymarket_positions(user_id: int) -> int:
    """
    Автоматически сканирует открытые погодные позиции пользователя на Polymarket
    и синхронизирует их в positions.db для радарного контроля и своевременного выхода.
    """
    wallet = get_user_wallet(user_id)
    if not wallet:
        return 0

    proxy_addr = wallet.get("proxy_address") or wallet.get("wallet_address") or ""
    if not proxy_addr:
        return 0

    url = f"https://data-api.polymarket.com/positions?user={proxy_addr}"
    synced_count = 0

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return 0
                positions_data = await resp.json()

        if not isinstance(positions_data, list):
            return 0

        existing_positions = get_user_positions(user_id)

        for item in positions_data:
            title = item.get("title", "")
            size = float(item.get("size") or 0.0)
            redeemable = item.get("redeemable", False)

            # Пропускаем уже закрытые рынки или нулевые балансы
            if redeemable or size <= 0 or "highest temperature" not in title.lower():
                continue

            detected_icao = _detect_city_icao(title)
            if not detected_icao:
                continue

            m_temp = re.search(r"be\s+(\d+)", title, re.IGNORECASE)
            if not m_temp:
                continue
            outcomes_str = f"{m_temp.group(1)}°C"

            target_date = item.get("endDate") or datetime.now().strftime("%Y-%m-%d")
            entry_price = round(float(item.get("avgPrice") or 0.0) * 100.0, 1)
            token_id = str(item.get("asset") or "")

            # Проверяем, есть ли уже такая сделка в базе
            is_already_added = any(
                p.get("icao") == detected_icao
                and p.get("target_date") == target_date
                and str(p.get("outcomes")).replace(" ", "") == outcomes_str
                for p in existing_positions
            )

            if not is_already_added:
                add_position(
                    user_id=user_id,
                    icao=detected_icao,
                    outcomes=outcomes_str,
                    target_date=target_date,
                    entry_price=entry_price,
                    shares=size,
                    token_id=token_id,
                )
                synced_count += 1
                logger.info(f"✅ Автосинхронизирована позиция {detected_icao} {outcomes_str} для пользователя {user_id}")

    except Exception as e:
        logger.error(f"Ошибка при синхронизации позиций Polymarket для {user_id}: {e}")

    return synced_count


async def _handle_public_wallet_input(message: Message, address: str) -> None:
    register_subscriber(message.from_user.id, message.from_user.username or "")
    clean_addr = address.strip()
    if not (clean_addr.startswith("0x") and len(clean_addr) == 42):
        await message.answer("❌ <b>Некорректный адрес кошелька.</b> Адрес должен начинаться с 0x и содержать 42 символа.", parse_mode="HTML")
        return

    proxy_addr = resolve_polymarket_proxy(clean_addr) or clean_addr
    save_user_public_wallet(message.from_user.id, clean_addr, proxy_addr)
    balance = get_wallet_collateral_balance("", wallet_address=clean_addr, proxy_address=proxy_addr)
    synced_count = await sync_user_polymarket_positions(message.from_user.id)

    info_lines = [
        f"• <b>Адрес кошелька (EVM):</b> <code>{clean_addr[:6]}...{clean_addr[-4:]}</code>",
    ]
    if proxy_addr and proxy_addr.lower() != clean_addr.lower():
        info_lines.append(f"• <b>Торговый сейф Polymarket:</b> <code>{proxy_addr[:6]}...{proxy_addr[-4:]}</code>")
    info_lines.append(f"• <b>Баланс средств:</b> <b>${balance:.2f} USDC</b>")
    info_lines.append("• <b>Режим работы:</b> 🟡 <b>Радарный мониторинг + сигналы на выход</b>")

    sync_note = (
        f"\n\n🎯 <i>Найдено и взято на радар открытых сделок: {synced_count}.</i>"
        if synced_count > 0
        else "\n\n<i>Открытых погодных сделок на сегодня пока не найдено. При открытии позиций на Polymarket/Preddy они автоматически подтянутся в «📌 Мои позиции».</i>"
    )

    await message.answer(
        "✅ <b>КОШЕЛЕК УСПЕШНО ПРИВЯЗАН К РАДАРУ!</b>\n\n"
        + "\n".join(info_lines)
        + sync_note +
        "\n\n🛡️ <b>Что делает бот в этом режиме:</b>\n"
        "1. Каждые 30 минут сопоставляет твои открытые позиции с фактом погоды METAR и графиком солнца.\n"
        "2. При достижении Тейк-Профита (+35% / 60¢), закрытии солнечного окна или дожде бот пришлет тебе <b>🚨 Срочный сигнал на продажу</b> прямо в Telegram.\n\n"
        "💡 <i>Если в будущем захочешь, чтобы бот сам автоматически закрывал сделки на бирже без твоего участия, отправь приватный ключ через /set_key.</i>",
        parse_mode="HTML"
    )


@router.message(F.text == "📌 Мои позиции", StateFilter("*"))
@router.message(Command("positions"), StateFilter("*"))
async def cmd_my_positions(message: Message, state: FSMContext):
    await state.clear()
    register_subscriber(message.from_user.id, message.from_user.username or "")
    await sync_user_polymarket_positions(message.from_user.id)
    positions = get_user_positions(message.from_user.id)

    if not positions:
        text = (
            "📌 <b>У тебя пока нет активных сделок на контроле.</b>\n\n"
            "Нажми <b>«➕ Добавить сделку»</b>, чтобы сканер каждые 30 минут отслеживал PnL, "
            "сигнализировал о Тейк-Профите (+35% / 60¢) и тайм-стопе!\n\n"
            "💡 <i>Или привяжи свой кошелек командой /set_wallet &lt;адрес&gt;, и бот сам найдет твои сделки с биржи!</i>"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить сделку", callback_data="add_new_pos")]]
        )
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    text_lines = ["📌 <b>Твои активные сделки под защитой сканера:</b>\n"]
    buttons = []

    for pos in positions:
        city_label = ALL_RADAR_CITIES.get(pos["icao"], pos["icao"])
        entry_s = f" (вход: {pos.get('entry_price', 0):.0f}¢)" if pos.get('entry_price') else ""
        text_lines.append(
            f"• <b>{city_label}</b> | Исходы: <code>{pos['outcomes']}</code>{entry_s} | Дата: <code>{pos['target_date']}</code>"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"⚡ AI Анализ: {pos['icao']}",
                callback_data=f"express_scan:{pos['icao']}"
            ),
            InlineKeyboardButton(
                text=f"❌ Закрыть: {pos['icao']}",
                callback_data=f"del_pos:{pos['id']}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="➕ Добавить еще сделку", callback_data="add_new_pos")])

    await message.answer(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.message(Command("digest"), StateFilter("*"))
async def cmd_force_digest(message: Message, state: FSMContext):
    """Выдает свежий 30-минутный сводный дайджест по 4 городам по прямому запросу пользователя."""
    await state.clear()
    register_subscriber(message.from_user.id, message.from_user.username or "")
    status_msg = await message.answer("🔄 <i>Считываю актуальные сводки METAR и моделей по 4 городам...</i>", parse_mode="HTML")
    try:
        from auto_scanner import (
            collect_city_metrics,
            build_dynamic_city_block,
            TARGET_CITIES,
            express_scan_keyboard,
            check_and_execute_auto_sell,
            sanitize_telegram_html,
        )
        now_khv = datetime.now(zoneinfo.ZoneInfo("Asia/Vladivostok"))
        time_khv_str = now_khv.strftime("%H:%M")
        header = (
            f"🔄 <b>ОБНОВЛЕНИЕ НА {time_khv_str} ХБР | ДИНАМИКА</b>\n"
            f"<i>Контроль темпа прогрева и статус открытых позиций</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        cities_metrics = []
        for icao in TARGET_CITIES.keys():
            cm = await collect_city_metrics(icao)
            cities_metrics.append(cm)
            await asyncio.sleep(0.2)

        positions = get_user_positions(message.from_user.id)
        user_blocks = []
        for cm in cities_metrics:
            pos = next((p for p in positions if p["icao"] == cm["icao"]), None)
            if pos:
                await check_and_execute_auto_sell(message.bot, cm, pos)
            user_blocks.append(build_dynamic_city_block(cm, pos))

        digest_msg = f"{header}\n\n" + "\n\n──────────────\n\n".join(user_blocks)
        safe_text = sanitize_telegram_html(digest_msg)
        await status_msg.edit_text(safe_text, parse_mode="HTML", reply_markup=express_scan_keyboard)
    except Exception as e:
        logger.error(f"Ошибка ручного вызова дайджеста: {e}", exc_info=True)
        await status_msg.edit_text(f"⚠️ Не удалось собрать дайджест: {e}")


# -------------------------------------------------------------
# АВТОПРОДАЖА И ПОДКЛЮЧЕНИЕ КОШЕЛЬКА POLYMARKET
# -------------------------------------------------------------

@router.message(Command("set_key"), StateFilter("*"))
async def cmd_set_wallet_key(message: Message):
    status_msg = await message.answer("⏳ <i>Проверяю ключ и подключаюсь к Polymarket...</i>", parse_mode="HTML")
    try:
        await message.delete()
    except Exception:
        pass

    register_subscriber(message.from_user.id, message.from_user.username or "")
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await status_msg.edit_text(
            "🔒 <b>Подключение торгового кошелька к автопродаже:</b>\n\n"
            "Отправь команду вместе с ключом из Predy:\n"
            "<code>/set_key &lt;твой_приватный_ключ&gt;</code>\n\n"
            "<i>(Или отправь /set_wallet &lt;адрес&gt; для мониторинга без приватного ключа)</i>\n\n"
            "🛡️ <i>Сообщение с ключом будет мгновенно удалено ботом из чата для безопасности.</i>",
            parse_mode="HTML",
        )
        return

    raw_key = args[1].strip()

    # Если пользователь отправил публичный EVM-адрес вместо приватного ключа
    if raw_key.startswith("0x") and len(raw_key) == 42:
        await status_msg.delete()
        await _handle_public_wallet_input(message, raw_key)
        return

    valid, address, err = validate_private_key(raw_key)
    if not valid:
        await status_msg.edit_text(
            f"❌ <b>Ошибка валидации ключа:</b> {err}\n\n"
            "Проверь, что скопирован именно секретный приватный ключ из раздела <i>Кошелек ➔ Экспорт ключа</i> в Predy.",
            parse_mode="HTML",
        )
        return

    try:
        proxy_address = resolve_polymarket_proxy(address) or address
        sig_type = 1 if (proxy_address and proxy_address.lower() != address.lower()) else 0
        save_user_wallet(message.from_user.id, raw_key, address, proxy_address, sig_type)
    except Exception as e_save:
        logger.error(f"Ошибка сохранения кошелька: {e_save}")
        proxy_address = address
        sig_type = 0
        save_user_wallet(message.from_user.id, raw_key, address, address, 0)

    balance = 0.0
    try:
        balance = get_wallet_collateral_balance(raw_key, wallet_address=address, proxy_address=proxy_address)
    except Exception as e_bal:
        logger.warning(f"Ошибка получения баланса: {e_bal}")

    try:
        await sync_user_polymarket_positions(message.from_user.id)
    except Exception as e_sync:
        logger.warning(f"Ошибка синхронизации позиций: {e_sync}")

    addr_info = [
        f"• <b>Ключ подписи (Signer):</b> <code>{address[:6]}...{address[-4:]}</code>",
    ]
    if proxy_address and proxy_address.lower() != address.lower():
        addr_info.append(f"• <b>Торговый сейф Polymarket:</b> <code>{proxy_address[:6]}...{proxy_address[-4:]}</code>")
    addr_info.append(f"• <b>Баланс средств:</b> <b>${balance:.2f} USDC</b>")
    addr_info.append("• <b>Статус:</b> 🟢 <b>Полный автопилот (автопродажа включена)</b>")

    await status_msg.edit_text(
        "✅ <b>КОШЕЛЕК УСПЕШНО ПОДКЛЮЧЕН К АВТОПРОДАЖЕ!</b>\n\n"
        + "\n".join(addr_info) + "\n\n"
        "🛡️ <i>Твое сообщение с ключом стерто из истории чата ради безопасности.</i>\n"
        "🚀 Теперь бот готов автоматически закрывать твои сделки по умному алгоритму:\n"
        "1. <b>Скользящий замок прибыли:</b> выжимает ракеты (+80%...+150%) и продает на первом откате вниз.\n"
        "2. <b>Погодный парашют:</b> экстренный сброс в рынок при дожде или плотной облачности.\n"
        "3. <b>Сезонный таймер:</b> продажа при закрытии солнечного окна в городе.\n\n"
        "💡 <i>Чтобы ключ никогда не сбрасывался при плановых обновлениях сервера, "
        "добавь переменную <code>WALLET_PRIVATE_KEY</code> в настройках хостинга (Environment).</i>",
        parse_mode="HTML",
    )


@router.message(Command("set_wallet"), StateFilter("*"))
async def cmd_set_wallet(message: Message):
    register_subscriber(message.from_user.id, message.from_user.username or "")
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "👛 <b>Привязка публичного кошелька:</b>\n\n"
            "Отправь команду вместе с EVM-адресом:\n"
            "<code>/set_wallet 0x...</code>\n\n"
            "<i>Бот автоматически найдет твой профиль Polymarket/Preddy, подтянет баланс и возьмет на радар все открытые погодные позиции!</i>",
            parse_mode="HTML"
        )
        return
    await _handle_public_wallet_input(message, args[1].strip())


@router.message(Command("sync"), StateFilter("*"))
async def cmd_sync(message: Message):
    register_subscriber(message.from_user.id, message.from_user.username or "")
    status_msg = await message.answer("🔄 <i>Синхронизирую открытые позиции с Polymarket...</i>", parse_mode="HTML")
    count = await sync_user_polymarket_positions(message.from_user.id)
    if count > 0:
        await status_msg.edit_text(f"✅ <b>Синхронизация завершена!</b> Добавлено на радар новых сделок: <b>{count}</b>.\nНажми «📌 Мои позиции», чтобы увидеть их.", parse_mode="HTML")
    else:
        await status_msg.edit_text("✅ <b>Синхронизация завершена.</b> Все открытые сделки уже под защитой сканера.", parse_mode="HTML")


@router.message(Command("wallet"), StateFilter("*"))
async def cmd_check_wallet(message: Message):
    register_subscriber(message.from_user.id, message.from_user.username or "")
    await sync_user_polymarket_positions(message.from_user.id)
    wallet = get_user_wallet(message.from_user.id)
    if not wallet:
        await message.answer(
            "👛 <b>Кошелек не подключен.</b>\n\n"
            "• Чтобы бот отслеживал твои позиции и присылал сигналы на продажу:\n"
            "  <code>/set_wallet &lt;твой_публичный_EVM_адрес&gt;</code>\n\n"
            "• Чтобы бот сам автоматически закрывал сделки на бирже:\n"
            "  <code>/set_key &lt;твой_приватный_ключ_из_Predy&gt;</code>",
            parse_mode="HTML",
        )
        return

    address = wallet["wallet_address"]
    proxy_address = wallet.get("proxy_address") or resolve_polymarket_proxy(address) or address
    has_pk = bool(wallet.get("private_key"))
    balance = get_wallet_collateral_balance(wallet.get("private_key", ""), wallet_address=address, proxy_address=proxy_address)

    addr_info = [
        f"• <b>Кошелек:</b> <code>{address[:6]}...{address[-4:]}</code>",
    ]
    if proxy_address and proxy_address.lower() != address.lower():
        addr_info.append(f"• <b>Торговый сейф Polymarket:</b> <code>{proxy_address[:6]}...{proxy_address[-4:]}</code>")
    addr_info.append(f"• <b>Баланс средств:</b> <b>${balance:.2f} USDC</b>")
    if has_pk:
        addr_info.append("• <b>Статус:</b> 🟢 <b>Полный автопилот (автопродажа активна)</b>")
    else:
        addr_info.append("• <b>Статус:</b> 🟡 <b>Радарный мониторинг (сигналы на продажу в Telegram)</b>")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🔌 Отключить кошелек", callback_data="disconnect_wallet_confirm")
        ]]
    )
    await message.answer(
        f"👛 <b>ПОДКЛЮЧЕННЫЙ КОШЕЛЕК POLYMARKET:</b>\n\n"
        + "\n".join(addr_info),
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data == "disconnect_wallet_confirm")
async def process_disconnect_wallet_callback(callback: CallbackQuery):
    delete_user_wallet(callback.from_user.id)
    await callback.message.edit_text(
        "🔌 <b>Кошелек успешно отключен.</b> Все данные удалены.",
        parse_mode="HTML",
    )


@router.message(Command("open"), StateFilter("*"))
async def cmd_quick_open_position(message: Message):
    """
    Быстрое добавление сделки: /open <город> <страйк> <цена_входа>
    Например: /open Милан 23 35
    """
    args = (message.text or "").split()[1:]
    if len(args) < 2:
        await message.answer(
            "📌 <b>Быстрое взятие сделки на автопилот:</b>\n\n"
            "Формат команды:\n"
            "<code>/open &lt;город&gt; &lt;градус&gt; &lt;цена_входа_в_центах&gt;</code>\n\n"
            "<i>Пример:</i> <code>/open Милан 23 35</code>\n"
            "<i>Пример:</i> <code>/open Лондон 25 18</code>",
            parse_mode="HTML",
        )
        return

    city_raw = args[0].strip().lower()
    city_map = {
        "лондон": "EGLC", "london": "EGLC", "eglc": "EGLC",
        "париж": "LFPB", "paris": "LFPB", "lfpb": "LFPB",
        "милан": "LIMC", "milan": "LIMC", "limc": "LIMC",
        "мадрид": "LEMD", "madrid": "LEMD", "lemd": "LEMD",
    }
    icao = city_map.get(city_raw, "EGLC")
    strike_val = extract_temp_value(args[1])
    if strike_val is None:
        await message.answer("❌ Не удалось распознать градус. Пример: <code>/open Милан 23 35</code>", parse_mode="HTML")
        return

    entry_price = float(extract_temp_value(args[2]) or 0.0) if len(args) >= 3 else 35.0
    outcomes_str = f"{int(strike_val)}°C"

    airport_data = resolve_airport(icao) or {}
    tz_name = airport_data.get("timezone", "UTC")
    try:
        local_date = datetime.now(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    except Exception:
        local_date = datetime.now().strftime("%Y-%m-%d")

    # Ищем token_id на Polymarket
    token_id = ""
    event = await find_city_weather_event(icao, target_date=local_date)
    if event and event.get("markets"):
        orderbook = parse_markets_orderbook(event["markets"])
        token_id = get_outcome_token_id(orderbook, outcomes_str) or ""

    pos_id = add_position(
        user_id=message.from_user.id,
        icao=icao,
        outcomes=outcomes_str,
        target_date=local_date,
        entry_price=entry_price,
        token_id=token_id,
    )

    wallet = get_user_wallet(message.from_user.id)
    has_pk = bool(wallet and wallet.get("private_key"))
    auto_status = (
        "🟢 <b>АКТИВНА</b> (Трейлинг + Погодный парашют)"
        if has_pk
        else ("🟡 <b>ТОЛЬКО СИГНАЛЫ</b> (для автопродажи отправь /set_key)" if wallet else "⚪ <b>Отключена</b> (отправь /set_key для автовыхода)")
    )

    city_name = ALL_RADAR_CITIES.get(icao, icao)
    await message.answer(
        f"✅ <b>СДЕЛКА ВЗЯТА НА АВТОПИЛОТ!</b>\n\n"
        f"• <b>Город:</b> {city_name}\n"
        f"• <b>Исход:</b> <code>{outcomes_str}</code> (Вход: <b>{entry_price:.0f}¢</b>)\n"
        f"• <b>Дата:</b> <code>{local_date}</code>\n"
        f"• <b>Автопродажа:</b> {auto_status}\n\n"
        f"🛡️ <i>Спокойно иди на смену. Бот сам продаст токен по рынку на пике разгона (+80%...+150%), "
        f"либо мгновенно катапультируется при дожде/тучах!</i>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "add_new_pos")
async def process_add_pos_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddPositionStates.waiting_for_city)
    await callback.message.edit_text(
        "🌍 <b>Шаг 1 из 2: Выбери город твоей открытой сделки:</b>",
        parse_mode="HTML",
        reply_markup=position_city_keyboard,
    )


@router.callback_query(F.data.startswith("pos_city:"))
async def process_pos_city_selected(callback: CallbackQuery, state: FSMContext):
    icao_code = callback.data.split(":")[1]
    airport_data = resolve_airport(icao_code) or {}
    tz_name = airport_data.get("timezone", "UTC")
    try:
        local_date = datetime.now(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    except Exception:
        local_date = datetime.now().strftime("%Y-%m-%d")

    await state.update_data(pos_icao=icao_code, pos_date=local_date)

    city_name = ALL_RADAR_CITIES.get(icao_code, icao_code)

    # Загружаем актуальный стакан Polymarket для генерации быстрых кнопок в 1 клик
    event = await find_city_weather_event(icao_code, target_date=local_date)
    buttons = []
    if event and event.get("markets"):
        orderbook = parse_markets_orderbook(event["markets"])
        row = []
        for item in orderbook:
            t = item.get("temp")
            p = item.get("price_cents", 0.0)
            if t is not None and 1.0 <= p <= 95.0:
                t_str = f"{int(t)}" if t == int(t) else f"{t}"
                btn_text = f"🎯 {t_str}°C ({p:.0f}¢)"
                cb_data = f"quick_pick:{icao_code}:{t_str}:{p:.0f}"
                row.append(InlineKeyboardButton(text=btn_text, callback_data=cb_data))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
        if row:
            buttons.append(row)

    buttons.append([InlineKeyboardButton(text="✏️ Ввести исход вручную", callback_data=f"manual_pos:{icao_code}")])
    buttons.append([InlineKeyboardButton(text="« Назад к выбору города", callback_data="add_new_pos")])

    await callback.message.edit_text(
        f"🎯 <b>Шаг 2 из 2: Локация {city_name}</b>\n\n"
        "👇 <b>Нажми на исход, который ты купил (цена и контракт подтянутся в 1 клик):</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("quick_pick:"))
async def process_quick_pick_pos(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    parts = callback.data.split(":")
    icao = parts[1]
    strike_str = parts[2]
    price_cents = float(parts[3])

    airport_data = resolve_airport(icao) or {}
    tz_name = airport_data.get("timezone", "UTC")
    try:
        local_date = datetime.now(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    except Exception:
        local_date = datetime.now().strftime("%Y-%m-%d")

    outcomes_str = f"{strike_str}°C" if not strike_str.endswith("°C") else strike_str

    # Получаем token_id для автопродажи
    token_id = ""
    event = await find_city_weather_event(icao, target_date=local_date)
    if event and event.get("markets"):
        orderbook = parse_markets_orderbook(event["markets"])
        token_id = get_outcome_token_id(orderbook, outcomes_str) or ""

    add_position(
        user_id=callback.from_user.id,
        icao=icao,
        outcomes=outcomes_str,
        target_date=local_date,
        entry_price=price_cents,
        token_id=token_id,
    )

    wallet = get_user_wallet(callback.from_user.id)
    has_pk = bool(wallet and wallet.get("private_key"))
    auto_status = (
        "🟢 <b>АКТИВНА</b> (Трейлинг + Погодный парашют)"
        if has_pk
        else ("🟡 <b>ТОЛЬКО СИГНАЛЫ</b> (для автопродажи отправь /set_key)" if wallet else "⚪ <b>Отключена</b> (отправь /set_key для автовыхода)")
    )

    city_name = ALL_RADAR_CITIES.get(icao, icao)
    await callback.message.edit_text(
        f"✅ <b>СДЕЛКА ВЗЯТА НА АВТОПИЛОТ В 1 КЛИК!</b>\n\n"
        f"• <b>Город:</b> {city_name}\n"
        f"• <b>Исход:</b> <code>{outcomes_str}</code> (Вход: <b>{price_cents:.0f}¢</b>)\n"
        f"• <b>Дата:</b> <code>{local_date}</code>\n"
        f"• <b>Автопродажа:</b> {auto_status}\n\n"
        f"🛡️ <i>Никаких команд писать не нужно. Бот сам зафиксирует прибыль на пике разгона (+80%...+150%) "
        f"или катапультируется в рынок при дожде/тучах!</i>",
        parse_mode="HTML",
    )
    await callback.answer("✅ Сделка на автопилоте!")


@router.callback_query(F.data.startswith("manual_pos:"))
async def process_manual_pos_prompt(callback: CallbackQuery, state: FSMContext):
    icao_code = callback.data.split(":")[1]
    await state.set_state(AddPositionStates.waiting_for_outcomes)
    city_name = ALL_RADAR_CITIES.get(icao_code, icao_code)
    await callback.message.edit_text(
        f"✏️ <b>Ручной ввод: Локация {city_name}</b>\n\n"
        "Напиши купленный исход и цену входа (например: <code>23°C 35¢</code> или просто <code>23</code>):",
        parse_mode="HTML",
    )


@router.message(AddPositionStates.waiting_for_outcomes, F.text)
async def process_pos_outcomes_input(message: Message, state: FSMContext):
    user_input = message.text.strip()
    data = await state.get_data()
    icao = data.get("pos_icao", "EGLC")
    target_date = data.get("pos_date", datetime.now().strftime("%Y-%m-%d"))

    # Парсим исход и цену входа (если указана)
    temp_val = extract_temp_value(user_input)
    price_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:¢|c|cents?|центов|\$)", user_input, re.IGNORECASE)
    entry_price = float(price_match.group(1)) if price_match else 0.0

    outcomes_str = f"{int(temp_val)}°C" if temp_val is not None else user_input

    # Ищем token_id на Polymarket для автопродажи
    token_id = ""
    event = await find_city_weather_event(icao, target_date=target_date)
    if event and event.get("markets"):
        orderbook = parse_markets_orderbook(event["markets"])
        token_id = get_outcome_token_id(orderbook, outcomes_str) or ""

    add_position(
        user_id=message.from_user.id,
        icao=icao,
        outcomes=outcomes_str,
        target_date=target_date,
        entry_price=entry_price,
        token_id=token_id,
    )

    await state.clear()
    city_name = ALL_RADAR_CITIES.get(icao, icao)
    entry_info = f" по цене <b>{entry_price:.0f}¢</b>" if entry_price > 0 else ""
    wallet = get_user_wallet(message.from_user.id)
    has_pk = bool(wallet and wallet.get("private_key"))
    auto_status = (
        "🟢 <b>АКТИВНА</b> (Трейлинг + Погодный парашют)"
        if has_pk
        else ("🟡 <b>ТОЛЬКО СИГНАЛЫ</b> (для автопродажи отправь /set_key)" if wallet else "⚪ <b>Отключена</b> (отправь /set_key для автовыхода)")
    )

    await message.answer(
        f"✅ <b>Позиция успешно добавлена под защиту сканера!</b>\n\n"
        f"📍 <b>Город:</b> {city_name}\n"
        f"🎯 <b>Исход:</b> <code>{outcomes_str}</code>{entry_info}\n"
        f"📅 <b>Дата:</b> <code>{target_date}</code>\n"
        f"🤖 <b>Автопродажа:</b> {auto_status}\n\n"
        f"🛡️ <i>Каждые 30 минут сканер сопоставляет стакан Polymarket, факт METAR и темп инсоляции. "
        f"При подключенном кошельке бот сам исполнит продажу на пике разгона (+80%...+150%) или при дожде/тучах!</i>",
        parse_mode="HTML",
        reply_markup=main_keyboard
    )


@router.callback_query(F.data.startswith("del_pos:"))
async def process_del_pos_callback(callback: CallbackQuery):
    pos_id = int(callback.data.split(":")[1])
    delete_position(pos_id, callback.from_user.id)
    await callback.answer("✅ Сделка закрыта.")

    positions = get_user_positions(callback.from_user.id)
    if not positions:
        await callback.message.edit_text(
            "📌 <b>Все сделки закрыты. Активных позиций на контроле нет.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить сделку", callback_data="add_new_pos")]]
            )
        )
        return

    buttons = []
    text_lines = ["📌 <b>Твои активные сделки под защитой сканера:</b>\n"]
    for pos in positions:
        city_label = ALL_RADAR_CITIES.get(pos["icao"], pos["icao"])
        text_lines.append(
            f"• <b>{city_label}</b> | Исходы: <code>{pos['outcomes']}</code> | Дата: <code>{pos['target_date']}</code>"
        )
        buttons.append([
            InlineKeyboardButton(
                text=f"❌ Закрыть: {pos['icao']} ({pos['outcomes']})",
                callback_data=f"del_pos:{pos['id']}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="➕ Добавить еще сделку", callback_data="add_new_pos")])
    await callback.message.edit_text(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


# -------------------------------------------------------------
# 1-CLICK БЫСТРАЯ ФИКСАЦИЯ И СБРОС СДЕЛКИ (QUICK POSITIONS)
# -------------------------------------------------------------

@router.callback_query(F.data.startswith("quick_pos_select:"))
async def process_quick_pos_select(callback: CallbackQuery):
    parts = callback.data.split(":")
    icao = parts[1]
    target_date = parts[2] if len(parts) > 2 else datetime.now().strftime("%Y-%m-%d")

    city_label = ALL_RADAR_CITIES.get(icao, icao)
    await callback.answer("Загружаю актуальные страйки...")

    event_data = await find_city_weather_event(icao, target_date)
    orderbook = []
    if event_data and event_data.get("markets"):
        orderbook = parse_markets_orderbook(event_data["markets"])

    buttons = []
    if orderbook:
        row = []
        for item in orderbook:
            t_num = item.get("temp")
            price = item.get("price_cents", 0.0)
            if t_num is not None:
                outcome_clean = f"{int(t_num)}C"
                btn_text = f"{int(t_num)}°C ({price:.0f}¢)"
                cb_data = f"qps:{icao}:{outcome_clean}:{int(round(price))}:{target_date}"
                row.append(InlineKeyboardButton(text=btn_text, callback_data=cb_data))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
        if row:
            buttons.append(row)

    if not buttons:
        fallback_temps = [20, 21, 22, 23, 24, 25, 26]
        row = []
        for t in fallback_temps:
            cb_data = f"qps:{icao}:{t}C:30:{target_date}"
            row.append(InlineKeyboardButton(text=f"{t}°C", callback_data=cb_data))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="🔙 Назад к анализу", callback_data=f"express_scan:{icao}")
    ])

    await callback.message.edit_text(
        f"💼 <b>Какой страйк вы купили по {city_label}?</b>\n\n"
        f"Нажмите на кнопку с вашей температурой. Бот мгновенно запишет сделку в базу и включит персональный риск-менеджмент:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("qps:"))
async def process_quick_pos_save(callback: CallbackQuery):
    parts = callback.data.split(":")
    icao = parts[1]
    outcome_raw = parts[2]
    price_val = float(parts[3])
    target_date = parts[4]

    temp_val = extract_temp_value(outcome_raw)
    outcomes_str = f"{int(temp_val)}°C" if temp_val is not None else outcome_raw
    user_id = callback.from_user.id if callback.from_user else 0

    # Ищем token_id для автопродажи
    token_id = ""
    event_data = await find_city_weather_event(icao, target_date)
    if event_data and event_data.get("markets"):
        orderbook = parse_markets_orderbook(event_data["markets"])
        token_id = get_outcome_token_id(orderbook, outcomes_str) or ""

    add_position(
        user_id=user_id,
        icao=icao,
        outcomes=outcomes_str,
        target_date=target_date,
        entry_price=price_val,
        token_id=token_id,
    )

    await callback.answer(f"✅ Позиция {outcomes_str} взята на радар!")

    city_label = ALL_RADAR_CITIES.get(icao, icao)
    wallet = get_user_wallet(user_id)
    has_pk = bool(wallet and wallet.get("private_key"))
    auto_status = (
        "🟢 <b>АКТИВНА</b> (Трейлинг + Погодный парашют)"
        if has_pk
        else ("🟡 <b>ТОЛЬКО СИГНАЛЫ</b> (для автопродажи отправь /set_key)" if wallet else "⚪ <b>Отключена</b> (отправь /set_key для автовыхода)")
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⚡ Посмотреть анализ по {icao}", callback_data=f"express_scan:{icao}")],
            [InlineKeyboardButton(text="📌 Мои позиции", callback_data="open_my_positions")]
        ]
    )

    await callback.message.edit_text(
        f"✅ <b>Позиция успешно взята под защиту!</b>\n\n"
        f"📍 <b>Город:</b> {city_label}\n"
        f"🎯 <b>Купленный страйк:</b> <code>{outcomes_str}</code>\n"
        f"💵 <b>Цена входа:</b> <code>{price_val:.0f}¢</code>\n"
        f"📅 <b>Дата экспирации:</b> <code>{target_date}</code>\n"
        f"🤖 <b>Автопродажа:</b> {auto_status}\n\n"
        f"🛡️ <i>Сканер теперь непрерывно сопоставляет факт METAR, ветер и солнце. "
        f"При признаках слома погоды или при достижении Тейк-Профита (+35% / 60¢) ты получишь персональный сигнал!</i>",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.callback_query(F.data.startswith("quick_pos_close:"))
async def process_quick_pos_close(callback: CallbackQuery):
    parts = callback.data.split(":")
    pos_id = int(parts[1])
    icao = parts[2] if len(parts) > 2 else "EGLC"

    user_id = callback.from_user.id if callback.from_user else 0
    delete_position(pos_id, user_id)
    await callback.answer("✅ Сделка закрыта в боте.")

    callback.data = f"express_scan:{icao}"
    await process_express_scan_callback(callback)


@router.callback_query(F.data == "open_my_positions")
async def process_open_my_positions(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    register_subscriber(callback.from_user.id, callback.from_user.username or "")
    await sync_user_polymarket_positions(callback.from_user.id)
    positions = get_user_positions(callback.from_user.id)
    if not positions:
        await callback.message.edit_text(
            "📌 <b>У тебя пока нет активных сделок на контроле.</b>\n\n"
            "Нажми <b>«➕ Добавить сделку»</b>, чтобы сканер каждые 30 минут отслеживал PnL!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить сделку", callback_data="add_new_pos")]]
            )
        )
        return

    wallet = get_user_wallet(callback.from_user.id)
    has_pk = bool(wallet and wallet.get("private_key"))
    mode_desc = (
        "🟢 <b>Автопродажа активна</b> (Трейлинг + Погодный парашют)"
        if has_pk
        else ("🟡 <b>Только сигналы</b> (для автопродажи отправь /set_key)" if wallet else "⚪ <b>Кошелек не подключен</b> (отправь /set_key)")
    )
    text_lines = [
        "📌 <b>Твои активные сделки под защитой сканера:</b>",
        f"🤖 <i>Режим защиты: {mode_desc}</i>\n",
    ]
    buttons = []
    for pos in positions:
        city_label = ALL_RADAR_CITIES.get(pos["icao"], pos["icao"])
        entry_s = f" (вход: {pos.get('entry_price', 0):.0f}¢)" if pos.get('entry_price') else ""
        text_lines.append(
            f"• <b>{city_label}</b> | Исходы: <code>{pos['outcomes']}</code>{entry_s} | Дата: <code>{pos['target_date']}</code>"
        )
        buttons.append([
            InlineKeyboardButton(text=f"⚡ AI Анализ: {pos['icao']}", callback_data=f"express_scan:{pos['icao']}"),
            InlineKeyboardButton(text=f"❌ Закрыть: {pos['icao']}", callback_data=f"del_pos:{pos['id']}")
        ])
    buttons.append([InlineKeyboardButton(text="➕ Добавить еще сделку", callback_data="add_new_pos")])

    await callback.message.edit_text(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.message(F.text == "🌍 Избранные города", StateFilter("*"))
@router.message(Command("cities"), StateFilter("*"))
async def cmd_cities_menu(message: Message, state: FSMContext):
    await state.clear()
    register_subscriber(message.from_user.id, message.from_user.username or "")
    await message.answer(
        "🌍 <b>Выбери город для полного синоптического анализа (4 модели + METAR):</b>",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


@router.callback_query(F.data.startswith("icao:"))
async def process_city_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    register_subscriber(callback.from_user.id, callback.from_user.username or "")
    icao_code = callback.data.split(":")[1]
    await callback.answer(f"Сбор метеоданных для {icao_code}...")
    await _execute_weather_pipeline(icao_code, callback.message)


@router.message(F.text == "🔍 Сканировать маркет", StateFilter("*"))
async def btn_scan_trigger(message: Message, state: FSMContext):
    await state.set_state(MarketScanStates.waiting_for_link)
    await message.answer(
        "📥 <b>Режим сканирования активирован!</b>\n\n"
        "Отправь ссылку на погодный маркет из <b>Preddy</b> или <b>Polymarket</b> следующим сообщением 👇",
        parse_mode="HTML",
    )


@router.message(Command("scan"), StateFilter("*"))
async def cmd_scan_market(message: Message, state: FSMContext):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await state.set_state(MarketScanStates.waiting_for_link)
        await message.answer(
            "📥 <b>Режим сканирования активирован!</b>\n\n"
            "Отправь ссылку на маркет следующим сообщением 👇",
            parse_mode="HTML",
        )
        return

    param_type, param_val = _parse_market_identifier(args[1])
    if not param_type or not param_val:
        await message.answer("❌ <b>Не удалось распознать ссылку.</b>", parse_mode="HTML")
        return

    event_data = await fetch_event_by_id_or_slug(param_type, param_val)
    if not event_data:
        await message.answer("❌ <b>Маркет не найден.</b> Проверь ссылку.", parse_mode="HTML")
        return

    await state.update_data(event_data=event_data, raw_link=args[1])
    await state.set_state(MarketScanStates.waiting_for_balance)
    await message.answer(
        f"📊 <b>Маркет найден:</b> {event_data.get('title', 'Событие')}\n\n"
        "💰 <b>Введи твой текущий баланс на Preddy ($)</b> сообщением или нажми кнопку ниже:",
        parse_mode="HTML",
        reply_markup=balance_quick_keyboard,
    )


@router.callback_query(F.data.startswith("set_bal:"))
async def process_quick_balance(callback: CallbackQuery, state: FSMContext):
    balance_val = float(callback.data.split(":")[1])
    data = await state.get_data()
    event_data = data.get("event_data")
    raw_link = data.get("raw_link", "")

    await state.clear()
    await callback.message.delete()

    if event_data:
        await _render_final_scan_report(event_data, balance_val, raw_link, callback.message)
    else:
        await callback.message.answer("⚠️ Сессия истекла. Нажми <b>«🔍 Сканировать маркет»</b> заново.", parse_mode="HTML")


@router.message(MarketScanStates.waiting_for_link, F.text)
async def process_market_link_input(message: Message, state: FSMContext):
    user_text = message.text.strip()
    param_type, param_val = _parse_market_identifier(user_text)

    if not param_type or not param_val:
        await message.answer(
            "❌ <b>Не удалось распознать ссылку.</b>\n"
            "Убедись, что отправляешь корректную ссылку с Preddy или Polymarket.",
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer("⚡ <i>Считываю котировки маркета...</i>", parse_mode="HTML")
    event_data = await fetch_event_by_id_or_slug(param_type, param_val)

    if not event_data:
        await status_msg.edit_text("❌ <b>Маркет не найден.</b> Проверь ссылку.", parse_mode="HTML")
        return

    await state.update_data(event_data=event_data, raw_link=user_text)
    await state.set_state(MarketScanStates.waiting_for_balance)

    await status_msg.delete()
    await message.answer(
        f"📊 <b>Маркет найден:</b> {event_data.get('title', 'Событие')}\n\n"
        "💰 <b>Введи твой текущий баланс на Preddy ($)</b> сообщением или выбери кнопку:",
        parse_mode="HTML",
        reply_markup=balance_quick_keyboard,
    )


@router.message(MarketScanStates.waiting_for_balance, F.text)
async def process_manual_balance_input(message: Message, state: FSMContext):
    user_text = message.text.strip().replace("$", "").replace(",", ".")
    try:
        user_balance = float(user_text)
        if user_balance <= 0:
            user_balance = 25.0
    except ValueError:
        user_balance = 25.0

    data = await state.get_data()
    event_data = data.get("event_data")
    raw_link = data.get("raw_link", "")

    await state.clear()
    if event_data:
        await _render_final_scan_report(event_data, user_balance, raw_link, message)
    else:
        await message.answer("⚠️ Сессия истекла. Нажми <b>«🔍 Сканировать маркет»</b> заново.", parse_mode="HTML")


async def _render_final_scan_report(event_data: Dict[str, Any], user_balance: float, raw_input: str, target_message: Message):
    title = event_data.get("title", "Погодный маркет")
    slug = event_data.get("slug", "")
    description = event_data.get("description", "")
    markets = event_data.get("markets", [])

    if not markets:
        await target_message.answer("⚠️ В этом событии нет активных котировок.", parse_mode="HTML")
        return

    tier_name, bet_size, corridor_info = calculate_tier_sizing(user_balance)
    safe_title = html.escape(str(title))
    report_lines = [
        f"📊 <b>{safe_title}</b>\n",
        f"💼 <b>Твой депозит:</b> <code>${user_balance:.2f}</code> ({tier_name})",
        f"🎯 <b>Точечный вход:</b> <code>${bet_size:.2f}</code>",
        f"🛡️ <b>Коридор:</b> <code>{corridor_info}</code>\n",
        "<b>Текущие котировки исходов (Стакан):</b>"
    ]

    for item in markets:
        raw_question = item.get("groupItemTitle") or item.get("question", "Исход")
        question = html.escape(str(raw_question))
        prices_str = item.get("outcomePrices", '["0", "0"]')
        try:
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            yes_price = float(prices[0]) if len(prices) > 0 else 0.0
        except Exception:
            yes_price = 0.0

        if yes_price > 0.0:
            shares_count = int(bet_size / yes_price)
            potential_payout = shares_count * 1.0
            net_profit = potential_payout - bet_size
            roi = ((1.0 / yes_price) - 1.0) * 100
            price_cents = round(yes_price * 100, 1)

            report_lines.append(
                f"• <b>{question}</b>: <code>{price_cents}¢</code> (${yes_price:.2f})\n"
                f"  └ <i>Вход (${bet_size:.2f}):</i> <b>{shares_count} shares</b> | Профит: <b>+${net_profit:.2f} (+{roi:.0f}%)</b>"
            )
        else:
            report_lines.append(f"• <b>{question}</b>: <code>0¢</code> (Нет ликвидности)")

    orderbook_block = "\n".join(report_lines)
    search_context = f"{title} {slug} {description} {raw_input}"
    detected_icao = _detect_city_icao(search_context)
    detected_date = _detect_market_target_date(search_context)

    # Формируем 1-Click кнопки перехода на Preddy и Polymarket (Вариант А)
    trade_buttons = []
    trade_row = []
    if slug:
        event_id = event_data.get("id")
        preddy_link = f"https://preddy.trade/event/{slug}/{event_id}" if event_id else f"https://preddy.trade/event/{slug}"
        poly_link = f"https://polymarket.com/event/{slug}"
        trade_row.append(InlineKeyboardButton(text="⚡ Открыть в Preddy", url=preddy_link))
        trade_row.append(InlineKeyboardButton(text="📊 Polymarket", url=poly_link))
        trade_buttons.append(trade_row)
    trade_markup = InlineKeyboardMarkup(inline_keyboard=trade_buttons) if trade_buttons else None

    if detected_icao:
        date_label = f" на {detected_date}" if detected_date else ""
        status_msg = await target_message.answer(
            f"⚡ <i>Считываю метеомодели и прогноз для {detected_icao}{date_label}...</i>",
            parse_mode="HTML",
        )
        success, summary_text, document_file, _ = await _collect_weather_data(detected_icao, explicit_date=detected_date)

        if success and summary_text:
            unified_report = f"{orderbook_block}\n\n{'━' * 22}\n\n{summary_text}"
            try:
                await status_msg.edit_text(unified_report, parse_mode="HTML", reply_markup=trade_markup)
            except Exception as e_edit:
                logger.warning(f"Ошибка edit_text с HTML: {e_edit}, пробуем plain-text")
                plain_report = html.unescape(re.sub(r"<[^>]+>", "", unified_report))
                await status_msg.edit_text(plain_report, parse_mode=None, reply_markup=trade_markup)

            await target_message.answer_document(
                document=document_file,
                caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{document_file.filename}</code>",
                parse_mode="HTML",
            )
            return

    try:
        await target_message.answer(orderbook_block, parse_mode="HTML", reply_markup=trade_markup)
    except Exception:
        plain_ob = html.unescape(re.sub(r"<[^>]+>", "", orderbook_block))
        await target_message.answer(plain_ob, parse_mode=None, reply_markup=trade_markup)
    await target_message.answer(
        "🌍 <b>Город не распознан автоматически.</b> Выбери его из списка ниже:",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


async def _execute_weather_pipeline(user_query: str, target_message: Message):
    status_msg = await target_message.answer(
        f"🔍 <i>Сбор данных и генерация RAW Data Package для {user_query}...</i>",
        parse_mode="HTML",
    )

    try:
        success, summary_text, document_file, err_msg = await _collect_weather_data(user_query)
        if not success:
            await status_msg.edit_text(err_msg, parse_mode="HTML")
            return

        await status_msg.delete()
        await target_message.answer(summary_text, parse_mode="HTML")
        await target_message.answer_document(
            document=document_file,
            caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{document_file.filename}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Ошибка при обработке запроса: {e}", exc_info=True)
        await status_msg.edit_text("❌ <b>Произошла ошибка при обработке запроса.</b>", parse_mode="HTML")


@router.message(F.text)
async def process_weather_request(message: Message):
    user_text = message.text.strip()
    register_subscriber(message.from_user.id, message.from_user.username or "")

    # Если отправлен публичный EVM-адрес кошелька (0x...)
    if user_text.startswith("0x") and len(user_text) == 42:
        await _handle_public_wallet_input(message, user_text)
        return

    coords = _parse_coordinates(user_text)

    if coords:
        lat, lon = coords
        try:
            tz_name = await asyncio.to_thread(tf.timezone_at, lng=lon, lat=lat) or "UTC"
            local_tz = zoneinfo.ZoneInfo(tz_name)
            target_date_local = datetime.now(local_tz).strftime("%Y-%m-%d")

            custom_location_data = {
                "icao": "GEO",
                "name": f"Координаты [{lat:.4f}, {lon:.4f}]",
                "city": f"Точка {lat:.3f}, {lon:.3f}",
                "country": "GEO",
                "latitude": lat,
                "longitude": lon,
                "timezone": tz_name,
            }

            raw_forecast_payload = await asyncio.to_thread(
                fetch_openmeteo_forecast, lat, lon, tz_name, target_date_local
            )

            synth_result = (
                await asyncio.to_thread(synthesize_forecast, raw_forecast_payload)
                if isinstance(raw_forecast_payload, dict)
                else {"success": False, "error": "Нет данных"}
            )

            noaa_payload = {
                "metar": {"available": True, "raw": f"GEO {lat:.4f}/{lon:.4f}", "temp_c": None},
                "taf": {"available": False, "raw": "N/A"},
            }

            summary_text = build_summary_caption(custom_location_data, synth_result, noaa_payload, target_date_local)
            package_dict = build_raw_data_package_dict(custom_location_data, raw_forecast_payload, synth_result, noaa_payload)
            json_bytes = json.dumps(package_dict, ensure_ascii=False, indent=2).encode("utf-8")
            clean_filename = f"weather_package_{abs(lat):.2f}_{abs(lon):.2f}_{target_date_local}.json"
            document_file = BufferedInputFile(file=json_bytes, filename=clean_filename)

            await message.answer(summary_text, parse_mode="HTML")
            await message.answer_document(
                document=document_file,
                caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{clean_filename}</code>",
                parse_mode="HTML",
            )
            return
        except Exception as e:
            logger.error(f"Ошибка координат: {e}")
            await message.answer("❌ <b>Ошибка при обработке координат.</b>", parse_mode="HTML")
            return

    user_query = user_text.upper()
    if len(user_query) == 4 and user_query.isalpha():
        await _execute_weather_pipeline(user_query, message)
        return

    await message.answer(
        "⚠️ <b>Формат не распознан.</b>\n\n"
        "• Отправь <b>4-значный ICAO-код</b> (например: <code>EGLC</code> или <code>KJFK</code>)\n"
        "• Отправь <b>координаты</b> (например: <code>48.52, 135.18</code>)\n"
        "• Нажми <b>«📌 Мои позиции»</b> для контроля открытых сделок\n"
        "• Или нажми <b>«🔍 Сканировать маркет»</b> для анализа ссылки.",
        parse_mode="HTML",
    )