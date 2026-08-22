import asyncio
import logging
import zoneinfo
from datetime import datetime
from typing import Any, Dict

# Настройка логирования для наглядного отображения шагов
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - [%(levelname)s] - %(message)s"
)
logger = logging.getLogger("PreFlightTest")


async def run_preflight_checks() -> None:
    logger.info(
        "🛠️ ЗАПУСК ЛОКАЛЬНОГО PRE-FLIGHT ТЕСТИРОВАНИЯ CORE ENGINE (БЕЗ TELEGRAM API)..."
    )
    print("=" * 70)

    # 1. Проверка Конфигурации и .env
    try:
        from config import BOT_TOKEN

        if BOT_TOKEN:
            logger.info(
                "✅ [1/6] Config: Файл config.py успешно загружен. BOT_TOKEN обнаружен."
            )
        else:
            logger.warning(
                "⚠️ [1/6] Config: BOT_TOKEN пуст или не найден в .env!"
            )
    except Exception as e:
        logger.error(f"❌ [1/6] Config Error: {e}")
        return

    test_icao = "UHHH"
    airport_data = None
    openmeteo_data = None
    noaa_data = None

    # 2. Проверка Airport Resolver
    try:
        from airport_resolver import resolve_airport

        airport_data = await asyncio.to_thread(resolve_airport, test_icao)

        if airport_data:
            logger.info(
                f"✅ [2/6] AirportResolver: Код '{test_icao}' успешно найден!"
            )
            logger.info(
                f"     -> Название: {airport_data.get('name', 'Н/Д')}"
            )
            logger.info(
                f"     -> Координаты: Lat {airport_data.get('lat')}, Lon {airport_data.get('lon')}"
            )
            logger.info(
                f"     -> Таймзона: {airport_data.get('timezone', 'Asia/Vladivostok')}"
            )
        else:
            logger.error(
                f"❌ [2/6] AirportResolver: Аэропорт с кодом '{test_icao}' не найден в базе."
            )
            return
    except Exception as e:
        logger.error(f"❌ [2/6] AirportResolver Error: {e}")
        return

    # Извлечение параметров и расчет даты строго в локальном часовом поясе аэропорта
    lat = airport_data.get("lat", 48.528)
    lon = airport_data.get("lon", 135.188)
    tz_name = airport_data.get("timezone", "Asia/Vladivostok")

    try:
        local_tz = zoneinfo.ZoneInfo(tz_name)
        target_date_local = datetime.now(local_tz).strftime("%Y-%m-%d")
        logger.info(
            f"📅 Расчетная локальная дата для {tz_name}: {target_date_local}"
        )
    except Exception as e:
        logger.error(f"❌ Ошибка вычисления локальной даты: {e}")
        return

    # 3. Проверка Open-Meteo Forecast Service
    try:
        from openmeteo_service import fetch_openmeteo_forecast

        logger.info(
            f"📡 [3/6] OpenMeteo: Запрос прогноза (Lat: {lat}, Lon: {lon}, TZ: {tz_name}, Date: {target_date_local})..."
        )
        openmeteo_data = await asyncio.to_thread(
            fetch_openmeteo_forecast, lat, lon, tz_name, target_date_local
        )

        if openmeteo_data:
            logger.info(
                "✅ [3/6] OpenMeteo: Данные погоды успешно получены!"
            )
        else:
            logger.warning("⚠️ [3/6] OpenMeteo: Вернулся пустой ответ.")
    except Exception as e:
        logger.error(f"❌ [3/6] OpenMeteo Error: {e}")

    # 4. Проверка NOAA Service (METAR/TAF)
    try:
        from noaa_service import get_noaa_package

        logger.info(
            f"📡 [4/6] NOAA Service: Запрос телеграмм METAR/TAF для {test_icao}..."
        )
        noaa_data = await asyncio.to_thread(get_noaa_package, test_icao)

        if noaa_data and isinstance(noaa_data, dict):
            metar_dict = noaa_data.get("metar", {})
            taf_dict = noaa_data.get("taf", {})

            metar_raw = (
                metar_dict.get("raw") if isinstance(metar_dict, dict) else None
            )
            taf_raw = (
                taf_dict.get("raw") if isinstance(taf_dict, dict) else None
            )

            logger.info("✅ [4/6] NOAA Service: Пакет данных получен!")
            if metar_raw:
                logger.info(
                    f"     -> METAR: {metar_raw[:60]}..."
                    if len(metar_raw) > 60
                    else f"     -> METAR: {metar_raw}"
                )
            else:
                logger.info("     -> METAR: Недоступен или пуст")

            if taf_raw:
                logger.info(
                    f"     -> TAF:   {taf_raw[:60]}..."
                    if len(taf_raw) > 60
                    else f"     -> TAF:   {taf_raw}"
                )
            else:
                logger.info("     -> TAF:   Недоступен или пуст")
        else:
            logger.warning("⚠️ [4/6] NOAA Service: Пакет вернулся пустым.")
    except Exception as e:
        logger.error(f"❌ [4/6] NOAA Service Error: {e}")

    # 5. Проверка Weather Synthesizer
    try:
        from weather_synthesizer import synthesize_forecast

        logger.info(
            "⚙️ [5/6] WeatherSynthesizer: Синтез финального отчета из openmeteo_data..."
        )
        synthesis_result = await asyncio.to_thread(
            synthesize_forecast, openmeteo_data
        )

        logger.info(
            "✅ [5/6] WeatherSynthesizer: Финальный отчет успешно сформирован!"
        )
        print("\n" + "-" * 70)
        print("СГЕНЕРИРОВАННЫЙ ТЕКСТ ОТЧЕТА (ВЫВОД ДЛЯ ПОЛЬЗОВАТЕЛЯ):")
        print("-" * 70)

        if isinstance(synthesis_result, dict):
            if synthesis_result.get("success"):
                print(synthesis_result.get("text", synthesis_result))
            else:
                print(
                    f"⚠️ Синтез вернул success=False. Данные: {synthesis_result}"
                )
        else:
            print(synthesis_result)
        print("-" * 70 + "\n")
    except Exception as e:
        logger.error(f"❌ [5/6] WeatherSynthesizer Error: {e}")

    # 6. Проверка Валидности Handlers и Middlewares
    try:
        from handlers import router
        from middlewares import ThrottlingMiddleware

        logger.info(
            "✅ [6/6] Handlers & Middlewares: Модули aiogram3 успешно импортированы и валидны."
        )
    except Exception as e:
        logger.error(f"❌ [6/6] Handlers/Middlewares Error: {e}")

    print("=" * 70)
    logger.info("🎉 ЛОКАЛЬНАЯ ПРОВЕРКА CORE ENGINE ЗАВЕРШЕНА!")


if __name__ == "__main__":
    asyncio.run(run_preflight_checks())
    