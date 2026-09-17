"""
Модуль легкого автоматического сканера метеоданных и динамики рынков (Auto Scanner v7.1).

Регламент работы:
1. Тайм-фильтр: Активен строго с 10:00 до 00:00 по времени Хабаровска (UTC+10).
   В ночные часы переходит в режим сна для экономии API-квот и ресурсов.
2. Цикл обхода: Каждые 30 минут сбор 4 пакетных моделей Open-Meteo и METAR (NOAA)
   по 4 ключевым городам: Лондон (EGLC), Париж (LFPB), Милан (LIMC), Мадрид (LEMD).
3. Оценка динамики (Сценарий Б):
   - Расчет темпа прогрева (°C/час) относительно утренней базы.
   - Контроль остатка окна солнечной инсоляции.
   - Применение региональной физики из knowledge_base.md v7.1.
4. Выдача практического вердикта:
   🟢 CASHOUT (Фиксация профита) / 🟢 ВХОД В ИМПУЛЬС / ⛔ СКИП МАРКЕТА.
5. Рассылка дайджеста администратору (ADMIN_CHAT_ID) и держателям позиций.
"""

import asyncio
from datetime import datetime
import logging
from typing import Dict, Optional, Tuple
import zoneinfo

from aiogram import Bot

import config
from airport_resolver import resolve_airport
from database import get_all_active_positions
from noaa_service import get_noaa_package
from openmeteo_service import fetch_openmeteo_forecast

logger = logging.getLogger("AutoScanner")

KHV_TZ = zoneinfo.ZoneInfo("Asia/Vladivostok")  # Хабаровск UTC+10

TARGET_CITIES = {
    "EGLC": "🇬БУ Лондон (EGLC)",
    "LFPB": "🇫🇷 Париж (LFPB)",
    "LIMC": "🇮🇹 Милан (LIMC)",
    "LEMD": "🇪🇸 Мадрид (LEMD)",
}

# Хранилище утренней базы для расчета темпа прогрева:
# icao -> {"date": "YYYY-MM-DD", "temp": float, "timestamp": float}
_MORNING_BASELINES: Dict[str, dict] = {}


def is_khv_active_hours() -> bool:
    """Проверяет, попадает ли текущее время Хабаровска в окно 10:00 - 00:00."""
    now_khv = datetime.now(KHV_TZ)
    return 10 <= now_khv.hour < 24


def _calculate_dynamics(
    icao: str,
    current_temp: Optional[float],
    local_dt: datetime,
    current_ts: float,
) -> Tuple[str, str, str]:
    """
    Рассчитывает темп прогрева (°C/час), остаток инсоляции и динамический статус.
    """
    if current_temp is None:
        return "Н/Д", "Н/Д", "Нет данных METAR"

    today_str = local_dt.strftime("%Y-%m-%d")
    baseline = _MORNING_BASELINES.get(icao)

    # Инициализация или сброс базы на новый день
    if not baseline or baseline.get("date") != today_str:
        _MORNING_BASELINES[icao] = {
            "date": today_str,
            "temp": current_temp,
            "timestamp": current_ts,
        }
        baseline = _MORNING_BASELINES[icao]

    time_diff_hours = (current_ts - baseline["timestamp"]) / 3600.0
    temp_diff = current_temp - baseline["temp"]

    if time_diff_hours >= 0.5:
        rate_per_hour = round(temp_diff / time_diff_hours, 2)
        rate_str = f"{'+' if rate_per_hour >= 0 else ''}{rate_per_hour:.1f}°C/ч"
    else:
        rate_str = "База зафиксирована"

    # Расчет остатка инсоляции (в Европе пик прогрева обычно наступает до 16:30 - 17:00 LT)
    local_hour = local_dt.hour + local_dt.minute / 60.0
    sunset_window_close = 17.0
    rem_hours = max(0.0, sunset_window_close - local_hour)
    rem_hours_str = f"{rem_hours:.1f} ч" if rem_hours > 0 else "Окно закрыто"

    status_str = f"База: {baseline['temp']}°C | Фактический темп: {rate_str}"
    return rate_str, rem_hours_str, status_str


def _determine_scenario_b_verdict(
    icao: str,
    temp_c: Optional[float],
    local_dt: datetime,
    rate_str: str,
    rem_hours_str: str,
    raw_metar: str,
    models_max: Dict[str, float],
) -> Tuple[str, str]:
    """
    Определяет вердикт Сценария Б на основе правил knowledge_base.md v7.1.
    """
    if temp_c is None:
        return "⛔ СКИП", "Отсутствуют фактические данные температуры."

    local_hour = local_dt.hour
    is_rain = any(sig in raw_metar for sig in ["RA", "DZ", "SN", "TS"])
    is_overcast = "OVC" in raw_metar

    # 1. Затяжные осадки или сплошная пелена — прогрев заблокирован
    if is_rain and is_overcast:
        return (
            "⛔ СКИП",
            "Обложной дождь и сплошная облачность OVC глушат солнечную радиацию.",
        )

    # 2. Окно дневного прогрева закрыто (после 16:30 - 17:00 местного времени)
    if local_hour >= 17:
        return (
            "🟢 CASHOUT (Фиксация профита)",
            f"Окно дневной инсоляции закрыто ({local_hour}:00 LT). Пик зафиксирован на {temp_c}°C.",
        )

    # 3. Региональная синоптическая логика KB v7.1
    gfs_peak = models_max.get("gfs_global")
    ecmwf_peak = models_max.get("ecmwf_hres")
    icon_peak = models_max.get("icon_global")

    if icao == "EGLC":
        # Лондон: UHI при SW/W ветре
        if "2" in raw_metar[:10] and ("KT" in raw_metar):  # SW сектор
            physics_note = "SW-ветер несет городской остров тепла Доклендса (+1.0°C к моделям)."
        else:
            physics_note = "Лондон: ориентир на модель GFS при окнах солнца."
    elif icao == "LFPB":
        physics_note = "Париж: GFS в приоритете для сухого радиационного прогрева."
    elif icao == "LIMC":
        physics_note = "Милан: термический купол долины По (приоритет ICON/GEM)."
    elif icao == "LEMD":
        physics_note = "Мадрид: плато Месета, опора на медиану ECMWF и GFS."
    else:
        physics_note = "Синтез численных моделей."

    # Если до расчетного пика осталось менее 0.5°C или темп замедлился
    highest_model_target = max(models_max.values()) if models_max else temp_c
    if highest_model_target - temp_c <= 0.4 and local_hour >= 14:
        return (
            "🟢 CASHOUT (Фиксация профита)",
            f"Температура подошла к расчетному пику ({temp_c}°C / цель {highest_model_target}°C). {physics_note}",
        )

    # Если солнце в зените, темп высокий и запас есть
    if local_hour < 15 and ("+" in rate_str or rate_str == "База зафиксирована"):
        return (
            "🟢 ВХОД В ИМПУЛЬС",
            f"Активная фаза прогрева. Остаток инсоляции: {rem_hours_str}. {physics_note}",
        )

    return (
        "🟢 CASHOUT (Фиксация профита)",
        f"Темп роста стабилизировался. {physics_note}",
    )


async def scan_single_city(bot: Bot, icao: str, admin_id: Optional[int]) -> None:
    """Выполняет легкий цикл проверки одного города."""
    airport_data = resolve_airport(icao)
    if not airport_data:
        return

    lat = airport_data["lat"]
    lon = airport_data["lon"]
    tz_name = airport_data.get("timezone", "UTC")
    city_label = TARGET_CITIES.get(icao, icao)

    try:
        local_tz = zoneinfo.ZoneInfo(tz_name)
        local_dt = datetime.now(local_tz)
    except Exception:
        local_tz = zoneinfo.ZoneInfo("UTC")
        local_dt = datetime.now(local_tz)

    target_date_local = local_dt.strftime("%Y-%m-%d")
    current_ts = asyncio.get_event_loop().time()

    # Запрашиваем кэшированные пакетные модели и свежий METAR
    forecast_task = asyncio.to_thread(fetch_openmeteo_forecast, lat, lon, tz_name, target_date_local)
    noaa_task = asyncio.to_thread(get_noaa_package, icao)

    forecast_data, noaa_data = await asyncio.gather(forecast_task, noaa_task, return_exceptions=True)

    if isinstance(noaa_data, Exception) or not isinstance(noaa_data, dict):
        noaa_data = {}
    if isinstance(forecast_data, Exception) or not isinstance(forecast_data, dict):
        forecast_data = {}

    metar = noaa_data.get("metar", {})
    temp_c = metar.get("temp_c")
    raw_metar = metar.get("raw", "")

    # Считываем суточные пики моделей
    models_max: Dict[str, float] = {}
    for m_key, m_val in forecast_data.get("primary_models", {}).items():
        if m_val.get("status", {}).get("available"):
            t_max = (m_val.get("derived_metrics") or {}).get("max_temp_c")
            if t_max is not None:
                models_max[m_key] = float(t_max)

    for m_key, m_val in forecast_data.get("secondary_models", {}).items():
        if m_val.get("status", {}).get("available"):
            t_max = (m_val.get("derived_metrics") or {}).get("max_temp_c")
            if t_max is not None:
                models_max[m_key] = float(t_max)

    rate_str, rem_hours_str, status_str = _calculate_dynamics(icao, temp_c, local_dt, current_ts)
    verdict, physics_explanation = _determine_scenario_b_verdict(
        icao, temp_c, local_dt, rate_str, rem_hours_str, raw_metar, models_max
    )

    # Формируем компактный Telegram-дайджест
    ecmwf_val = f"{models_max.get('ecmwf_hres', 'Н/Д')}°C"
    gfs_val = f"{models_max.get('gfs_global', 'Н/Д')}°C"
    icon_val = f"{models_max.get('icon_global', 'Н/Д')}°C"
    gem_val = f"{models_max.get('gem_global', 'Н/Д')}°C"

    digest_text = (
        f"🔄 <b>ВНУТРИДНЕВНОЙ АПДЕЙТ: ДИНАМИКА ПРОГРЕВА</b>\n\n"
        f"📍 <b>Локация:</b> {city_label}\n"
        f"🕒 <b>Время:</b> <code>{local_dt.strftime('%H:%M')} LT</code> | Инсоляция: <b>{rem_hours_str}</b>\n"
        f"🌡️ <b>Факт (METAR):</b> <code>{temp_c if temp_c is not None else 'Н/Д'}°C</code> ({status_str})\n"
        f"📊 <b>Модели (T_max):</b> ECMWF: {ecmwf_val} | GFS: {gfs_val} | ICON: {icon_val} | GEM: {gem_val}\n"
        f"🔬 <b>Физика:</b> {physics_explanation}\n\n"
        f"🎯 <b>ВЕРДИКТ:</b> <b>{verdict}</b>"
    )

    # Отправка администратору
    if admin_id:
        try:
            await bot.send_message(chat_id=admin_id, text=digest_text, parse_mode="HTML")
        except Exception as err:
            logger.warning(f"Не удалось отправить дайджест админу {admin_id}: {err}")

    # Отправка пользователям, держащим открытую позицию по этому аэропорту
    active_positions = await asyncio.to_thread(get_all_active_positions)
    notified_users = set()
    for pos in active_positions:
        if pos.get("icao") == icao:
            uid = pos.get("user_id")
            if uid and uid not in notified_users and uid != admin_id:
                notified_users.add(uid)
                try:
                    await bot.send_message(chat_id=uid, text=digest_text, parse_mode="HTML")
                except Exception:
                    pass


async def run_auto_scanner(bot: Bot) -> None:
    """
    Основной цикл легковесного фонового сканера.
    Работает строго с 10:00 до 00:00 по времени Хабаровска (UTC+10),
    опрашивая города каждые 30 минут.
    """
    logger.info("🚀 Автосканер Weather Alpha Engine v7.1 запущен (Тайм-фильтр: 10:00–00:00 ХБР).")
    admin_id = getattr(config, "ADMIN_CHAT_ID", None)

    while True:
        try:
            if not is_khv_active_hours():
                now_khv_str = datetime.now(KHV_TZ).strftime("%H:%M")
                logger.info(f"🌙 Ночное окно Хабаровска ({now_khv_str} ХБР). Автосканер спит для экономии квот.")
                # В неактивное время спим 15 минут до следующей проверки окна
                await asyncio.sleep(900)
                continue

            logger.info("📡 Запуск 30-минутного цикла обхода ключевых городов...")
            for icao in TARGET_CITIES.keys():
                try:
                    await scan_single_city(bot, icao, admin_id)
                except Exception as city_err:
                    logger.error(f"Ошибка при сканировании города {icao}: {city_err}")
                await asyncio.sleep(2.0)  # Безопасная пауза между городами

            # Ожидание 30 минут до следующего цикла
            await asyncio.sleep(1800)

        except asyncio.CancelledError:
            logger.info("🛑 Автосканер остановлен.")
            break
        except Exception as loop_err:
            logger.error(f"⚠️ Сбой в основном цикле автосканера: {loop_err}", exc_info=True)
            await asyncio.sleep(60)
