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
_START_TIME = 0.0
_SUPERVISOR_RESTARTS = 0


async def setup_bot_commands(bot: Bot) -> None:
    """Регистрирует полное синхронизированное меню команд Telegram."""
    commands = [
        BotCommand(command="start", description="🚀 Главное меню и кнопки управления"),
        BotCommand(command="digest", description="🔄 Свежий сводный дайджест по городам"),
        BotCommand(command="standby", description="🧭 Почему вне рынка? Разбор ожидания"),
        BotCommand(command="positions", description="📌 Мои открытые сделки и автопилот"),
        BotCommand(command="stats", description="📊 Статистика торговли и винрейт"),
        BotCommand(command="ai", description="🤖 AI-анализ погоды (Gemini)"),
        BotCommand(command="cities", description="🌍 Избранные города (4 модели + METAR)"),
        BotCommand(command="scan", description="🔍 Сканировать маркет (Preddy / Polymarket)"),
        BotCommand(command="wallet", description="👛 Мой кошелек и баланс Polymarket"),
        BotCommand(command="deposit", description="💰 Управление депозитом и банкроллом"),
        BotCommand(command="mode", description="⚡/🧪 Переключить режим (Боевой / Тест)"),
        BotCommand(command="report", description="📄 Скачать аудиторский отчет за день"),
        BotCommand(command="sync", description="🔄 Синхронизировать сделки с Polymarket"),
        BotCommand(command="open", description="➕ Добавить сделку на автопилот"),
        BotCommand(command="status", description="🩺 Статус системы и сканера 24/7"),
        BotCommand(command="help", description="📖 Справка по всем командам и кнопкам"),
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
    Предоставляет расширенный JSON-статус на /status и /diag для удаленного мониторинга.
    """
    async def handle_ping(request: web.Request) -> web.Response:
        return web.Response(text="OK: Weather Alpha Bot v8.0 is running 24/7", status=200)

    async def handle_status(request: web.Request) -> web.Response:
        import time as _t
        from database import get_bot_state, get_all_subscribers, get_all_active_positions
        from auto_scanner import KHV_TZ, get_next_sleep_seconds
        now_khv = datetime.now(KHV_TZ)
        sleep_secs, next_target, _ = get_next_sleep_seconds(now_khv)
        uptime = int(_t.time() - _START_TIME) if _START_TIME > 0 else 0
        try:
            subs = len(get_all_subscribers())
        except Exception:
            subs = 0
        try:
            open_pos = len(get_all_active_positions())
        except Exception:
            open_pos = 0
        diag = {
            "status": "online",
            "uptime_seconds": uptime,
            "uptime_human": f"{uptime // 3600}h {(uptime % 3600) // 60}m {uptime % 60}s",
            "khv_time": now_khv.strftime("%Y-%m-%d %H:%M:%S"),
            "last_digest_slot": get_bot_state("last_sent_digest_slot") or "none",
            "last_eu_report": get_bot_state("last_eu_report_date") or "none",
            "last_us_report": get_bot_state("last_us_report_date") or "none",
            "next_digest_target": next_target.strftime("%H:%M:%S"),
            "sleep_remaining_seconds": round(sleep_secs, 1),
            "subscribers_count": subs,
            "open_positions": open_pos,
            "supervisor_restarts": _SUPERVISOR_RESTARTS,
        }
        return web.json_response(diag)

    app = web.Application()
    for path in ["/", "/healthz", "/health", "/ping", "/cron"]:
        app.router.add_route("*", path, handle_ping)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/diag", handle_status)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"🌐 Health-Check сервер запущен на порту {port}.")


async def render_keepalive_loop() -> None:
    """
    Периодически пингует внешний URL сервиса на Render каждые 3 минуты (180 сек).
    Предотвращает засыпание бесплатного инстанса Render (15-минутный таймаут) даже при одиночном пропуске сети.
    Автоматически использует гарантированный fallback-URL сервиса.
    """
    external_url = (
        os.getenv("RENDER_EXTERNAL_URL")
        or os.getenv("RENDER_URL")
        or (f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}" if os.getenv("RENDER_EXTERNAL_HOSTNAME") else None)
        or "https://weather-bot-3xim.onrender.com"
    )

    health_url = f"{external_url.rstrip('/')}/healthz"
    logger.info(f"🔄 Запущен Keep-Alive пингер для Render: {health_url} (интервал 3 мин).")

    await asyncio.sleep(30)
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    logger.debug(f"🏓 Self-Ping Render Keep-Alive: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"⚠️ Ошибка Self-Ping Keep-Alive: {e}")
        await asyncio.sleep(180)  # 3 минуты


async def auto_scanner_supervisor(bot: Bot) -> None:
    """
    Неубиваемый супервизор фонового сканера.
    Непрерывно следит за состоянием задачи автосканера.
    Если задача завершилась (сбой сети, непредвиденная ошибка, таймаут) —
    немедленно перезапускает её с логированием причины.
    """
    global _SUPERVISOR_RESTARTS
    logger.info("🛡️ Супервизор фонового автосканера запущен.")
    while True:
        try:
            task = asyncio.create_task(run_auto_scanner(bot))
            await task
        except asyncio.CancelledError:
            logger.info("🛑 Супервизор автосканера штатно остановлен.")
            break
        except Exception as e:
            _SUPERVISOR_RESTARTS += 1
            logger.error(
                f"💥 Фоновый автосканер непредвиденно завершил работу: {e}. Перезапуск через 5 сек...",
                exc_info=True,
            )
            await asyncio.sleep(5)


async def start_bot_with_retry(bot: Bot, dp: Dispatcher) -> None:
    """Запускает опрос Telegram API и фоновый сканер с бесконечной защитой от сбоев сети."""
    attempt = 0
    while True:
        attempt += 1
        scanner_task = None
        keepalive_task = None
        try:
            logger.info(f"🔄 Подключение к Telegram API (попытка {attempt})...")
            await bot.delete_webhook(drop_pending_updates=True)
            await setup_bot_commands(bot)

            # Восстановление базы данных и активных сделок из облачного сейфа Telegram
            try:
                from state_sync import restore_state_from_telegram
                restored = await restore_state_from_telegram(bot)
                if restored:
                    logger.info("💾 Состояние успешно восстановлено из облачного сейфа Telegram.")
                else:
                    logger.warning("ℹ️ Сейф Telegram не был восстановлен при старте. Перезапись сейфа отменена для защиты открытых позиций.")
            except Exception as e_safe:
                logger.warning(f"Ошибка инициализации облачного сейфа: {e_safe}")

            # Запуск неубиваемого супервизора автосканера и Keep-Alive пингера Render
            scanner_task = asyncio.create_task(auto_scanner_supervisor(bot))
            keepalive_task = asyncio.create_task(render_keepalive_loop())

            logger.info("🚀 УСПЕШНО! Weather Alpha Engine v8.0 запущен и слушает команды.")
            await dp.start_polling(bot)
            logger.warning("⚠️ dp.start_polling завершился, перезапуск сессии...")
        except Exception as error:
            logger.warning(f"⚠️ Сбой связи с Telegram API (попытка {attempt}): {error}")
        finally:
            if scanner_task:
                scanner_task.cancel()
            if keepalive_task:
                keepalive_task.cancel()

        backoff = min(30.0, 3.0 * (1.3 ** min(attempt, 8)))
        await asyncio.sleep(backoff)


async def main() -> None:
    """Главная функция инициализации компонентов бота."""
    if not config.BOT_TOKEN:
        logger.critical("❌ КРИТИЧЕСКАЯ ОШИБКА: BOT_TOKEN не обнаружен в .env!")
        return

    # 1. Инициализация базы данных SQLite
    global _START_TIME
    import time as _t
    _START_TIME = _t.time()
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