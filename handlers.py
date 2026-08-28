import asyncio
from datetime import datetime
import json
import logging
import re
from typing import Any, Dict, Optional, Tuple
import zoneinfo

import aiohttp
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
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
        [KeyboardButton(text="📖 Справка / Помощь")],
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
    ]
)

# Словарь сопоставления ключевых слов маркета с ICAO-кодами городов
CITY_KEYWORD_MAP = {
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

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com/events"


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


def _parse_coordinates(text: str) -> Optional[Tuple[float, float]]:
    """Распознает координаты: '55.75, 37.61' или '55.75 37.61' с валидацией диапазонов."""
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
    """
    Универсальный парсер: извлекает ID или SLUG из ссылок Preddy Web, Preddy TMA и Polymarket.
    Возвращает пару: (тип_параметра, значение).
    """
    text = url_or_text.strip()

    # 1. Формат Preddy Web: preddy.trade/event/.../<id>
    preddy_id_match = re.search(r"preddy\.trade/event/[^/]+/(\d+)", text)
    if preddy_id_match:
        return "id", preddy_id_match.group(1)

    # 2. Формат Preddy Telegram Mini App: startapp=<slug_или_id>
    tma_match = re.search(r"startapp=([a-zA-Z0-9_-]+)", text)
    if tma_match:
        val = tma_match.group(1)
        return ("id" if val.isdigit() else "slug"), val

    # 3. Формат Polymarket Event: polymarket.com/event/<slug>
    poly_match = re.search(r"polymarket\.com/event/([a-zA-Z0-9_-]+)", text)
    if poly_match:
        return "slug", poly_match.group(1)

    # 4. Чистый ID или Slug
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


async def _collect_weather_data(user_query: str) -> Tuple[bool, Optional[str], Optional[BufferedInputFile], Optional[str]]:
    """Внутренний модуль сбора метеоданных: формирует текст сводки и RAW JSON файл."""
    airport_data = await asyncio.to_thread(resolve_airport, user_query)
    if not airport_data:
        return False, None, None, f"❌ Аэропорт с кодом <code>{user_query}</code> не найден в базе данных."

    lat, lon, tz_name = _extract_airport_coords(airport_data)
    if lat is None or lon is None or not tz_name:
        return False, None, None, f"⚠️ Неполные метаданные для аэропорта <code>{user_query}</code>."

    try:
        local_tz = zoneinfo.ZoneInfo(tz_name)
        target_date_local = datetime.now(local_tz).strftime("%Y-%m-%d")
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
    """Пайплайн для прямого запроса погоды по ICAO (кнопка в меню или ввод текста)."""
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
    """Единый пайплайн: объединяет стакан и метеосводку в ОДИН цельный дашборд."""
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

    # Автоопределение города по названию/ссылке
    search_context = f"{title} {slug} {description} {raw_input}"
    detected_icao = _detect_city_icao(search_context)

    if detected_icao:
        await status_msg.edit_text(
            f"⚡ <i>Котировки получены! Считываю метеомодели и METAR для {detected_icao}...</i>",
            parse_mode="HTML",
        )
        success, summary_text, document_file, _ = await _collect_weather_data(detected_icao)

        if success and summary_text:
            # Склеиваем стакан и метеосводку в одно сообщение
            unified_report = f"{orderbook_block}\n\n{'━' * 22}\n\n{summary_text}"
            await status_msg.edit_text(unified_report, parse_mode="HTML")
            await target_message.answer_document(
                document=document_file,
                caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{document_file.filename}</code>",
                parse_mode="HTML",
            )
            return

    # Если город не определен автоматически — показываем стакан и кнопки городов
    await status_msg.edit_text(orderbook_block, parse_mode="HTML")
    await target_message.answer(
        "🌍 <b>Город не распознан автоматически.</b> Выбери его из списка ниже:",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


# -------------------------------------------------------------
# БЛОК 1: Базовые команды и меню
# -------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 <b>Weather & Alpha Bot активен!</b>\n\n"
        "🔹 <b>Анализ погоды:</b>\n"
        "• Отправь 4-значный ICAO-код (например, <code>KJFK</code>)\n"
        "• Или отправь координаты (например: <code>48.35, 11.78</code>)\n"
        "• Или нажми <b>«🌍 Избранные города»</b>\n\n"
        "🔹 <b>Сканер маркетов:</b>\n"
        "• Нажми <b>«🔍 Сканировать маркет»</b> и отправь ссылку."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(Command("help"))
@router.message(F.text == "📖 Справка / Помощь")
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    help_text = (
        "📖 <b>Справка по работе с ботом:</b>\n\n"
        "1. <b>Сканирование стаканов:</b>\n"
        "   Нажми <b>«🔍 Сканировать маркет»</b> и отправь ссылку на событие.\n\n"
        "2. <b>Поиск по координатам:</b>\n"
        "   Отправь широту и долготу через запятую (например: <code>55.75, 37.61</code>).\n\n"
        "3. <b>Метеосводки по коду:</b>\n"
        "   Введи 4-буквенный ICAO-код вручную или выбери в <b>«🌍 Избранные города»</b>."
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(F.text == "🌍 Избранные города")
@router.message(Command("cities"))
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


# -------------------------------------------------------------
# БЛОК 2: Интерактивный запуск сканирования и FSM
# -------------------------------------------------------------

@router.message(F.text == "🔍 Сканировать маркет")
async def btn_scan_trigger(message: Message, state: FSMContext):
    await state.set_state(MarketScanStates.waiting_for_link)
    await message.answer(
        "📥 <b>Режим сканирования активирован!</b>\n\n"
        "Отправь ссылку на погодный маркет из <b>Preddy</b> или <b>Polymarket</b> следующим сообщением 👇",
        parse_mode="HTML",
    )


@router.message(Command("scan"))
@router.edited_message(Command("scan"))
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


# Обработка сообщения, когда бот ждет ссылку в состоянии FSM
@router.message(MarketScanStates.waiting_for_link, F.text)
async def process_market_link_input(message: Message, state: FSMContext):
    user_text = message.text.strip()

    # Сброс режима ожидания при нажатии системных кнопок
    if user_text == "🌍 Избранные города":
        await state.clear()
        await cmd_cities_menu(message, state)
        return
    if user_text in ["📖 Справка / Помощь", "/help"]:
        await state.clear()
        await cmd_help(message, state)
        return
    if user_text.startswith("/start"):
        await state.clear()
        await cmd_start(message, state)
        return
    if user_text == "🔍 Сканировать маркет":
        await message.answer("Жду ссылку на маркет. Просто вставь её сюда 👇", parse_mode="HTML")
        return

    await state.clear()
    await _execute_scan_pipeline(user_text, message)


# -------------------------------------------------------------
# БЛОК 3: Обработка ICAO-кодов и Координат
# -------------------------------------------------------------

@router.message(F.text)
async def process_weather_request(message: Message):
    user_text = message.text.strip()

    # 1. Проверка ввода координат (например: 48.35, 11.78 или 48.35 11.78)
    coords = _parse_coordinates(user_text)
    if coords:
        lat, lon = coords
        await _execute_coordinates_pipeline(lat, lon, message)
        return

    # 2. Проверка 4-значного ICAO-кода
    user_query = user_text.upper()
    if len(user_query) == 4 and user_query.isalpha():
        await _execute_weather_pipeline(user_query, message)
        return

    # 3. Подсказка при некорректном формате
    await message.answer(
        "⚠️ <b>Формат не распознан.</b>\n\n"
        "• Отправь <b>4-значный ICAO-код</b> (например: <code>KJFK</code>)\n"
        "• Отправь <b>координаты</b> (например: <code>55.75, 37.61</code>)\n"
        "• Или нажми <b>«🔍 Сканировать маркет»</b> для анализа ссылки.",
        parse_mode="HTML",
    )