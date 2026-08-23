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
from aiogram.types import BufferedInputFile, KeyboardButton, Message, ReplyKeyboardMarkup

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

# Главное постоянное меню с кнопками
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔍 Сканировать маркет"), KeyboardButton(text="📖 Справка / Помощь")]
    ],
    resize_keyboard=True,
)

POLYMARKET_API = "https://gamma-api.polymarket.com/events"


def _extract_airport_coords(
    airport_data: Dict[str, Any],
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """Извлекает географические координаты и часовой пояс из данных аэропорта."""
    if not airport_data:
        return None, None, None
    lat = airport_data.get("lat") if airport_data.get("lat") is not None else airport_data.get("latitude")
    lon = airport_data.get("lon") if airport_data.get("lon") is not None else airport_data.get("longitude")
    tz = airport_data.get("timezone") or airport_data.get("iana_timezone")
    return lat, lon, tz


def _extract_slug(url_or_text: str) -> str:
    """Извлекает идентификатор маркета (slug) из ссылок Polymarket, Preddy TMA или чистого текста."""
    url_or_text = url_or_text.strip()
    # Поиск slug в ссылках Polymarket (/event/slug-name) или Preddy TMA (startapp=slug-name)
    event_match = re.search(r"(?:event/|startapp=)([a-zA-Z0-9_-]+)", url_or_text)
    if event_match:
        return event_match.group(1)

    # Очистка строки, если передан прямой slug без протокола https://
    clean_slug = re.sub(r"[^a-zA-Z0-9_-]", "", url_or_text)
    return clean_slug


async def _fetch_polymarket_orderbook(slug: str) -> Optional[Dict[str, Any]]:
    """Бесплатно забирает котировки стакана через официальный открытый Gamma API."""
    url = f"{POLYMARKET_API}?slug={slug}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                if isinstance(data, list) and len(data) > 0:
                    return data[0]
                return None
    except Exception as error:
        logger.error(f"Ошибка при обращении к Polymarket Gamma API ({slug}): {error}")
        return None


# -------------------------------------------------------------
# БЛОК 1: Базовые команды и меню
# -------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = (
        "👋 <b>Weather Alpha Bot активен!</b>\n\n"
        "🔹 <b>Анализ погоды:</b> отправь 4-значный ICAO-код аэропорта.\n"
        "• Например: <code>UHHH</code> (Хабаровск), <code>KJFK</code> (Нью-Йорк).\n\n"
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
        "1. <b>Метеопакеты (ICAO):</b>\n"
        "   Введи 4 буквы кода аэропорта (например: <code>UHHH</code>) — бот пришлет сводку моделей (ECMWF, GFS, ICON, GEM) и прикрепит JSON-файл для анализатора.\n\n"
        "2. <b>Сканер стаканов (/scan):</b>\n"
        "   Введи <code>/scan https://polymarket.com/event/...</code> или ссылку из <b>Preddy TMA</b> — бот мгновенно выведет актуальные цены исходов и потенциал выплат."
    )
    await message.answer(help_text, parse_mode="HTML", reply_markup=main_keyboard)


@router.message(F.text == "🔍 Сканировать маркет")
async def btn_scan_hint(message: Message):
    await message.answer(
        "📥 <b>Как просканировать маркет:</b>\n\n"
        "Отправь команду со ссылкой на событие в формате:\n"
        "<code>/scan https://polymarket.com/event/highest-temperature-in-chicago-on-august-23</code>\n\n"
        "<i>Поддерживаются ссылки с Polymarket и Preddy.</i>",
        parse_mode="HTML",
    )


# -------------------------------------------------------------
# БЛОК 2: Сканирование Polymarket и Preddy (/scan)
# -------------------------------------------------------------

@router.message(Command("scan"))
async def cmd_scan_market(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "⚠️ <b>Укажи ссылку после команды:</b>\n"
            "<code>/scan https://polymarket.com/event/...</code>",
            parse_mode="HTML",
        )
        return

    raw_input = args[1]
    slug = _extract_slug(raw_input)

    if not slug:
        await message.answer("❌ Не удалось распознать маркет. Проверь ссылку.", parse_mode="HTML")
        return

    status_msg = await message.answer(
        "⚡ <i>Считываю стакан и актуальные котировки...</i>",
        parse_mode="HTML",
    )

    event_data = await _fetch_polymarket_orderbook(slug)

    if not event_data:
        await status_msg.edit_text(
            "❌ <b>Маркет не найден</b> или Polymarket API временно недоступен. Проверь корректность ссылки.",
            parse_mode="HTML",
        )
        return

    title = event_data.get("title", "Погодный маркет Polymarket")
    markets = event_data.get("markets", [])

    if not markets:
        await status_msg.edit_text("⚠️ В этом событии нет активных котировок.", parse_mode="HTML")
        return

    report_lines = [
        f"📊 <b>{title}</b>\n",
        "<b>Котировки исходов (Стакан):</b>"
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

    report_lines.append("\n💡 <i>Сверь эти котировки с METAR и правилом Worst-Case ROI перед входом.</i>")

    await status_msg.edit_text("\n".join(report_lines), parse_mode="HTML")


# -------------------------------------------------------------
# БЛОК 3: Обработка ICAO метеозапросов (4-значный код)
# -------------------------------------------------------------

@router.message(F.text)
async def process_weather_request(message: Message):
    user_query = message.text.strip().upper()

    # Валидация входных данных (Strict 4-letter ICAO format)
    if len(user_query) != 4 or not user_query.isalpha():
        await message.answer(
            "⚠️ Пожалуйста, введите корректный <b>4-значный ICAO-код</b> аэропорта (например: <code>UHHH</code>, <code>KJFK</code>) "
            "или используйте команду <code>/scan &lt;ссылка&gt;</code>.",
            parse_mode="HTML",
        )
        return

    status_msg = await message.answer(
        f"🔍 <i>Сбор данных и генерация RAW Data Package для {user_query}...</i>",
        parse_mode="HTML",
    )

    try:
        # 1. Резолвинг аэропорта в отдельном потоке (DevSecOps: защита Event Loop)
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

        # In-Memory сериализация JSON без сохранения на диск (Zero-Disk Footprint)
        json_bytes = json.dumps(package_dict, ensure_ascii=False, indent=2).encode("utf-8")
        filename = f"weather_package_{user_query}_{target_date_local}.json"
        document_file = BufferedInputFile(file=json_bytes, filename=filename)

        # 5. Отправка сообщений пользователю
        await status_msg.delete()
        await message.answer(summary_text, parse_mode="HTML")
        await message.answer_document(
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