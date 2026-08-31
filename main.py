import asyncio
from datetime import datetime
import json
import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import zoneinfo

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web

import config
from airport_resolver import resolve_airport
from database import get_all_active_positions, init_db
from handlers import (
    ALL_RADAR_CITIES,
    active_radar_cities,
    router,
)
from middlewares import ThrottlingMiddleware
from noaa_service import get_noaa_package
from openmeteo_service import fetch_openmeteo_forecast

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("WeatherBotMain")

POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com/events"

# Кэш кулдауна алертов (ключ -> timestamp)
last_alert_sent: Dict[str, float] = {}
previous_wind_dirs: Dict[str, int] = {}

# Память дневного максимума: ключ 'ICAO_YYYY-MM-DD' -> max_temp_float
daily_max_records: Dict[str, float] = {}


async def setup_bot_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="🚀 Главное меню и статус бота"),
        BotCommand(command="positions", description="📌 Мои открытые сделки"),
        BotCommand(command="scan", description="🔍 Сканировать маркет (Preddy / Polymarket)"),
        BotCommand(command="radar", description="⚙️ Панель радара аномалий"),
        BotCommand(command="cities", description="🌍 Быстрый выбор избранных городов"),
        BotCommand(command="help", description="📖 Справка по командам"),
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("📋 Интерактивное меню команд зарегистрировано.")
    except Exception as error:
        logger.warning(f"⚠️ Ошибка регистрации команд: {error}")


async def run_health_check_server() -> None:
    async def handle_ping(request: web.Request) -> web.Response:
        return web.Response(text="OK: Weather Alpha Bot is running 24/7", status=200)

    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Health-Check сервер запущен на порту {port}.")


def _extract_number_from_outcome(title: str) -> Optional[float]:
    """Извлекает числовое значение температуры из названия исхода (например, '24°C', '82-83°F')."""
    match = re.search(r"(\d+(?:\.\d+)?)", title)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


async def _fetch_city_market(city_keyword: str) -> Optional[Dict[str, Any]]:
    """Бесплатно забирает активный маркет из Polymarket Gamma API."""
    url = f"{POLYMARKET_GAMMA_API}?limit=5&active=true&closed=false&tag_id=weather"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5.0)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if isinstance(data, list):
                    for ev in data:
                        title_clean = ev.get("title", "").lower()
                        if city_keyword.lower() in title_clean:
                            return ev
                return None
    except Exception:
        return None


async def check_multi_model_mispricing(bot: Bot, icao: str, admin_id: Optional[int]) -> None:
    """
    Сканер копеечных (Penny 1-5¢) и недооцененных (Value 6-20¢) аномалий по 4 моделям.
    """
    airport_data = resolve_airport(icao)
    if not airport_data:
        return

    lat = airport_data.get("lat")
    lon = airport_data.get("lon")
    tz_name = airport_data.get("timezone", "UTC")
    city_name = ALL_RADAR_CITIES.get(icao, icao)

    try:
        local_date = datetime.now(zoneinfo.ZoneInfo(tz_name)).strftime("%Y-%m-%d")
    except Exception:
        local_date = datetime.now().strftime("%Y-%m-%d")

    # 1. Запрашиваем метеомодели
    forecast_data = await asyncio.to_thread(
        fetch_openmeteo_forecast, lat, lon, tz_name, local_date
    )
    if not forecast_data:
        return

    model_peaks: Dict[str, float] = {}
    primaries = forecast_data.get("primary_models", {})
    secondaries = forecast_data.get("secondary_models", {})

    for m_key, m_val in {**primaries, **secondaries}.items():
        if m_val.get("status", {}).get("available"):
            d_met = m_val.get("derived_metrics") or {}
            max_t = d_met.get("max_temp_c")
            if max_t is not None:
                model_peaks[m_key] = float(max_t)

    if not model_peaks:
        return

    # 2. Ищем открытый маркет на Polymarket
    city_search_tag = airport_data.get("city", "")
    market_event = await _fetch_city_market(city_search_tag)
    if not market_event:
        return

    markets = market_event.get("markets", [])
    current_time = asyncio.get_event_loop().time()

    for item in markets:
        outcome_title = item.get("groupItemTitle") or item.get("question", "")
        prices_raw = item.get("outcomePrices", '["0", "0"]')
        
        try:
            prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            yes_price = float(prices[0]) if len(prices) > 0 else 0.0
        except Exception:
            yes_price = 0.0

        outcome_temp = _extract_number_from_outcome(outcome_title)
        if outcome_temp is None or yes_price <= 0.0:
            continue

        price_cents = round(yes_price * 100, 1)

        # Проверяем, какая из моделей попадает в этот исход (с допуском +- 0.6°C)
        matching_models = [
            f"{m_name.upper()} ({m_peak}°C)"
            for m_name, m_peak in model_peaks.items()
            if abs(m_peak - outcome_temp) <= 0.6
        ]

        if not matching_models:
            continue

        models_str = ", ".join(matching_models)

        # ---------------------------------------------------------
        # ТИП 1: 🎯 Penny Lottery Alpha (1.0¢ - 5.0¢)
        # ---------------------------------------------------------
        if yes_price <= 0.05:
            alert_key = f"{icao}_penny_{outcome_title}"
            if current_time - last_alert_sent.get(alert_key, 0) > 7200:
                last_alert_sent[alert_key] = current_time
                potential_mult = int(1.0 / yes_price)
                penny_text = (
                    f"🎯 <b>PENNY LOTTERY ALPHA | КОПЕЕЧНАЯ АНОМАЛИЯ</b>\n\n"
                    f"📍 <b>Локация:</b> {city_name}\n"
                    f"🏷️ <b>Исход:</b> <code>{outcome_title}</code>\n"
                    f"💰 <b>Цена токена:</b> <code>{price_cents}¢</code> (${yes_price:.2f}) | Потенциал: <b>x{potential_mult}</b>\n"
                    f"🔬 <b>Сигнал моделей:</b> {models_str}\n\n"
                    f"🎯 <b>ЧТО ДЕЛАТЬ:</b>\n"
                    f"• Рынок списал исход в ноль, но физика видит его реализацию.\n"
                    f"• Выдели <b>строго $1.00</b> (Penny Cap) для входа на Preddy / Polymarket."
                )
                if admin_id:
                    try:
                        await bot.send_message(chat_id=admin_id, text=penny_text, parse_mode="HTML")
                    except Exception:
                        pass

        # ---------------------------------------------------------
        # ТИП 2: 💎 Value Mispricing Alpha (6.0¢ - 20.0¢)
        # ---------------------------------------------------------
        elif 0.05 < yes_price <= 0.20:
            alert_key = f"{icao}_value_{outcome_title}"
            if current_time - last_alert_sent.get(alert_key, 0) > 7200:
                last_alert_sent[alert_key] = current_time
                roi_val = int(((1.0 / yes_price) - 1.0) * 100)
                value_text = (
                    f"💎 <b>VALUE MISPRICING ALPHA | НЕДООЦЕНЕННЫЙ ИСХОД</b>\n\n"
                    f"📍 <b>Локация:</b> {city_name}\n"
                    f"🏷️ <b>Исход:</b> <code>{outcome_title}</code>\n"
                    f"💰 <b>Цена токена:</b> <code>{price_cents}¢</code> (${yes_price:.2f}) | Профит: <b>+{roi_val}%</b>\n"
                    f"🔬 <b>Сигнал моделей:</b> {models_str}\n\n"
                    f"🎯 <b>ЧТО ДЕЛАТЬ:</b>\n"
                    f"• Отличный дисконт. Рассмотри добавление в коридор или покупку по сетке Tier ($1.00 – $2.00)."
                )
                if admin_id:
                    try:
                        await bot.send_message(chat_id=admin_id, text=value_text, parse_mode="HTML")
                    except Exception:
                        pass


async def background_radar_worker(bot: Bot) -> None:
    logger.info("📡 Фоновый Радар Аномалий и Страж Позиций запущен.")
    admin_id = getattr(config, "ADMIN_CHAT_ID", None)

    while True:
        try:
            # Опрос каждые 15 минут
            await asyncio.sleep(900)

            current_timestamp = asyncio.get_event_loop().time()
            active_positions = await asyncio.to_thread(get_all_active_positions)

            target_icaos = set(active_radar_cities)
            for pos in active_positions:
                target_icaos.add(pos["icao"])

            if not target_icaos:
                continue

            for icao in list(target_icaos):
                # 1. Запуск сканера копеечных и недооцененных аномалий (Penny + Value)
                await check_multi_model_mispricing(bot, icao, admin_id)

                # 2. Метеоконтроль через METAR
                noaa_data = await asyncio.to_thread(get_noaa_package, icao)
                metar = noaa_data.get("metar", {})

                if not metar.get("available"):
                    continue

                temp_c = metar.get("temp_c")
                raw_metar = metar.get("raw", "")
                wind_dir = metar.get("wind_dir_degrees")
                wind_speed = metar.get("wind_speed_kts") or 0
                city_name = ALL_RADAR_CITIES.get(icao, icao)

                airport_data = resolve_airport(icao) or {}
                tz_name = airport_data.get("timezone", "UTC")
                try:
                    local_dt = datetime.now(zoneinfo.ZoneInfo(tz_name))
                except Exception:
                    local_dt = datetime.now()

                local_date_str = local_dt.strftime("%Y-%m-%d")
                local_hour = local_dt.hour
                local_time_str = local_dt.strftime("%H:%M")

                record_key = f"{icao}_{local_date_str}"
                if temp_c is not None:
                    current_record = daily_max_records.get(record_key, -999.0)
                    if temp_c > current_record:
                        daily_max_records[record_key] = float(temp_c)

                day_peak_record = daily_max_records.get(record_key, temp_c)

                # ⚡ Front Crash Alert
                has_storm = any(sig in raw_metar for sig in ["TS", "CB", "SQ", "+RA", "GR"])
                has_strong_gusts = "G" in raw_metar and any(int(g) >= 25 for g in re.findall(r"G(\d{2})KT", raw_metar))

                if has_storm or has_strong_gusts:
                    alert_key = f"{icao}_front_crash"
                    if current_timestamp - last_alert_sent.get(alert_key, 0) > 3600:
                        last_alert_sent[alert_key] = current_timestamp
                        crash_msg = (
                            f"⚡ <b>FRONT CRASH ALERT | РЕЗКИЙ СЛОМ ПОГОДЫ</b>\n\n"
                            f"📍 <b>Локация:</b> {city_name}\n"
                            f"🌡️ <b>Температура сейчас:</b> <code>{temp_c}°C</code> (Пик дня: <code>{day_peak_record}°C</code>)\n"
                            f"⚠️ <b>Физика:</b> Холодный фронт / порывы ветра > 25 kt. Прогрев заблокирован!\n\n"
                            f"🎯 <b>ЧТО ДЕЛАТЬ:</b>\n"
                            f"• Если твоя сделка на высокие температуры — <b>жми CASHOUT</b>."
                        )
                        for pos in active_positions:
                            if pos["icao"] == icao:
                                try:
                                    await bot.send_message(chat_id=pos["user_id"], text=crash_msg, parse_mode="HTML")
                                except Exception:
                                    pass
                        if admin_id:
                            try:
                                await bot.send_message(chat_id=admin_id, text=crash_msg, parse_mode="HTML")
                            except Exception:
                                pass

                # 💨 Wind Shift Detector (JFK)
                if isinstance(wind_dir, int):
                    prev_dir = previous_wind_dirs.get(icao)
                    previous_wind_dirs[icao] = wind_dir

                    if icao == "KJFK" and prev_dir is not None:
                        is_onshore = (110 <= wind_dir <= 190) and (wind_speed >= 8)
                        was_offshore = 220 <= prev_dir <= 310
                        if is_onshore and was_offshore:
                            shift_key = f"{icao}_wind_shift"
                            if current_timestamp - last_alert_sent.get(shift_key, 0) > 7200:
                                last_alert_sent[shift_key] = current_timestamp
                                shift_msg = (
                                    f"💨 <b>WIND SHIFT DETECTOR | МОРСКОЙ БРИЗ (JFK)</b>\n\n"
                                    f"📍 <b>Локация:</b> {city_name}\n"
                                    f"🌊 Ветер сменился на океанический бриз (<code>{wind_dir}° / {wind_speed} kt</code>).\n"
                                    f"🎯 <b>ЧТО ДЕЛАТЬ:</b> Пик зафиксирован на <code>{day_peak_record}°C</code>. Не докупай верхние исходы."
                                )
                                for pos in active_positions:
                                    if pos["icao"] == icao:
                                        try:
                                            await bot.send_message(chat_id=pos["user_id"], text=shift_msg, parse_mode="HTML")
                                        except Exception:
                                            pass

                # ☀️ Solar Peak Window
                if local_hour >= 17 and temp_c is not None:
                    solar_key = f"{icao}_solar_peak"
                    if current_timestamp - last_alert_sent.get(solar_key, 0) > 21600:
                        last_alert_sent[solar_key] = current_timestamp
                        for pos in active_positions:
                            if pos["icao"] == icao:
                                solar_msg = (
                                    f"☀️ <b>SOLAR PEAK WINDOW | ОКНО ПРОГРЕВА ЗАКРЫТО</b>\n\n"
                                    f"📍 <b>Локация:</b> {city_name} (Время: <code>{local_time_str} LT</code>)\n"
                                    f"🏆 <b>Фактический рекорд дня:</b> <code>{day_peak_record}°C</code>\n"
                                    f"📌 <b>Твои купленные исходы:</b> <code>{pos['outcomes']}</code>\n\n"
                                    f"🎯 <b>ЧТО ДЕЛАТЬ:</b>\n"
                                    f"• Если твой исход совпадает с <code>{int(round(day_peak_record))}°C</code> — держи до расчета.\n"
                                    f"• Если рекорд дня ниже твоих исходов — солнце село, роста больше не будет."
                                )
                                try:
                                    await bot.send_message(chat_id=pos["user_id"], text=solar_msg, parse_mode="HTML")
                                except Exception:
                                    pass

        except asyncio.CancelledError:
            logger.info("📡 Фоновый воркер радара остановлен.")
            break
        except Exception as err:
            logger.error(f"⚠️ Ошибка в цикле фонового радара: {err}", exc_info=True)
            await asyncio.sleep(60)


async def start_bot_with_retry(bot: Bot, dp: Dispatcher, max_retries: int = 5) -> None:
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"🔄 Подключение к Telegram API ({attempt}/{max_retries})...")
            await bot.delete_webhook(drop_pending_updates=True)
            await setup_bot_commands(bot)

            radar_task = asyncio.create_task(background_radar_worker(bot))

            logger.info("🚀 УСПЕШНО! Бот слушает команды.")
            await dp.start_polling(bot)
            
            radar_task.cancel()
            break
        except Exception as error:
            logger.warning(f"⚠️ Сбой связи: {error}")
            if attempt < max_retries:
                await asyncio.sleep(3.0)
            else:
                logger.error("❌ Превышен лимит попыток подключения.", exc_info=True)


async def main() -> None:
    if not config.BOT_TOKEN:
        logger.critical("❌ КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не обнаружен!")
        return

    init_db()
    logger.info("🗄️ База данных SQLite (positions.db) инициализирована.")

    await run_health_check_server()

    session = AiohttpSession(proxy=config.PROXY_URL) if config.PROXY_URL else None
    bot = Bot(
        token=config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    dp.message.middleware(ThrottlingMiddleware())
    dp.include_router(router)

    try:
        await start_bot_with_retry(bot, dp)
    finally:
        await bot.session.close()
        logger.info("🛑 Сессия бота закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("👋 Бот остановлен.")