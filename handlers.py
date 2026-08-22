import asyncio
from datetime import datetime
import json
import logging
from typing import Any, Dict, Optional, Tuple
import zoneinfo

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message

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


@router.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = (
        "👋 <b>Привет! Я бот сбора погодных пакетов (Weather Data Package).</b>\n\n"
        "Я собираю полные данные 4 мировых метеомоделей (ECMWF, GFS, ICON, GEM) и сводки NOAA METAR/TAF.\n\n"
        "📌 <b>Как пользоваться:</b>\n"
        "Отправь 4-значный ICAO-код аэропорта.\n"
        "• Например: <code>UHHH</code> (Хабаровск), <code>UUEE</code> (Шереметьево), <code>KJFK</code> (Нью-Йорк).\n\n"
        "Справка: /help"
    )
    await message.answer(welcome_text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📖 <b>Справка:</b>\n\n"
        "1. Введи 4-буквенный ICAO-код аэропорта (например: <code>UHHH</code>).\n"
        "2. Бот пришлет краткую сводку и прикрепит файл <b>JSON</b> с полным 24-часовым массивом данных.\n"
        "3. Прикрепленный JSON-файл готов для загрузки в Gemini Analyzer."
    )
    await message.answer(help_text, parse_mode="HTML")


@router.message(F.text)
async def process_weather_request(message: Message):
    user_query = message.text.strip().upper()

    # Валидация входных данных (Strict ICAO format)
    if len(user_query) != 4 or not user_query.isalpha():
        await message.answer(
            "⚠️ Пожалуйста, введите корректный <b>4-значный ICAO-код</b> аэропорта (например: <code>UHHH</code>, <code>UUEE</code>).",
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