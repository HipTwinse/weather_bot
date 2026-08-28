import asyncio
from datetime import datetime
import json
import logging
import re
from typing import Any, Dict, Optional, Set, Tuple
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

from airport_resolver import resolve_airport
from noaa_service import get_noaa_package
from openmeteo_service import fetch_openmeteo_forecast
from weather_synthesizer import (
    build_raw_data_package_dict,
    build_summary_caption,
    synthesize_forecast,
)

logger = logging.getLogger(__name__)
router = Router()

# Офлайн-движок поиска часовых поясов по координатам
tf = TimezoneFinder()

# Глобальное хранилище активных городов для радара
active_radar_cities: Set[str] = {"EGLC", "LFPB", "EDDM"}

# База отслеживаемых городов для радара
ALL_RADAR_CITIES = {
    "EGLC": "🇬🇧 Лондон (EGLC)",
    "LFPB": "🇫🇷 Париж (LFPB)",
    "EDDM": "🇩🇪 Мюнхен (EDDM)",
    "KJFK": "🇺🇸 Нью-Йорк (KJFK)",
    "RJTT": "🇯🇵 Токио (RJTT)",
    "RKSI": "🇰🇷 Сеул (RKSI)",
    "ZSPD": "🇨🇳 Шанхай (ZSPD)",
    "LEMD": "🇪🇸 Мадрид (LEMD)",
    "LIMC": "🇮🇹 Милан (LIMC)",
    "LTAC": "🇹🇷 Анкара (LTAC)",
    "NZWN": "🇳🇿 Веллингтон (NZWN)",
    "UHHH": "🇷🇺 Хабаровск (UHHH)",
}


# Состояние ожидания ссылки на маркет (FSM)
class MarketScanStates(StatesGroup):
    waiting_for_link = State()


# Главное постоянное меню Telegram
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="🔍 Сканировать маркет"),
            KeyboardButton(text="🌍 Избранные города"),
        ],
        [
            KeyboardButton(text="⚙️ Радар аномалий"),
            KeyboardButton(text="📖 Справка / Помощь"),
        ],
    ],
    resize_keyboard=True,
)

# Интерактивная клавиатура с избранными торговыми городами
cities_inline_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 Лондон (EGLC)", callback_data="icao:EGLC"),
            InlineKeyboardButton(text="🇫🇷 Париж (LFPB)", callback_data="icao:LFPB"),
        ],
        [
            InlineKeyboardButton(text="🇯🇵 Токио (RJTT)", callback_data="icao:RJTT"),
            InlineKeyboardButton(text="🇰🇷 Сеул (RKSI)", callback_data="icao:RKSI"),
        ],
        [
            InlineKeyboardButton(text="🇨🇳 Шанхай (ZSPD)", callback_data="icao:ZSPD"),
            InlineKeyboardButton(text="🇩🇪 Мюнхен (EDDM)", callback_data="icao:EDDM"),
        ],
        [
            InlineKeyboardButton(text="🇪🇸 Мадрид (LEMD)", callback_data="icao:LEMD"),
            InlineKeyboardButton(text="🇮🇹 Милан (LIMC)", callback_data="icao:LIMC"),
        ],
        [
            InlineKeyboardButton(text="🇹🇷 Анкара (LTAC)", callback_data="icao:LTAC"),
            InlineKeyboardButton(text="🇳🇿 Веллингтон (NZWN)", callback_data="icao:NZWN"),
        ],
        [
            InlineKeyboardButton(text="🇷🇺 Хабаровск (UHHH)", callback_data="icao:UHHH"),
        ],
    ]
)

# Словарь сопоставления ключевых слов маркета с ICAO-кодами городов
CITY_KEYWORD_MAP = {
    "UHHH": ["khabarovsk", "хабаровск", "uhhh", "новый"],
    "EGLC": ["london", "лондон", "eglc", "city airport", "heathrow", "gatwick"],
    "LFPB": ["paris", "париж", "lfpb", "le bourget", "charles de gaulle", "orly"],
    "RJTT": ["tokyo", "токио", "rjtt", "haneda", "narita"],
    "RKSI": ["seoul", "сеул", "rksi", "incheon", "gimpo"],
    "ZSPD": ["shanghai", "шанхай", "zspd", "pudong", "hongqiao"],
    "EDDM": ["munich", "мюнхен", "eddm"],
    "LEMD": ["madrid", "мадрид", "lemd", "barajas"],
    "LIMC": ["milan", "милан", "limc", "malpensa", "linate"],
    "LTAC": ["ankara", "анкара", "ltac", "esenboga"],
    "NZWN": ["wellington", "веллингтон", "nzwn"],
    "KJFK": ["new york", "нью-йорк", "nyc", "kjfk", "jfk"],
    "KORD": ["chicago", "чикаго", "kord", "ohare"],
    "KMIA": ["miami", "майами", "kmia"],
    "KLAX": ["los angeles", "лос-анджелес", "klax", "lax"],
}

# Словарь месяцев для парсинга дат из заголовков маркетов
MONTH_NAMES = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com/events"


def get_radar_keyboard() -> InlineKeyboardMarkup:
    """Динамически генерирует кнопки включения/выключения радара."""
    buttons = []
    row = []
    for icao, name in ALL_RADAR_CITIES.items():
        is_active = icao in active_radar_cities
        status_icon = "🟢 ВКЛ" if is_active else "🔴 ВЫКЛ"
        btn = InlineKeyboardButton(
            text=f"{status_icon} {name}",
            callback_data=f"toggle_radar:{icao}",
        )
        row.append(btn)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _extract_airport_coords(
    airport_data: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Извлекает координаты и таймзону аэропорта из базы данных."""
    if not airport_data:
        return None, None, None
    lat = airport_data.get("lat") if airport_data.get("lat") is not None else airport_data.get("latitude")
    lon = airport_data.get("lon") if airport_data.get("lon") is not None else airport_data.get("longitude")
    tz = airport_data.get("timezone") or airport_data.get("iana_timezone")
    return lat, lon, tz


def _detect_city_icao(text_context: str) -> Optional[str]:
    """Автоматически находит ICAO-код города в названии, описании или ссылке события."""
    normalized = text_context.lower()
    for icao, keywords in CITY_KEYWORD_MAP.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", normalized):
                return icao
    return None


def _detect_market_target_date(text_context: str) -> Optional[str]:
    """Извлекает дату маркета из названия или слага (например, 'August 29' -> '2026-08-29')."""
    normalized = text_context.lower()
    
    # 1. Формат "August 29" или "Aug 29"
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

    # 2. Формат ISO YYYY-MM-DD в ссылке/слаге
    iso_match = re.search(r"\b(202\d-\d{2}-\d{2})\b", normalized)
    if iso_match:
        return iso_match.group(1)

    return None


def _parse_coordinates(text: str) -> Optional[Tuple[float, float]]:
    """Распознает координаты: '55.75, 37.61' с валидацией диапазонов."""
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
    """Извлекает ID или SLUG из ссылок Preddy Web, Preddy TMA и Polymarket."""
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


async def _fetch_polymarket_orderbook(param_type: str, param_val: str) -> Optional[Dict[str, Any]]:
    """Бесплатно и безопасно забирает стакан через официальный шлюз Polymarket Gamma API."""
    url = f"{POLYMARKET_GAMMA_API}?{param_type}={param_val}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6.0)) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                if isinstance(data, list) and len(data) > 0:
                    return data[0]
                elif isinstance(data, dict) and "markets" in data:
                    return data
                return None
    except Exception as error:
        logger.error(f"Сбой при запросе к Polymarket API ({param_type}={param_val}): {error}")
        return None


async def _collect_weather_data(user_query: str, explicit_date: Optional[str] = None) -> Tuple[bool, Optional[str], Optional[BufferedInputFile], Optional[str]]:
    """Внутренний модуль сбора метеоданных: формирует текст сводки и RAW JSON файл с учетом даты маркета."""
    airport_data = await asyncio.to_thread(resolve_airport, user_query)
    if not airport_data:
        return False, None, None, f"❌ Аэропорт с кодом <code>{user_query}</code> не найден в базе данных."

    lat, lon, tz_name = _extract_airport_coords(airport_data)
    if lat is None or lon is None or not tz_name:
        return False, None, None, f"⚠️ Неполные метаданные для аэропорта <code>{user_query}</code>."

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


async def _execute_coordinates_pipeline(lat: float, lon: float, target_message: Message):
    """Пайплайн сбора метеоданных по географическим координатам (Широта, Долгота)."""
    status_msg = await target_message.answer(
        f"🧭 <i>Определяю часовой пояс и собираю метеомодели для [{lat:.4f}, {lon:.4f}]...</i>",
        parse_mode="HTML",
    )

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

        if not isinstance(raw_forecast_payload, dict):
            raw_forecast_payload = {}

        synth_result = (
            await asyncio.to_thread(synthesize_forecast, raw_forecast_payload)
            if raw_forecast_payload
            else {"success": False, "error": "Нет данных"}
        )

        noaa_payload = {
            "metar": {
                "available": True,
                "raw": f"GEO {lat:.4f}/{lon:.4f} (Анализ по сетке численных моделей)",
                "temp_c": None,
                "dewpoint_c": None,
                "wind_speed_kts": None,
            },
            "taf": {"available": False, "raw": "TAF доступен только для официальных станций ICAO"},
        }

        summary_text = build_summary_caption(
            custom_location_data, synth_result, noaa_payload, target_date_local
        )

        package_dict = build_raw_data_package_dict(
            custom_location_data, raw_forecast_payload, synth_result, noaa_payload
        )

        json_bytes = json.dumps(package_dict, ensure_ascii=False, indent=2).encode("utf-8")
        clean_filename = f"weather_package_coords_{abs(lat):.2f}_{abs(lon):.2f}_{target_date_local}.json"
        document_file = BufferedInputFile(file=json_bytes, filename=clean_filename)

        await status_msg.delete()
        await target_message.answer(summary_text, parse_mode="HTML")
        await target_message.answer_document(
            document=document_file,
            caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{clean_filename}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Ошибка при обработке координат ({lat}, {lon}): {e}", exc_info=True)
        await status_msg.edit_text("❌ <b>Произошла ошибка при обработке координат.</b>", parse_mode="HTML")


async def _execute_weather_pipeline(user_query: str, target_message: Message):
    """Пайплайн для прямого запроса погоды по ICAO."""
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
        logger.error(f"Ошибка при обработке запроса '{user_query}': {e}", exc_info=True)
        await status_msg.edit_text("❌ <b>Произошла ошибка при обработке запроса.</b>", parse_mode="HTML")


async def _execute_scan_pipeline(raw_input: str, target_message: Message):
    """Единый пайплайн: считывает стакан, вычисляет точную дату маркета и подгружает соответствующий прогноз."""
    param_type, param_val = _parse_market_identifier(raw_input)

    if not param_type or not param_val:
        await target_message.answer(
            "❌ <b>Не удалось распознать ссылку.</b>\n"
            "Убедись, что отправляешь корректную ссылку с Preddy или Polymarket.",
            parse_mode="HTML",
        )
        return

    status_msg = await target_message.answer(
        f"⚡ <i>Считываю котировки маркета ({param_type.upper()}: {param_val})...</i>",
        parse_mode="HTML",
    )

    event_data = await _fetch_polymarket_orderbook(param_type, param_val)

    if not event_data:
        await status_msg.edit_text(
            "❌ <b>Маркет не найден</b> или API временно недоступен. Проверь ссылку.",
            parse_mode="HTML",
        )
        return

    title = event_data.get("title", "Погодный маркет")
    slug = event_data.get("slug", "")
    description = event_data.get("description", "")
    markets = event_data.get("markets", [])

    if not markets:
        await status_msg.edit_text("⚠️ В этом событии нет активных котировок.", parse_mode="HTML")
        return

    report_lines = [
        f"📊 <b>{title}</b>\n",
        "<b>Текущие котировки исходов (Стакан):</b>"
    ]

    for item in markets:
        question = item.get("groupItemTitle") or item.get("question", "Исход")
        prices_str = item.get("outcomePrices", '["0", "0"]')

        try:
            prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str
            yes_price = float(prices[0]) if len(prices) > 0 else 0.0
        except Exception:
            yes_price = 0.0

        if yes_price > 0.0:
            shares_per_dollar = 1.0 / yes_price
            roi = (shares_per_dollar - 1.0) * 100
            price_cents = round(yes_price * 100, 1)
            report_lines.append(
                f"• <b>{question}</b>: <code>{price_cents}¢</code> (${yes_price:.2f}) | Потенциал: <b>+{roi:.0f}%</b>"
            )
        else:
            report_lines.append(f"• <b>{question}</b>: <code>0¢</code> (Нет ликвидности)")

    orderbook_block = "\n".join(report_lines)

    # Автоопределение города и целевой даты маркета
    search_context = f"{title} {slug} {description} {raw_input}"
    detected_icao = _detect_city_icao(search_context)
    detected_date = _detect_market_target_date(search_context)

    if detected_icao:
        date_label = f" на {detected_date}" if detected_date else ""
        await status_msg.edit_text(
            f"⚡ <i>Котировки получены! Считываю метеомодели для {detected_icao}{date_label}...</i>",
            parse_mode="HTML",
        )
        success, summary_text, document_file, _ = await _collect_weather_data(detected_icao, explicit_date=detected_date)

        if success and summary_text:
            unified_report = f"{orderbook_block}\n\n{'━' * 22}\n\n{summary_text}"
            await status_msg.edit_text(unified_report, parse_mode="HTML")
            await target_message.answer_document(
                document=document_file,
                caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{document_file.filename}</code>",
                parse_mode="HTML",
            )
            return

    await status_msg.edit_text(orderbook_block, parse_mode="HTML")
    await target_message.answer(
        "🌍 <b>Город не распознан автоматически.</b> Выбери его из списка ниже:",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


# -------------------------------------------------------------
# БЛОК 1: Приоритетные системные команды и кнопки меню
# -------------------------------------------------------------

@router.message(CommandStart(), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 <b>Weather & Alpha Bot активен!</b>\n\n"
        "🔹 <b>Анализ погоды:</b>\n"
        "• Отправь 4-значный ICAO-код (например, <code>KJFK</code> или <code>UHHH</code>)\n"
        "• Или отправь координаты (например: <code>48.52, 135.18</code>)\n"
        "• Или нажми <b>«🌍 Избранные города»</b>\n\n"
        "🔹 <b>Сканер маркетов:</b>\n"
        "• Нажми <b>«🔍 Сканировать маркет»</b> и отправь ссылку.\n\n"
        "🔹 <b>Управление радаром:</b>\n"
        "• Нажми <b>«⚙️ Радар аномалий»</b> для выбора отслеживаемых городов."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(Command("help"), StateFilter("*"))
@router.message(F.text == "📖 Справка / Помощь", StateFilter("*"))
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = (
        "📖 <b>Справка по работе с ботом:</b>\n\n"
        "1. <b>Сканирование стаканов:</b>\n"
        "   Нажми <b>«🔍 Сканировать маркет»</b> и отправь ссылку на событие.\n\n"
        "2. <b>Радар аномалий:</b>\n"
        "   Нажми <b>«⚙️ Радар аномалий»</b> и переключай города тумблерами ВКЛ/ВЫКЛ.\n\n"
        "3. <b>Метеосводки по коду или координатам:</b>\n"
        "   Введи 4-буквенный ICAO-код или отправь координаты (например: <code>55.75, 37.61</code>)."
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(F.text == "⚙️ Радар аномалий", StateFilter("*"))
@router.message(Command("radar"), StateFilter("*"))
async def cmd_radar_settings(message: Message, state: FSMContext):
    await state.clear()
    active_count = len(active_radar_cities)
    text = (
        f"⚙️ <b>Панель управления Радаром аномалий</b>\n\n"
        f"📡 Сейчас активно городов: <b>{active_count} из {len(ALL_RADAR_CITIES)}</b>\n\n"
        "Нажимай на кнопки ниже, чтобы включать (🟢) или выключать (🔴) фоновое отслеживание стаканов и METAR:"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=get_radar_keyboard())


@router.message(F.text == "🌍 Избранные города", StateFilter("*"))
@router.message(Command("cities"), StateFilter("*"))
async def cmd_cities_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🌍 <b>Выбери город для моментального метеопакета:</b>",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


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

    await state.clear()
    await _execute_scan_pipeline(args[1], message)


# -------------------------------------------------------------
# БЛОК 2: Инлайн-колбэки (Кнопки городов и Радара)
# -------------------------------------------------------------

@router.callback_query(F.data.startswith("icao:"))
async def process_city_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    icao_code = callback.data.split(":")[1]
    await callback.answer(f"Сбор данных для {icao_code}...")
    await _execute_weather_pipeline(icao_code, callback.message)


@router.callback_query(F.data.startswith("toggle_radar:"))
async def process_toggle_radar(callback: CallbackQuery):
    icao_code = callback.data.split(":")[1]
    
    if icao_code in active_radar_cities:
        active_radar_cities.remove(icao_code)
        await callback.answer(f"🔴 {icao_code} отключен от радара")
    else:
        active_radar_cities.add(icao_code)
        await callback.answer(f"🟢 {icao_code} включен в радар")
        
    active_count = len(active_radar_cities)
    text = (
        f"⚙️ <b>Панель управления Радаром аномалий</b>\n\n"
        f"📡 Сейчас активно городов: <b>{active_count} из {len(ALL_RADAR_CITIES)}</b>\n\n"
        "Нажимай на кнопки ниже, чтобы включать (🟢) или выключать (🔴) фоновое отслеживание стаканов и METAR:"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_radar_keyboard())


# -------------------------------------------------------------
# БЛОК 3: Обработка ссылки в FSM-режиме ожидания
# -------------------------------------------------------------

@router.message(MarketScanStates.waiting_for_link, F.text)
async def process_market_link_input(message: Message, state: FSMContext):
    user_text = message.text.strip()
    await state.clear()
    await _execute_scan_pipeline(user_text, message)


# -------------------------------------------------------------
# БЛОК 4: Обработка ICAO-кодов и Координат (Ввод текста)
# -------------------------------------------------------------

@router.message(F.text)
async def process_weather_request(message: Message):
    user_text = message.text.strip()

    coords = _parse_coordinates(user_text)
    if coords:
        lat, lon = coords
        await _execute_coordinates_pipeline(lat, lon, message)
        return

    user_query = user_text.upper()
    if len(user_query) == 4 and user_query.isalpha():
        await _execute_weather_pipeline(user_query, message)
        return

    await message.answer(
        "⚠️ <b>Формат не распознан.</b>\n\n"
        "• Отправь <b>4-значный ICAO-код</b> (например: <code>KJFK</code> или <code>UHHH</code>)\n"
        "• Отправь <b>координаты</b> (например: <code>48.52, 135.18</code>)\n"
        "• Или нажми <b>«🔍 Сканировать маркет»</b> для анализа ссылки.",
        parse_mode="HTML",
    )