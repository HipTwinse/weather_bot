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
from database import add_position, delete_position, get_user_positions
from noaa_service import get_noaa_package
from openmeteo_service import fetch_openmeteo_forecast
from polymarket_service import (
    find_city_weather_event,
    parse_markets_orderbook,
    extract_temp_value,
    fetch_event_by_id_or_slug,
    POLYMARKET_GAMMA_API,
)
from weather_synthesizer import (
    build_raw_data_package_dict,
    build_summary_caption,
    synthesize_forecast,
)
from gemini_analyzer import analyze_city_weather_ai, is_gemini_configured
from auto_scanner import get_priority_target

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
            KeyboardButton(text="📌 Мои позиции"),
        ],
        [
            KeyboardButton(text="🌍 Избранные города"),
            KeyboardButton(text="📖 Справка / Регламент v7.1"),
        ],
    ],
    resize_keyboard=True,
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

    status_msg = await callback.message.reply(
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
    target_val, priority_model = get_priority_target(icao, models_max, avg_peak, raw_metar)

    # Расчет остатка инсоляции
    local_hour = local_dt.hour + local_dt.minute / 60.0
    rem_hours = max(0.0, 17.0 - local_hour)

    # 2. Разбор стакана котировок Polymarket
    orderbook = parse_markets_orderbook(event_data.get("markets", [])) if event_data else []

    orderbook_lines = []
    favorite_candidate = None
    basket_sum = 0.0
    is_overheated = False

    if orderbook:
        orderbook_lines.append("📊 <b>Текущие котировки (Стакан Polymarket):</b>")
        for item in orderbook:
            t_val = item["temp"]
            p_cents = item["price_cents"]
            title = item["title"]

            # Ищем совпадение с целевой температурой
            is_target = t_val is not None and abs(t_val - target_val) <= 0.6
            tag = " 🎯 <b>(ЦЕЛЬ)</b>" if is_target else ""

            orderbook_lines.append(f"• <code>{title}</code>: <b>{p_cents:.0f}¢</b> (${item['yes_price']:.2f}){tag}")

            if is_target:
                favorite_candidate = item
                basket_sum += p_cents

        if basket_sum >= 80.0 or (favorite_candidate and favorite_candidate["price_cents"] >= 80.0):
            is_overheated = True
    else:
        orderbook_lines.append("⚠️ <i>Активный контракт на Polymarket для этой даты пока не опубликован или закрыт.</i>")

    # 3. Выработка стратегии по правилам KB v7.1
    is_rain = any(s in raw_metar for s in ["RA", "DZ", "TS", "SN"]) and "OVC" in raw_metar

    if is_rain or is_overheated:
        strategy_block = (
            "⛔ <b>ВЕРДИКТ: СКИП МАРКЕТА</b>\n"
            "⚠️ <b>ПОКУПКА ОДИНОЧНОГО СТРАЙКА ЗДЕСЬ = СЛИВ ДЕПОЗИТА.</b> Рынок перегрет маркетмейкером, сиди на заборе."
        )
    elif favorite_candidate and 25.0 <= favorite_candidate["price_cents"] <= 48.0 and rem_hours >= 3.0:
        strategy_block = (
            f"🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b>\n"
            f"• <b>Рекомендуемый исход:</b> <code>{favorite_candidate['title']}</code> (цена <b>{favorite_candidate['price_cents']:.0f}¢</b>)\n"
            f"• <b>Запас инсоляции:</b> {rem_hours:.1f} ч | Приоритет: {priority_model}\n"
            f"• <b>Цель:</b> продажа токена толпе на дневном разгоне (+25%...+40% или 60¢–70¢), а не удержание до ночи!"
        )
    else:
        # Корзинный вход
        base_t = int(round(target_val))
        strategy_block = (
            f"🟢 <b>СИГНАЛ: СВЯЗКА КОРЗИНОЙ (MOMENTUM CASHOUT)</b>\n"
            f"• <b>Базовый страйк:</b> <code>{base_t}°C</code> + опцион <code>{base_t+1}°C</code> (сумма связки ≤ 75¢)\n"
            f"• <b>Тейк-профит:</b> Сброс корзины лимитными ордерами при росте на +25%...+40% до 13:30 LT."
        )

    response_text = (
        f"⚡ <b>ЭКСПРЕСС-АНАЛИЗ: {city_label}</b>\n"
        f"🕒 <i>Время: {local_dt.strftime('%H:%M')} LT | Дата: {target_date}</i>\n\n"
        f"🌡️ <b>Факт METAR:</b> <code>{temp_c if temp_c is not None else 'Н/Д'}°C</code>\n"
        f"📊 <b>Модели:</b> ECMWF: {models_max.get('ecmwf_hres', 'Н/Д')}°C | GFS: {models_max.get('gfs_global', 'Н/Д')}°C | ICON: {models_max.get('icon_global', 'Н/Д')}°C\n"
        f"🎯 <b>Расчетный пик:</b> <b>{avg_peak}°C</b> (Опора: <i>{priority_model}</i>)\n\n"
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
            "rate_str": f"{round(target_val - (temp_c or target_val), 1)}°C/остаток",
            "rem_hours_str": f"{rem_hours:.1f} ч",
            "orderbook": orderbook,
        }
        try:
            ai_verdict = await analyze_city_weather_ai(city_pack, scenario="A")
            if ai_verdict:
                response_text = ai_verdict
        except Exception as e:
            logger.warning(f"Ошибка вызова Gemini AI: {e}")

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
    welcome_text = (
        "👋 <b>Weather Alpha Engine v7.1 активен!</b>\n\n"
        "🔹 <b>Анализ погоды:</b> Отправь 4-значный ICAO-код (например, <code>EGLC</code>, <code>KJFK</code>) или координаты.\n"
        "🔹 <b>Сканер маркетов:</b> Нажми <b>«🔍 Сканировать маркет»</b> и отправь ссылку с Preddy / Polymarket.\n"
        "🔹 <b>Мои позиции:</b> Нажми <b>«📌 Мои позиции»</b> для взятия сделок под защиту автосканера.\n"
        "🔹 <b>Быстрый выбор:</b> Нажми <b>«🌍 Избранные города»</b>."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(Command("help"), StateFilter("*"))
@router.message(F.text == "📖 Справка / Регламент v7.1", StateFilter("*"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = (
        "📖 <b>Справка Weather Alpha Engine v7.1:</b>\n\n"
        "1. <b>Консолидированный дайджест:</b> раз в 30 минут с 10:00 до 00:00 ХБР бот присылает единую сводку по Лондону, Парижу, Милану и Мадриду с кнопками моментального анализа стакана.\n"
        "2. <b>Sniper Momentum (одиночный вход):</b> вход в один исход разрешен при цене 25¢–48¢, запасе солнца >= 3ч и подтверждении приоритетной модели города.\n"
        "3. <b>Дисциплина Тейк-Профита:</b> при росте купленного токена на >= +35% или цене >= 60¢ — немедленно фиксируй прибыль лимиткой в стакан!\n"
        "4. <b>Тайм-стоп 13:30 LT:</b> если к полудню цель не пробита — сброс остаточной стоимости в рынок."
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(Command("ai", "gemini"), StateFilter("*"))
async def cmd_ai_status(message: Message, state: FSMContext):
    await state.clear()
    if is_gemini_configured():
        text = (
            "🤖 <b>AI Квант-Синоптик Gemini v7.4 АКТИВЕН!</b>\n\n"
            "Все экспресс-сканы рынков обрабатываются нейросетью Google Gemini с применением синоптической базы, "
            "анализа полного стакана Polymarket и тактических Pro-Tips.\n\n"
            "Выбери город ниже для мгновенного AI-анализа с кнопками перехода в Preddy:"
        )
    else:
        text = (
            "ℹ️ <b>AI Квант-Синоптик Gemini v7.4</b>\n\n"
            "Чтобы активировать встроенный AI-анализ прямо в боте, добавь в файл <code>.env</code> бесплатный ключ:\n"
            "<code>GEMINI_API_KEY=ваш_ключ</code>\n\n"
            "🔑 Получить ключ можно бесплатно в Google AI Studio:\n"
            "https://aistudio.google.com/app/apikey\n\n"
            "<i>(Сейчас бот работает на надежном локальном алгоритме синтеза).</i>"
        )
    await message.answer(text, parse_mode="HTML", reply_markup=cities_inline_keyboard)


@router.message(F.text == "📌 Мои позиции", StateFilter("*"))
@router.message(Command("positions"), StateFilter("*"))
async def cmd_my_positions(message: Message, state: FSMContext):
    await state.clear()
    positions = get_user_positions(message.from_user.id)

    if not positions:
        text = (
            "📌 <b>У тебя пока нет активных сделок на контроле.</b>\n\n"
            "Нажми <b>«➕ Добавить сделку»</b>, чтобы сканер каждые 30 минут отслеживал PnL, "
            "сигнализировал о Тейк-Профите (+35% / 60¢) и тайм-стопе 13:30 LT!"
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
                text=f"❌ Закрыть: {pos['icao']} ({pos['outcomes']})",
                callback_data=f"del_pos:{pos['id']}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="➕ Добавить еще сделку", callback_data="add_new_pos")])

    await message.answer(
        "\n".join(text_lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
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
    await state.set_state(AddPositionStates.waiting_for_outcomes)

    city_name = ALL_RADAR_CITIES.get(icao_code, icao_code)
    await callback.message.edit_text(
        f"🎯 <b>Шаг 2 из 2: Локация {city_name}</b>\n\n"
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

    add_position(
        user_id=message.from_user.id,
        icao=icao,
        outcomes=outcomes_str,
        target_date=target_date,
        entry_price=entry_price
    )

    await state.clear()
    city_name = ALL_RADAR_CITIES.get(icao, icao)
    entry_info = f" по цене <b>{entry_price:.0f}¢</b>" if entry_price > 0 else ""

    await message.answer(
        f"✅ <b>Позиция успешно добавлена под защиту сканера!</b>\n\n"
        f"📍 <b>Город:</b> {city_name}\n"
        f"🎯 <b>Исход:</b> <code>{outcomes_str}</code>{entry_info}\n"
        f"📅 <b>Дата:</b> <code>{target_date}</code>\n\n"
        f"🛡️ <i>Каждые 30 минут сканер будет сопоставлять стакан Polymarket, факт METAR и темп инсоляции. "
        f"При росте на +35% или цене >= 60¢ ты получишь сигнал на немедленный Тейк-Профит!</i>",
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


@router.message(F.text == "🌍 Избранные города", StateFilter("*"))
@router.message(Command("cities"), StateFilter("*"))
async def cmd_cities_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🌍 <b>Выбери город для моментального метеопакета:</b>",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


@router.callback_query(F.data.startswith("icao:"))
async def process_city_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    icao_code = callback.data.split(":")[1]
    await callback.answer(f"Сбор данных для {icao_code}...")
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