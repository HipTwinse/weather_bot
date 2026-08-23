import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiohttp import web

import config
from handlers import router
from middlewares import ThrottlingMiddleware

# Настройка системного логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("WeatherBotMain")


async def run_health_check_server() -> None:
    """Запускает легковесный веб-сервер для удовлетворения проверок портов Render (Health Check)."""
    async def handle_ping(request: web.Request) -> web.Response:
        return web.Response(text="OK: Weather Bot is running 24/7", status=200)

    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/healthz", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()

    # Считываем порт из окружения Render (по умолчанию 10000 или 8080)
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Встроенный Health-Check сервер успешно запущен на порту {port}.")


async def start_bot_with_retry(bot: Bot, dp: Dispatcher, max_retries: int = 5) -> None:
    """Запускает бота с автоповтором при временных сбоях сети."""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"🔄 Попытка подключения к Telegram API ({attempt}/{max_retries})...")
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("🚀 УСПЕШНО! Бот слушает команды. Ожидание погодных запросов...")
            await dp.start_polling(bot)
            break
        except Exception as error:
            logger.warning(f"⚠️ Сбой связи с сервером: {error}")
            if attempt < max_retries:
                logger.info("⏳ Пауза 3 секунды перед повторным подключением...")
                await asyncio.sleep(3.0)
            else:
                logger.error("❌ Превышен лимит попыток подключения к Telegram API.", exc_info=True)


async def main() -> None:
    # 1. Проверка токена
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