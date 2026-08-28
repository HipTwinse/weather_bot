import asyncio
from datetime import datetime
import logging
import os
import sys
from typing import Dict

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web

import config
from handlers import (
    ALL_RADAR_CITIES,
    active_radar_cities,
    router,
)
from middlewares import ThrottlingMiddleware
from noaa_service import get_noaa_package

# Настройка системного логирования в консоль
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("WeatherBotMain")

# Кэш для защиты от повторного спама уведомлениями (ICAO -> время последней отправки)
last_alert_sent: Dict[str, float] = {}


async def setup_bot_commands(bot: Bot) -> None:
    """Регистрирует интерактивное всплывающее меню команд при вводе '/' в Telegram."""
    commands = [
        BotCommand(command="start", description="🚀 Главное меню и статус бота"),
        BotCommand(command="cities", description="🌍 Быстрый выбор избранных городов"),
        BotCommand(command="scan", description="🔍 Сканировать маркет (Preddy / Polymarket)"),
        BotCommand(command="radar", description="⚙️ Панель радара аномалий"),
        BotCommand(command="help", description="📖 Справка по ICAO и маркетам"),
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("📋 Интерактивное меню команд успешно зарегистрировано в Telegram.")
    except Exception as error:
        logger.warning(f"⚠️ Не удалось зарегистрировать меню команд: {error}")


async def run_health_check_server() -> None:
    """Запускает легковесный веб-сервер для прохождения проверок портов Render (Health Check)."""
    async def handle_ping(request: web.Request) -> web.Response:
        return web.Response(text="OK: Weather Bot is running 24/7", status=200)

    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    # Считываем порт из системного окружения Render (по умолчанию 10000)
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Встроенный Health-Check сервер успешно запущен на порту {port}.")


async def background_radar_worker(bot: Bot) -> None:
    """
    Фоновый воркер: периодически проверяет только включенные города в active_radar_cities
    и уведомляет администратора при фиксации опережения прогрева (Delta-Offset) или чистого неба.
    """
    logger.info("📡 Фоновый Радар Аномалий запущен в отдельном асинхронном потоке.")
    admin_id = getattr(config, "ADMIN_CHAT_ID", None)

    while True:
        try:
            # Опрос происходит каждые 15 минут (900 секунд)
            await asyncio.sleep(900)

            if not active_radar_cities:
                continue

            current_timestamp = asyncio.get_event_loop().time()
            logger.info(f"🔍 [Радар] Проверка активных городов: {list(active_radar_cities)}")

            for icao in list(active_radar_cities):
                # Проверка кулдауна: не чаще 1 уведомления в 2 часа (7200 сек) на один город
                last_time = last_alert_sent.get(icao, 0)
                if current_timestamp - last_time < 7200:
                    continue

                noaa_data = await asyncio.to_thread(get_noaa_package, icao)
                metar = noaa_data.get("metar", {})

                if not metar.get("available"):
                    continue

                temp_c = metar.get("temp_c")
                raw_metar = metar.get("raw", "")
                city_name = ALL_RADAR_CITIES.get(icao, icao)

                # Детектируем потенциальное окно прогрева: чистое небо (CAVOK/NCD) и наличие температуры
                if temp_c is not None and ("CAVOK" in raw_metar or "NCD" in raw_metar or "CLR" in raw_metar):
                    last_alert_sent[icao] = current_timestamp
                    alert_text = (
                        f"🚨 <b>РАДАР АНОМАЛИЙ | АКТИВНЫЙ СИГНАЛ</b>\n\n"
                        f"📍 <b>Локация:</b> {city_name}\n"
                        f"🌡️ <b>Текущий факт METAR:</b> <code>{temp_c}°C</code>\n"
                        f"☀️ <b>Условия:</b> Чистое небо (CAVOK), активное окно солнечной инсоляции.\n\n"
                        f"📝 <code>{raw_metar}</code>\n\n"
                        f"💡 <i>Проверь открытые маркеты Preddy/Polymarket по этому городу через /scan!</i>"
                    )

                    if admin_id:
                        try:
                            await bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
                            logger.info(f"📢 Уведомление по радару для {icao} успешно отправлено администратору.")
                        except Exception as send_err:
                            logger.error(f"Не удалось отправить уведомление радара для {admin_id}: {send_err}")

        except asyncio.CancelledError:
            logger.info("📡 Фоновый воркер радара остановлен.")
            break
        except Exception as err:
            logger.error(f"⚠️ Ошибка в цикле фонового радара: {err}", exc_info=True)
            await asyncio.sleep(60)


async def start_bot_with_retry(bot: Bot, dp: Dispatcher, max_retries: int = 5) -> None:
    """Запускает бота с автоповтором при временных сбоях сети."""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"🔄 Попытка подключения к Telegram API ({attempt}/{max_retries})...")
            await bot.delete_webhook(drop_pending_updates=True)
            
            # Регистрация меню команд перед стартом поллинга
            await setup_bot_commands(bot)

            # Запуск фонового воркера Радара
            radar_task = asyncio.create_task(background_radar_worker(bot))

            logger.info("🚀 УСПЕШНО! Бот слушает команды. Ожидание погодных запросов...")
            await dp.start_polling(bot)
            
            radar_task.cancel()
            break
        except Exception as error:
            logger.warning(f"⚠️ Сбой связи с сервером: {error}")
            if attempt < max_retries:
                logger.info("⏳ Пауза 3 секунды перед повторным подключением...")
                await asyncio.sleep(3.0)
            else:
                logger.error("❌ Превышен лимит попыток подключения к Telegram API.", exc_info=True)


async def main() -> None:
    # 1. Проверка наличия токена
    if not config.BOT_TOKEN:
        logger.critical("❌ КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не обнаружен!")
        return

    # 2. Запуск фонового веб-сервера для Render
    await run_health_check_server()

    # 3. Инициализация сетевой сессии
    if config.PROXY_URL:
        session = AiohttpSession(proxy=config.PROXY_URL)
        logger.info(f"🌐 Сетевая сессия инициализирована через прокси: {config.PROXY_URL}")
    else:
        session = None
        logger.info("🌐 Прямое сетевое подключение к Telegram API (Production Cloud).")

    # 4. Инициализация Bot и Dispatcher
    bot = Bot(
        token=config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # 5. Подключение Middleware и Router
    dp.message.middleware(ThrottlingMiddleware())
    logger.info("🛡️ ThrottlingMiddleware (Rate Limiting) успешно подключен.")

    dp.include_router(router)
    logger.info("🔀 Основной Router из handlers.py зарегистрирован.")

    # 6. Запуск бота
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