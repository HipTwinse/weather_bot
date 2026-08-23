import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

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

    # 2. Инициализация сессии (с прокси для ПК или напрямую для Render)
    if config.PROXY_URL:
        session = AiohttpSession(proxy=config.PROXY_URL)
        logger.info(f"🌐 Сетевая сессия инициализирована через прокси: {config.PROXY_URL}")
    else:
        session = None
        logger.info("🌐 Прямое сетевое подключение к Telegram API (Production Cloud).")

    # 3. Инициализация Bot и Dispatcher
    bot = Bot(
        token=config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # 4. Подключение Middleware и Router
    dp.message.middleware(ThrottlingMiddleware())
    logger.info("🛡️ ThrottlingMiddleware (Rate Limiting) успешно подключен.")

    dp.include_router(router)
    logger.info("🔀 Основной Router из handlers.py зарегистрирован.")

    # 5. Запуск с защитой от сбоев
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