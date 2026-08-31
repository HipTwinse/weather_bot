import asyncio
from datetime import datetime
import logging
import os
import re
import sys
from typing import Dict
import zoneinfo

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("WeatherBotMain")

# Кэш для защиты от спама (ключ: ICAO или ICAO_alert_type -> timestamp)
last_alert_sent: Dict[str, float] = {}
previous_wind_dirs: Dict[str, int] = {}


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


async def background_radar_worker(bot: Bot) -> None:
    """
    Фоновый радар-страж:
    1. Проверяет активные сделки пользователей (Front Crash, Wind Shift, Solar Peak).
    2. Сканирует включенные города на предмет общих аномалий прогрева.
    """
    logger.info("📡 Фоновый Радар Аномалий и Страж Позиций запущен.")
    admin_id = getattr(config, "ADMIN_CHAT_ID", None)

    while True:
        try:
            # Опрос каждые 15 минут
            await asyncio.sleep(900)

            current_timestamp = asyncio.get_event_loop().time()
            active_positions = await asyncio.to_thread(get_all_active_positions)

            # Собираем список всех станций для опроса (города из радара + города из открытых сделок)
            target_icaos = set(active_radar_cities)
            for pos in active_positions:
                target_icaos.add(pos["icao"])

            if not target_icaos:
                continue

            for icao in list(target_icaos):
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

                local_hour = local_dt.hour
                local_time_str = local_dt.strftime("%H:%M")

                # -------------------------------------------------------------
                # ФИЧА 1: ⚡ Front Crash Alert (Резкие порывы ветра и грозовые ячейки)
                # -------------------------------------------------------------
                has_storm = any(sig in raw_metar for sig in ["TS", "CB", "SQ", "+RA", "GR"])
                has_strong_gusts = "G" in raw_metar and any(int(g) >= 25 for g in re.findall(r"G(\d{2})KT", raw_metar))

                if has_storm or has_strong_gusts:
                    alert_key = f"{icao}_front_crash"
                    if current_timestamp - last_alert_sent.get(alert_key, 0) > 3600:
                        last_alert_sent[alert_key] = current_timestamp
                        crash_msg = (
                            f"⚡ <b>FRONT CRASH ALERT | РЕЗКИЙ СЛОМ ПОГОДЫ</b>\n\n"
                            f"📍 <b>Локация:</b> {city_name}\n"
                            f"🌡️ <b>Температура:</b> <code>{temp_c}°C</code>\n"
                            f"⚠️ <b>Опасность:</b> Зафиксирован грозовой фронт или порывы > 25 kt. "
                            f"Возможен резкий обвал температуры!\n\n"
                            f"📝 <code>{raw_metar}</code>\n\n"
                            f"🛑 <b>Рекомендация:</b> Проверь позиции и рассмотри досрочный <b>CASHOUT</b>."
                        )

                        # Оповещаем владельцев сделок по этому городу
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

                # -------------------------------------------------------------
                # ФИЧА 2: 💨 Wind Shift Detector (Разворот на морской бриз)
                # -------------------------------------------------------------
                if isinstance(wind_dir, int):
                    prev_dir = previous_wind_dirs.get(icao)
                    previous_wind_dirs[icao] = wind_dir

                    # Для Нью-Йорка (KJFK): разворот с суши (240-310) на холодный океан (110-190)
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
                                    f"🌊 Ветер сменился на океанический бриз (<code>{wind_dir}° / {wind_speed} kt</code>). "
                                    f"Прогрев датчика заблокирован!\n\n"
                                    f"📝 <code>{raw_metar}</code>"
                                )
                                for pos in active_positions:
                                    if pos["icao"] == icao:
                                        try:
                                            await bot.send_message(chat_id=pos["user_id"], text=shift_msg, parse_mode="HTML")
                                        except Exception:
                                            pass

                # -------------------------------------------------------------
                # ФИЧА 3: ☀️ Solar Peak Window (Закрытие окна солнечного прогрева)
                # -------------------------------------------------------------
                if local_hour >= 17 and temp_c is not None:
                    solar_key = f"{icao}_solar_peak"
                    if current_timestamp - last_alert_sent.get(solar_key, 0) > 21600:
                        last_alert_sent[solar_key] = current_timestamp
                        for pos in active_positions:
                            if pos["icao"] == icao:
                                solar_msg = (
                                    f"☀️ <b>SOLAR PEAK WINDOW | ОКНО ПРОГРЕВА ЗАКРЫТО</b>\n\n"
                                    f"📍 <b>Локация:</b> {city_name} (Местное время: <code>{local_time_str}</code>)\n"
                                    f"🌡️ <b>Текущий факт:</b> <code>{temp_c}°C</code> | Твои исходы: <code>{pos['outcomes']}</code>\n"
                                    f"📉 Инсоляция пошла на спад. Дальнейший рост маловероятен. "
                                    f"Рекомендуется зафиксировать результат."
                                )
                                try:
                                    await bot.send_message(chat_id=pos["user_id"], text=solar_msg, parse_mode="HTML")
                                except Exception:
                                    pass

                # -------------------------------------------------------------
                # ФИЧА 4: Базовый радар чистого неба (Общие сигналы для админа)
                # -------------------------------------------------------------
                if temp_c is not None and ("CAVOK" in raw_metar or "NCD" in raw_metar or "CLR" in raw_metar):
                    general_key = f"{icao}_cavok"
                    if current_timestamp - last_alert_sent.get(general_key, 0) > 7200:
                        last_alert_sent[general_key] = current_timestamp
                        alert_text = (
                            f"🚨 <b>РАДАР АНОМАЛИЙ | АКТИВНЫЙ ПРОГРЕВ</b>\n\n"
                            f"📍 <b>Локация:</b> {city_name}\n"
                            f"🌡️ <b>Факт METAR:</b> <code>{temp_c}°C</code> (Время: {local_time_str} LT)\n"
                            f"☀️ <b>Условия:</b> Чистое небо (CAVOK), активная инсоляция.\n\n"
                            f"📝 <code>{raw_metar}</code>"
                        )
                        if admin_id and (icao in active_radar_cities):
                            try:
                                await bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
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
            logger.info(f"🔄 Попытка подключения к Telegram API ({attempt}/{max_retries})...")
            await bot.delete_webhook(drop_pending_updates=True)
            await setup_bot_commands(bot)

            # Запуск фонового воркера Радара
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

    # 1. Инициализация базы данных SQLite
    init_db()
    logger.info("🗄️ База данных SQLite (positions.db) инициализирована.")

    # 2. Запуск Health-Check сервера Render
    await run_health_check_server()

    # 3. Сессия и бот
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
        logger.info("🛑 Сессия бота корректно закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("👋 Бот остановлен.")