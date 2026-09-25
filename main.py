"""
Точка входа Telegram-бота Weather Alpha Engine v7.1.

Архитектура:
1. Запуск легковесного HTTP Health-Check сервера на порту 10000 (эндпоинты / и /healthz)
   для предотвращения засыпания бесплатного инстанса на Render.
2. Инициализация базы данных SQLite (positions.db).
3. Интерактивный Telegram-интерфейс (aiogram 3.x) с поддержкой команд /start, /scan, /positions, /cities, /help.
4. Запуск оптимизированного легковесного фонового автосканера (auto_scanner.py).
5. Память рекордов суточного максимума (daily_max_records).
"""

import asyncio
import logging
import os
import sys
from typing import Dict

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

import config
from auto_scanner import run_auto_scanner
from database import init_db
from handlers import router
from middlewares import ThrottlingMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("WeatherBotMain")

# Память суточных рекордов температурного максимума: ключ 'ICAO_YYYY-MM-DD' -> max_temp_float
daily_max_records: Dict[str, float] = {}


async def setup_bot_commands(bot: Bot) -> None:
    """Регистрирует интерактивное меню команд Telegram."""
    commands = [
        BotCommand(command="start", description="🚀 Главное меню и статус бота"),
        BotCommand(command="scan", description="🔍 Сканировать маркет (Preddy / Polymarket)"),
        BotCommand(command="ai", description="🤖 AI-анализ погоды (Gemini v7.4)"),
        BotCommand(command="positions", description="📌 Мои открытые сделки"),
        BotCommand(command="cities", description="🌍 Быстрый выбор избранных городов"),
        BotCommand(command="help", description="📖 Справка и регламент v7.1"),
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("📋 Интерактивное меню команд зарегистрировано.")
    except Exception as error:
        logger.warning(f"⚠️ Ошибка регистрации команд: {error}")


async def run_health_check_server() -> None:
    """
    Запускает HTTP Health-Check сервер на порту 10000.
    Критически важен для удержания бесплатного контейнера Render от засыпания.
    """
    async def handle_ping(request: web.Request) -> web.Response:
        return web.Response(text="OK: Weather Alpha Bot v8.0 is running 24/7", status=200)

    app = web.Application()
    for path in ["/", "/healthz", "/health", "/ping", "/cron", "/status"]:
        app.router.add_route("*", path, handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Health-Check сервер запущен на порту {port}.")


async def render_keepalive_loop() -> None:
    """
    Периодически пингует внешний URL сервиса на Render каждые 9 минут (540 сек).
    Предотвращает засыпание бесплатного инстанса Render (15-минутный таймаут).
    """
    external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("RENDER_URL")
    if not external_url:
        logger.info("ℹ️ RENDER_EXTERNAL_URL не задан (Keep-Alive активен только на Render).")
        return

    health_url = f"{external_url.rstrip('/')}/healthz"
    logger.info(f"🔄 Запущен Keep-Alive пингер для Render: {health_url} (интервал 9 мин).")

    while True:
        await asyncio.sleep(540)  # 9 минут
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(health_url, timeout=15) as resp:
                    logger.debug(f"🏓 Self-Ping Render Keep-Alive: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка Self-Ping Keep-Alive: {e}")


async def start_bot_with_retry(bot: Bot, dp: Dispatcher, max_retries: int = 5) -> None:
    """Запускает опрос Telegram API и фоновый сканер с защитой от разрыва сети."""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"🔄 Подключение к Telegram API ({attempt}/{max_retries})...")
            await bot.delete_webhook(drop_pending_updates=True)
            await setup_bot_commands(bot)

            # Запуск автосканера и Keep-Alive пингера Render
            scanner_task = asyncio.create_task(run_auto_scanner(bot))
            keepalive_task = asyncio.create_task(render_keepalive_loop())

            logger.info("🚀 УСПЕШНО! Weather Alpha Engine v8.0 запущен и слушает команды.")
            await dp.start_polling(bot)

            scanner_task.cancel()
            keepalive_task.cancel()
            break
        except Exception as error:
            logger.warning(f"⚠️ Сбой связи с Telegram API: {error}")
            if attempt < max_retries:
                await asyncio.sleep(3.0)
            else:
                logger.error("❌ Превышен лимит попыток подключения к Telegram.", exc_info=True)


async def main() -> None:
    """Главная функция инициализации компонентов бота."""
    if not config.BOT_TOKEN:
        logger.critical("❌ КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не обнаружен в .env!")
        return

    # 1. Инициализация базы данных SQLite
    init_db()
    logger.info("🗄️ База данных SQLite (positions.db) инициализирована.")

    # 2. Запуск HTTP Health-Check сервера (Render)
    await run_health_check_server()

    # 3. Настройка сетевой сессии и бота
    session = AiohttpSession(proxy=config.PROXY_URL) if config.PROXY_URL else None
    bot = Bot(
        token=config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # 4. Регистрация Rate Limiter и маршрутов
    dp.message.middleware(ThrottlingMiddleware())
    dp.include_router(router)

    try:
        await start_bot_with_retry(bot, dp)
    finally:
        await bot.session.close()
        logger.info("🛑 Сессия Telegram-бота закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("👋 Бот штатно остановлен.")