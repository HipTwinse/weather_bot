import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from config import BOT_TOKEN
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
    """Запускает бота с автоповтором при временных сбоях прокси-туннеля."""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info(f"🔄 Попытка подключения к Telegram API ({attempt}/{max_retries})...")
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("🚀 УСПЕШНО! Бот слушает команды. Ожидание ICAO-запросов...")
            await dp.start_polling(bot)
            break
        except Exception as error:
            logger.warning(f"⚠️ Сбой связи через туннель: {error}")
            if attempt < max_retries:
                logger.info("⏳ Пауза 3 секунды перед повторным подключением...")
                await asyncio.sleep(3.0)
            else:
                logger.error("❌ Превышен лимит попыток подключения к Telegram API.", exc_info=True)


async def main() -> None:
    # 1. Проверка токена
    if not BOT_TOKEN:
        logger.critical("❌ КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не обнаружен в .env!")
        return

    # 2. Инициализация SOCKS5-сессии
    proxy_url = "socks5://127.0.0.1:10808"
    session = AiohttpSession(proxy=proxy_url)
    logger.info(f"🌐 Сетевая сессия инициализирована через: {proxy_url}")

    # 3. Инициализация Bot и Dispatcher
    bot = Bot(
        token=BOT_TOKEN,
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
        logger.info("👋 Бот остановлен пользователем.")