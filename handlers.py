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
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

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


def _parse_market_identifier(url_or_text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Универсальный парсер: извлекает ID или SLUG из ссылок Preddy Web, Preddy TMA и Polymarket.
    Возвращает пару: (тип_параметра, значение).
    """
    text = url_or_text.strip()

    # 1. Формат Preddy Web: preddy.trade/event/.../<id> (например: 883962)
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

    # 4. Чистый ID или Slug, переданный напрямую текстом
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


async def _execute_weather_pipeline(user_query: str, target_message: Message):
    """Единый пайплайн сбора погодных моделей и NOAA для текстового ввода и инлайн-кнопок."""
    status_msg = await target_message.answer(
        f"🔍 <i>Сбор данных и генерация RAW Data Package для {user_query}...</i>",
        parse_mode="HTML",
    )

    try:
        # 1. Резолвинг аэропорта в отдельном потоке (защита Event Loop)
        airport_data = await asyncio.to_thread(resolve_airport, user_query)
        if not airport_data:
            await status_msg.edit_text(
                f"❌ <b>Ошибка:</b> Аэропорт с ICAO-кодом <code>{user_query}</code> не найден в базе данных.",
                parse_mode="HTML",
            )
            return

        lat, lon, tz_name = _extract_airport_coords(airport_data)
        if lat is None or lon is None or not tz_name:
            await status_msg.edit_text(
                f"⚠️ <b>Ошибка:</b> Неполные метаданные для аэропорта <code>{user_query}</code>.",
                parse_mode="HTML",
            )
            return

        try:
            local_tz = zoneinfo.ZoneInfo(tz_name)
            target_date_local = datetime.now(local_tz).strftime("%Y-%m-%d")
        except Exception as e:
            logger.error(f"Недопустимый часовой пояс '{tz_name}' для {user_query}: {e}")
            await status_msg.edit_text(
                f"⚠️ <b>Ошибка:</b> Недопустимый часовой пояс <code>{tz_name}</code>.",
                parse_mode="HTML",
            )
            return

        # 2. Параллельный сбор Open-Meteo и NOAA
        async def _get_openmeteo():
            return await asyncio.to_thread(
                fetch_openmeteo_forecast, lat, lon, tz_name, target_date_local
            )

        async def _get_noaa():
            return await asyncio.to_thread(get_noaa_package, user_query)

        raw_forecast_payload, noaa_payload = await asyncio.gather(
            _get_openmeteo(), _get_noaa(), return_exceptions=True
        )

        if isinstance(raw_forecast_payload, Exception) or not isinstance(raw_forecast_payload, dict):
            raw_forecast_payload = {}
        if isinstance(noaa_payload, Exception) or not isinstance(noaa_payload, dict):
            noaa_payload = {}

        # 3. Синтез прогноза
        synth_result = (
            await asyncio.to_thread(synthesize_forecast, raw_forecast_payload)
            if raw_forecast_payload
            else {"success": False, "error": "Нет данных"}
        )

        # 4. Формирование Summary текста и RAW JSON пакета
        summary_text = build_summary_caption(
            airport_data, synth_result, noaa_payload, target_date_local
        )

        package_dict = build_raw_data_package_dict(
            airport_data, raw_forecast_payload, synth_result, noaa_payload
        )

        # In-Memory сериализация JSON без создания временных файлов на диске
        json_bytes = json.dumps(package_dict, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"weather_package_{user_query}_{target_date_local}.json"
        document_file = BufferedInputFile(file=json_bytes, filename=filename)

        # 5. Отправка сообщений пользователю
        await status_msg.delete()
        await target_message.answer(summary_text, parse_mode="HTML")
        await target_message.answer_document(
            document=document_file,
            caption=f"📦 <b>RAW DATA PACKAGE:</b> <code>{filename}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        logger.error(f"Ошибка при обработке запроса '{user_query}': {e}", exc_info=True)
        await status_msg.edit_text(
            "❌ <b>Произошла ошибка при обработке запроса.</b>",
            parse_mode="HTML",
        )


# -------------------------------------------------------------
# БЛОК 1: Базовые команды и меню
# -------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = (
        "👋 <b>Weather & Alpha Bot активен!</b>\n\n"
        "🔹 <b>Анализ погоды:</b> нажми <b>«🌍 Избранные города»</b> или отправь любой ICAO-код (например, <code>KJFK</code>).\n\n"
        "🔹 <b>Сканер маркетов Preddy / Polymarket:</b>\n"
        "• Отправь команду: <code>/scan &lt;ссылка&gt;</code>\n"
        "• Или нажми кнопку <b>«🔍 Сканировать маркет»</b> внизу."
    )
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(Command("help"))
@router.message(F.text == "📖 Справка / Помощь")
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>Справка по работе с ботом:</b>\n\n"
        "1. <b>Быстрый выбор городов:</b>\n"
        "   Нажми кнопку <b>«🌍 Избранные города»</b> и выбери нужный аэропорт.\n\n"
        "2. <b>Метеосводки по коду:</b>\n"
        "   Введи любой 4-буквенный ICAO-код вручную — бот пришлет сводку и JSON-пакет.\n\n"
        "3. <b>Сканер стаканов (/scan):</b>\n"
        "   Введи <code>/scan &lt;ссылка&gt;</code> — бот выведет стакан цен и чистый ROI."
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(F.text == "🌍 Избранные города")
@router.message(Command("cities"))
async def cmd_cities_menu(message: Message):
    await message.answer(
        "🌍 <b>Выбери город для моментального метеопакета:</b>",
        parse_mode="HTML",
        reply_markup=cities_inline_keyboard,
    )


@router.message(F.text == "🔍 Сканировать маркет")
async def btn_scan_hint(message: Message):
    await message.answer(
        "📥 <b>Отправь команду со ссылкой на событие:</b>\n\n"
        "<code>/scan https://preddy.trade/event/highest-temperature/883962</code>\n\n"
        "<i>Поддерживаются ссылки Preddy Web, Preddy TMA и Polymarket.</i>",
        parse_mode="HTML",
    )


# -------------------------------------------------------------
# БЛОК 2: Обработка нажатий на инлайн-кнопки городов
# -------------------------------------------------------------

@router.callback_query(F.data.startswith("icao:"))
async def process_city_callback(callback: CallbackQuery):
    icao_code = callback.data.split(":")[1]
    await callback.answer(f"Сбор данных для {icao_code}...")
    await _execute_weather_pipeline(icao_code, callback.message)


# -------------------------------------------------------------
# БЛОК 3: Сканирование Polymarket и Preddy (/scan)
# -------------------------------------------------------------

@router.message(Command("scan"))
@router.edited_message(Command("scan"))
async def cmd_scan_market(message: Message):
    args = (message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Укажи ссылку после команды:</b>\n"
            "<code>/scan https://preddy.trade/event/highest-temperature/883962</code>",
            parse_mode="HTML",
        )
        return

    raw_input = args[1]
    param_type, param_val = _parse_market_identifier(raw_input)

    if not param_type or not param_val:
        await message.answer("❌ Не удалось распознать ID или slug маркета. Проверь ссылку.", parse_mode="HTML")
        return

    status_msg = await message.answer(
        f"⚡ <i>Считываю стакан маркета ({param_type.upper()}: {param_val})...</i>",
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

    report_lines.append("\n💡 <i>Сверь эти исходы с правилом Worst-Case ROI (≥30%) и сводкой METAR.</i>")

    await status_msg.edit_text("\n".join(report_lines), parse_mode="HTML")


# -------------------------------------------------------------
# БЛОК 4: Ручной ввод 4-значного ICAO-кода
# -------------------------------------------------------------

@router.message(F.text)
async def process_weather_request(message: Message):
    user_query = message.text.strip().upper()

    if len(user_query) != 4 or not user_query.isalpha():
        await message.answer(
            "⚠️ Введите <b>4-значный ICAO-код</b> (например: <code>KJFK</code>) "
            "или выберите город в меню <b>«🌍 Избранные города»</b>.",
            parse_mode="HTML",
        )
        return

    await _execute_weather_pipeline(user_query, message)