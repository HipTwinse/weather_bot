"""
Модуль единого автоматического сканера метеоданных и динамики рынков (Auto Scanner v7.1).

Регламент работы:
1. Синхронизация с METAR (строго 2 раза в час):
   Европейские станции выпускают сводки в :00 и :30 (в NOAA поступают к :02 и :32).
   Дайджест отправляется строго в :02 и :32 минуты каждого часа (ХБР / UTC).
   Никакого спама при перезапусках: сон точно рассчитывается до ближайшей точки :02 или :32.
2. Тайм-фильтр:
   Активен строго с 10:00 до 00:00 по времени Хабаровска (UTC+10).
   В ночные часы (00:00–10:00 ХБР) сканер спит до 10:02.
3. Устранение ложного СКИПа из-за ночного выхолаживания:
   - Если местное время станции LT < 09:00: расчет темпа и триггер физического слома
     НЕ переводят рынок в СКИП. Фиксируется только утренняя база (Morning Floor),
     а статус рынка стабилен: 🟡 ПОТЕНЦИАЛ ВХОДА (Ожидание старта инсоляции).
   - Триггер физического слома прогрева (темп < +0.4°C/ч при BKN/OVC) активируется
     СТРОГО в окне активной дневной инсоляции: с 10:30 LT до 15:00 LT.
4. Единый дайджест:
   Все 4 города (Лондон, Париж, Милан, Мадрид) объединяются в ОДИН пост.
   - Первое сообщение дня (10:02 ХБР): «🌅 НОВЫЙ ТОРГОВЫЙ ДЕНЬ | БАЗОВЫЙ ПРОГНОЗ ([ДАТА])».
   - Последующие (каждые :02 и :32): «🔄 ОБНОВЛЕНИЕ НА HH:MM ХБР | ДИНАМИКА».
5. Персонализация позиций (positions.db):
   При наличии открытой сделки блок города дополняется PnL, триггерами Тейк-Профита (>= +35% / >= 60¢),
   Тайм-Стопа (13:30 LT) и дневного физического слома. Без сделки слово «CASHOUT» не используется.
"""

import asyncio
from datetime import datetime, timedelta
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import zoneinfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from airport_resolver import resolve_airport
from database import get_all_active_positions
from noaa_service import get_noaa_package
from openmeteo_service import fetch_openmeteo_forecast
from polymarket_service import find_city_weather_event, parse_markets_orderbook, get_current_outcome_price, extract_temp_value

logger = logging.getLogger("AutoScanner")

KHV_TZ = zoneinfo.ZoneInfo("Asia/Vladivostok")  # Хабаровск UTC+10

TARGET_CITIES = {
    "EGLC": "🇬🇧 Лондон (EGLC)",
    "LFPB": "🇫🇷 Париж (LFPB)",
    "LIMC": "🇮🇹 Милан (LIMC)",
    "LEMD": "🇪🇸 Мадрид (LEMD)",
}

# Хранилище утренней базы прогрева: icao -> {"date": "YYYY-MM-DD", "temp": float, "timestamp": float}
_MORNING_BASELINES: Dict[str, dict] = {}
_LAST_MORNING_DIGEST_DATE: Optional[str] = None
_LAST_SENT_SLOT: Optional[str] = None

# Инлайн-кнопки быстрого анализа под сводным сообщением
express_scan_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇬🇧 Лондон EGLC", callback_data="express_scan:EGLC"),
            InlineKeyboardButton(text="🇫🇷 Париж LFPB", callback_data="express_scan:LFPB"),
        ],
        [
            InlineKeyboardButton(text="🇮🇹 Милан LIMC", callback_data="express_scan:LIMC"),
            InlineKeyboardButton(text="🇪🇸 Мадрид LEMD", callback_data="express_scan:LEMD"),
        ],
    ]
)


def is_khv_active_hours() -> bool:
    """Проверяет, попадает ли время Хабаровска в торговое окно 10:00 - 00:00."""
    now_khv = datetime.now(KHV_TZ)
    return 10 <= now_khv.hour < 24


def get_next_sleep_seconds(now: datetime) -> Tuple[float, datetime, str]:
    """
    Рассчитывает целевое время следующей контрольной точки (:02 или :32)
    и точное количество секунд сна до неё.
    С буфером в 2 секунды (:02:02 / :32:02) для гарантированного попадания в NOAA METAR.
    """
    if now.minute < 2 or (now.minute == 2 and now.second < 2):
        target = now.replace(minute=2, second=2, microsecond=0)
    elif now.minute < 32 or (now.minute == 32 and now.second < 2):
        target = now.replace(minute=32, second=2, microsecond=0)
    else:
        next_hour = now + timedelta(hours=1)
        target = next_hour.replace(minute=2, second=2, microsecond=0)

    sleep_secs = (target - now).total_seconds()
    if sleep_secs <= 0.1:
        if now.minute < 32:
            target = now.replace(minute=32, second=2, microsecond=0)
        else:
            next_hour = now + timedelta(hours=1)
            target = next_hour.replace(minute=2, second=2, microsecond=0)
        sleep_secs = max(1.0, (target - now).total_seconds())

    slot_key = target.strftime("%Y-%m-%d %H:%M")
    return sleep_secs, target, slot_key


def _calculate_dynamics(
    icao: str,
    current_temp: Optional[float],
    local_dt: datetime,
    current_ts: float,
) -> Tuple[str, str, float]:
    """
    Рассчитывает темп прогрева (°C/час), остаток инсоляции и числовой темп.
    До 09:00 местного времени фиксируется только утренний пол (выхолаживание),
    а числовой темп блокируется от ложных отрицательных скачков.
    """
    if current_temp is None:
        return "Н/Д", "Н/Д", 0.0

    today_str = local_dt.strftime("%Y-%m-%d")
    baseline = _MORNING_BASELINES.get(icao)

    if not baseline or baseline.get("date") != today_str:
        _MORNING_BASELINES[icao] = {
            "date": today_str,
            "temp": current_temp,
            "timestamp": current_ts,
        }
        baseline = _MORNING_BASELINES[icao]
    elif local_dt.hour < 9:
        # До 09:00 LT идет предрассветное выхолаживание: фиксируем минимальный утренний пол
        if current_temp < baseline["temp"]:
            baseline["temp"] = current_temp
            baseline["timestamp"] = current_ts

    # До 09:00 LT солнце еще не прогревает станцию, темп не считается за слом
    if local_dt.hour < 9:
        rate_val = 0.0
        rate_str = "Утренний пол (выхолаживание)"
    else:
        time_diff_hours = (current_ts - baseline["timestamp"]) / 3600.0
        temp_diff = current_temp - baseline["temp"]

        if time_diff_hours >= 0.4:
            rate_val = round(temp_diff / time_diff_hours, 2)
            rate_str = f"{'+' if rate_val >= 0 else ''}{rate_val:.1f}°C/ч"
        else:
            rate_val = 0.0
            rate_str = "База зафиксирована"

    local_hour = local_dt.hour + local_dt.minute / 60.0
    sunset_close = 17.0
    rem_hours = max(0.0, sunset_close - local_hour)
    rem_hours_str = f"{rem_hours:.1f} ч" if rem_hours > 0 else "Окно закрыто"

    return rate_str, rem_hours_str, rate_val


def _get_city_physics_note(icao: str, raw_metar: str, temp_c: Optional[float], local_dt: datetime) -> str:
    """Возвращает актуальную синоптическую заметку на базе квант-законов KB v8.0."""
    wind_match = re.search(r"\b(\d{3})(\d{2,3})(?:G\d{2,3})?KT\b", raw_metar)
    wdir = int(wind_match.group(1)) if wind_match else None
    wspd = int(wind_match.group(2)) if wind_match else None

    has_low_cloud = any(c in raw_metar for c in ["OVC0", "BKN0", "OVC01", "BKN01", "OVC02", "BKN02", "OVC03", "BKN03"])
    has_cirrus = any(c in raw_metar for c in ["CI", "CS", "FEW2", "SCT2", "FEW3", "SCT3", "NCD", "CAVOK", "CLR", "SKC"])
    has_rain = any(r in raw_metar for r in ["RA", "DZ", "TS", "SN"])

    if has_rain:
        return "Осадки (дождь/морось): скрытое тепло испарения блокирует подъем температуры."

    if icao == "EGLC":
        if wdir is not None and 50 <= wdir <= 120:
            return "Восточный ветер (050°-120°): холодный воздух с эстуария Темзы гасит UHI. ВЕТО на GFS! Рулит ICON (MAE 0.34°C)."
        elif wdir is not None and 190 <= wdir <= 280:
            if wspd and wspd >= 20:
                return "Шквалистый SW-ветер (>=20 kt): тепловой шлейф Лондона пробивает полосу транзитом (+1.2°C)."
            return "SW-ветер несет городской остров тепла центра Лондона (UHI активен, опора на ICON+GFS)."
        return "Лондон: стабильный радиационный прогрев при прозрачной атмосфере (лидер ICON)."

    elif icao == "LFPB":
        calm_str = " (Штиль <=4 kt: ламинарный перегрев +0.6°C)" if (wspd and wspd <= 4) else ""
        if has_cirrus and not has_low_cloud:
            return f"Париж: перистые облака Cirrus не блокируют солнечную радиацию (пропускание >85%). Лидер ICON.{calm_str}"
        return f"Париж: приоритет ICON (MAE 0.40°C). Холодный дефект ECMWF (-1.04°C) игнорируется.{calm_str}"

    elif icao == "LIMC":
        calm_str = " (Штиль: ламинарный слой долины По +0.4°C)" if (wspd and wspd <= 4) else ""
        if has_cirrus and not has_low_cloud:
            return f"Милан: перистые облака удерживают тепло купола По. Опора строго на ICON (MAE 0.46°C).{calm_str}"
        return f"Милан: термический купол долины реки По. Абсолютный лидер ICON (ECMWF занижает на 1.0°C).{calm_str}"

    elif icao == "LEMD":
        return "Мадрид: сухое плато Месета (>600 м). СТРОГОЕ ВЕТО на GFS (-1.01°C занижение!). Опора на ICON+0.4°C."

    elif icao == "EDDM":
        return "Мюнхен: при южном ветре (150°-210°) работает Альпийский фён (+1.2°C...+2.0°C к консенсусу)."

    return "Синтез эмпирических физических законов KB v8.0."


async def collect_city_metrics(icao: str) -> Dict[str, Any]:
    """Собирает полный набор метеометрик и моделей по одному городу."""
    airport = resolve_airport(icao)
    if not airport:
        return {}

    lat, lon = airport["lat"], airport["lon"]
    tz_name = airport.get("timezone", "UTC")
    try:
        local_tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        local_tz = zoneinfo.ZoneInfo("UTC")

    local_dt = datetime.now(local_tz)
    target_date_local = local_dt.strftime("%Y-%m-%d")
    current_ts = asyncio.get_event_loop().time()

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

    models_max: Dict[str, float] = {}
    for m_key, m_val in {**forecast_data.get("primary_models", {}), **forecast_data.get("secondary_models", {})}.items():
        if m_val.get("status", {}).get("available"):
            t_max = (m_val.get("derived_metrics") or {}).get("max_temp_c")
            if t_max is not None:
                models_max[m_key] = float(t_max)

    rate_str, rem_hours_str, rate_val = _calculate_dynamics(icao, temp_c, local_dt, current_ts)
    physics_note = _get_city_physics_note(icao, raw_metar, temp_c, local_dt)

    # Расчет целевого пика (T_max)
    peaks = list(models_max.values())
    if peaks:
        avg_peak = round(sum(peaks) / len(peaks), 1)
        peak_str = f"{avg_peak}°C ({min(peaks):.1f}–{max(peaks):.1f}°C)"
    else:
        avg_peak = temp_c or 20.0
        peak_str = f"{avg_peak}°C"

    # Получение текущего стакана Polymarket для сопоставления цен
    orderbook = []
    try:
        poly_event = await find_city_weather_event(icao, target_date_local)
        if poly_event:
            orderbook = parse_markets_orderbook(poly_event.get("markets", []))
    except Exception:
        orderbook = []

    return {
        "icao": icao,
        "city_name": TARGET_CITIES.get(icao, icao),
        "local_dt": local_dt,
        "temp_c": temp_c,
        "raw_metar": raw_metar,
        "models_max": models_max,
        "rate_str": rate_str,
        "rate_val": rate_val,
        "rem_hours_str": rem_hours_str,
        "physics_note": physics_note,
        "peak_str": peak_str,
        "avg_peak": avg_peak,
        "orderbook": orderbook,
    }


def get_priority_target(icao: str, models: Dict[str, float], avg_peak: float, raw_metar: str = "") -> Tuple[float, str]:
    """
    Определяет приоритетный целевой пик температуры на базе физических законов KB v8.0.
    Учитывает:
    1. Абсолютное лидерство ICON (MAE 0.34-0.60°C по Европе).
    2. Холодный дефект ECMWF в Париже (-1.04°C) и Милане (-1.01°C).
    3. Дефект занижения GFS в Мадриде (-1.01°C).
    4. Ветровую адвекцию Лондона (E/NE барьер Темзы vs SW/W UHI).
    5. Ламинарный перегрев при штиле (<=4 kt: +0.4...+0.6°C) vs турбулентный сдув при порывах.
    6. Перистые облака (Cirrus): прозрачность для солнца (>85%) и парниковое удержание тепла (+0.3...+0.5°C).
    """
    wind_match = re.search(r"\b(\d{3})(\d{2,3})(?:G\d{2,3})?KT\b", raw_metar)
    wdir = int(wind_match.group(1)) if wind_match else None
    wspd = int(wind_match.group(2)) if wind_match else None

    has_low_cloud = any(c in raw_metar for c in ["OVC0", "BKN0", "OVC01", "BKN01", "OVC02", "BKN02", "OVC03", "BKN03"])
    has_cirrus = any(c in raw_metar for c in ["CI", "CS", "FEW2", "SCT2", "FEW3", "SCT3", "NCD", "CAVOK", "CLR", "SKC"])
    is_calm = wspd is not None and wspd <= 4

    icon_val = models.get("icon_global")
    gfs_val = models.get("gfs_global")
    ecm_val = models.get("ecmwf_hres")

    if icao == "EGLC":
        # Лондон
        if wdir is not None and 50 <= wdir <= 120:
            # Холодный эстуарий Темзы: UHI выключен. Строгое вето на GFS!
            val = icon_val if icon_val is not None else (ecm_val if ecm_val is not None else avg_peak)
            return val, f"ICON ({val:.1f}°C, Барьер Темзы)"
        elif wdir is not None and 190 <= wdir <= 280:
            # SW ветер несет городской остров тепла (UHI)
            if wspd and wspd >= 20:
                base = icon_val or avg_peak
                return round(base + 1.0, 1), f"ICON+UHI ({base+1.0:.1f}°C, Шквал SW)"
            if icon_val is not None and gfs_val is not None:
                val = round((icon_val + gfs_val) / 2.0, 1)
                return val, f"ICON+GFS ({val:.1f}°C, UHI активен)"
            val = icon_val if icon_val is not None else (gfs_val if gfs_val is not None else avg_peak)
            return val, f"ICON ({val:.1f}°C, UHI)"
        else:
            val = icon_val if icon_val is not None else avg_peak
            return val, f"ICON ({val:.1f}°C, Лидер точности)"

    elif icao == "LFPB":
        # Париж (Ле Бурже): Лидер ICON (MAE 0.40°C), вето на ECMWF (-1.04°C)
        base = icon_val if icon_val is not None else (gfs_val if gfs_val is not None else avg_peak)
        add = 0.0
        reason = f"ICON ({base:.1f}°C)"
        if is_calm:
            add += 0.4
            reason = f"ICON+0.4°C ({base+add:.1f}°C, Штиль/Ламинар)"
        elif has_cirrus and not has_low_cloud:
            add += 0.3
            reason = f"ICON+0.3°C ({base+add:.1f}°C, Cirrus)"
        return round(base + add, 1), reason

    elif icao == "LIMC":
        # Милан (Мальпенса): Лидер ICON (MAE 0.46°C), вето на ECMWF (-1.01°C)
        base = icon_val if icon_val is not None else avg_peak
        add = 0.0
        reason = f"ICON ({base:.1f}°C)"
        if is_calm:
            add += 0.4
            reason = f"ICON+0.4°C ({base+add:.1f}°C, Купол По)"
        elif has_cirrus and not has_low_cloud:
            add += 0.3
            reason = f"ICON+0.3°C ({base+add:.1f}°C, Cirrus)"
        return round(base + add, 1), reason

    elif icao == "LEMD":
        # Мадрид (Барахас): Плато Месета (>600 м). Строгое ВЕТО на GFS (-1.01°C занижение!)
        if icon_val is not None:
            val = round(icon_val + 0.4, 1)
            return val, f"ICON+0.4°C ({val:.1f}°C, Месета)"
        elif ecm_val is not None:
            return ecm_val, f"ECMWF ({ecm_val:.1f}°C, Месета)"
        return avg_peak, f"Консенсус ({avg_peak:.1f}°C)"

    elif icao == "EDDM":
        # Мюнхен: Альпийский фён при южном ветре
        if wdir is not None and 150 <= wdir <= 210:
            val = round(avg_peak + 1.2, 1)
            return val, f"Альпийский фён ({val:.1f}°C)"
        val = icon_val if icon_val is not None else avg_peak
        return val, f"ICON ({val:.1f}°C)"

    else:
        val = icon_val if icon_val is not None else avg_peak
        return val, f"ICON/Консенсус ({val:.1f}°C)"


_get_priority_target = get_priority_target


def build_morning_city_block(city_data: Dict[str, Any]) -> str:
    """Формирует блок города для утреннего базового прогноза (10:02 ХБР)."""
    icao = city_data["icao"]
    city_name = city_data["city_name"]
    local_dt = city_data["local_dt"]
    temp_c = city_data["temp_c"]
    models = city_data["models_max"]
    peak_str = city_data["peak_str"]
    avg_peak = city_data.get("avg_peak", 20.0)
    physics_note = city_data["physics_note"]
    raw_metar = city_data["raw_metar"]
    orderbook = city_data.get("orderbook", [])

    ecmwf_s = f"{models.get('ecmwf_hres', 'Н/Д')}°C"
    gfs_s = f"{models.get('gfs_global', 'Н/Д')}°C"
    icon_s = f"{models.get('icon_global', 'Н/Д')}°C"
    gem_s = f"{models.get('gem_global', 'Н/Д')}°C"

    target_val, priority_name = _get_priority_target(icao, models, avg_peak, raw_metar)

    # Проверка стакана на Sniper Momentum и Анти-Скип
    favorite_candidate = None
    is_overheated = False
    if orderbook:
        for item in orderbook:
            t_num = item.get("temp")
            p_cents = item.get("price_cents", 0.0)
            if t_num is not None and abs(t_num - target_val) <= 0.6:
                favorite_candidate = item
                if p_cents >= 80.0:
                    is_overheated = True

    is_rain = any(s in raw_metar for s in ["RA", "DZ", "TS", "SN"]) and "OVC" in raw_metar
    if is_rain:
        status_line = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> — Обложные осадки глушат дневную радиацию."
    elif is_overheated:
        status_line = (
            "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b>\n"
            "⚠️ <b>ПОКУПКА ОДИНОЧНОГО СТРАЙКА ЗДЕСЬ = СЛИВ ДЕПОЗИТА.</b> Рынок перегрет маркетмейкером, сиди на заборе."
        )
    elif favorite_candidate and 25.0 <= favorite_candidate["price_cents"] <= 48.0:
        status_line = (
            f"🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b>\n"
            f"• Рекомендуемый исход: <code>{favorite_candidate['title']}</code> (цена <b>{favorite_candidate['price_cents']:.0f}¢</b>)\n"
            f"• Цель: продажа токена толпе на дневном разгоне, а не удержание до ночи."
        )
    else:
        status_line = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Ориентир корзины ≤ 75¢, одиночный Sniper 25¢–48¢)."

    return (
        f"📍 <b>{city_name}</b> (Время: <code>{local_dt.strftime('%H:%M')} LT</code>)\n"
        f"• Факт METAR: <code>{temp_c if temp_c is not None else 'Н/Д'}°C</code>\n"
        f"• Модели: ECMWF: {ecmwf_s} | GFS: {gfs_s} | ICON: {icon_s} | GEM: {gem_s}\n"
        f"• Ожидаемый пик: <b>{peak_str}</b> (Опора: {priority_name})\n"
        f"• Драйвер: <i>{physics_note}</i>\n"
        f"{status_line}"
    )


def build_dynamic_city_block(city_data: Dict[str, Any], user_position: Optional[Dict[str, Any]]) -> str:
    """Формирует блок города для регулярного обновления динамики (:02 и :32)."""
    icao = city_data["icao"]
    city_name = city_data["city_name"]
    local_dt = city_data["local_dt"]
    temp_c = city_data["temp_c"]
    models = city_data.get("models_max", {})
    rate_str = city_data["rate_str"]
    rate_val = city_data["rate_val"]
    rem_hours = city_data["rem_hours_str"]
    peak_str = city_data["peak_str"]
    avg_peak = city_data.get("avg_peak", 20.0)
    physics_note = city_data["physics_note"]
    raw_metar = city_data["raw_metar"]
    orderbook = city_data["orderbook"]

    local_hour = local_dt.hour
    local_min = local_dt.minute
    local_time_val = local_hour + local_min / 60.0

    lines = [
        f"📍 <b>{city_name}</b> (<code>{local_dt.strftime('%H:%M')} LT</code> | Инсоляция: <b>{rem_hours}</b>)",
        f"• Факт METAR: <code>{temp_c if temp_c is not None else 'Н/Д'}°C</code> (Темп: <b>{rate_str}</b>) | Ожидаемый пик: <b>{peak_str}</b>",
        f"• Физика: <i>{physics_note}</i>",
    ]

    # СЦЕНАРИЙ 1: У пользователя есть открытая сделка по этому городу
    if user_position:
        target_outcomes = user_position.get("outcomes", "Н/Д")
        entry_price = float(user_position.get("entry_price") or 0.0)

        # Пытаемся получить актуальную цену из стакана
        cur_price = get_current_outcome_price(orderbook, target_outcomes)
        if cur_price is None:
            cur_price = entry_price if entry_price > 0 else 35.0  # Разумный fallback

        if entry_price > 0:
            pnl_val = round(((cur_price - entry_price) / entry_price) * 100, 1)
            pnl_str = f"{'+' if pnl_val >= 0 else ''}{pnl_val:.0f}%"
        else:
            pnl_str = "+0%"
            entry_price = cur_price

        lines.append(
            f"💼 <b>ВАША ПОЗИЦИЯ:</b> <code>{target_outcomes}</code> "
            f"(вход: <code>{entry_price:.0f}¢</code> | сейчас в стакане: <code>{cur_price:.0f}¢</code> | PnL: <b>{pnl_str}</b>)"
        )

        target_temp = extract_temp_value(target_outcomes)

        # 1. Триггер Тейк-Профита (Take-Profit Alert: PnL >= +35% или цена >= 60¢)
        if (entry_price > 0 and (cur_price - entry_price) / entry_price >= 0.35) or cur_price >= 60.0:
            verdict = (
                f"🚨 <b>ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ)</b> — Цель импульса закрыта ({pnl_str}). "
                f"До экспирации не сидеть! Сбрасывай страйк по лимитке в стакан прямо сейчас!"
            )
        # 2. Триггер Тайм-Стопа (13:30 LT)
        elif (local_time_val >= 13.5) and (target_temp and temp_c and temp_c < target_temp):
            verdict = (
                f"⏱️ <b>ТАЙМ-СТОП (13:30 LT)</b> — Сброс в рынок для спасения остаточной стоимости! До полудня цель не пробита."
            )
        # 3. Триггер Физического слома: СТРОГО в диапазоне дневной инсоляции (10:30 LT - 15:00 LT)
        elif 10.5 <= local_time_val <= 15.0 and (rate_val < 0.4 and any(c in raw_metar for c in ["BKN", "OVC", "RA"])):
            verdict = (
                f"🛑 <b>ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)</b> — Темп прогрева затух (<+0.4°C/ч) и небо затянуло облачностью!"
            )
        # 4. Раннее утро (LT < 09:00): ночное выхолаживание не инвалидирует позу
        elif local_hour < 9:
            verdict = "🟢 <b>ДЕРЖАТЬ ПОЗИЦИЮ (УТРЕННИЙ ПОЛ)</b> — До 09:00 LT идет предрассветное выхолаживание, старт инсоляции впереди."
        # 5. Нормальное удержание
        else:
            verdict = "🟢 <b>ДЕРЖАТЬ ПОЗИЦИЮ</b> — Темп прогрева в норме, инсоляция работает по плану."

        lines.append(f"👉 <b>ВЕРДИКТ:</b> {verdict}")

    # СЦЕНАРИЙ 2: Позиции нет — оцениваем общий статус рынка
    else:
        is_overcast = any(c in raw_metar for c in ["OVC", "RA", "DZ"])
        has_heavy_rain = any(c in raw_metar for c in ["RA", "DZ", "TS", "SN"]) and "OVC" in raw_metar

        target_val, priority_name = _get_priority_target(icao, models, avg_peak, raw_metar)
        fav_candidate = None
        is_overheated = False
        if orderbook:
            for item in orderbook:
                t_num = item.get("temp")
                p_cents = item.get("price_cents", 0.0)
                if t_num is not None and abs(t_num - target_val) <= 0.6:
                    fav_candidate = item
                    if p_cents >= 80.0:
                        is_overheated = True

        # 1. Раннее утро (LT < 09:00): расчет темпа не переводит в СКИП!
        if local_hour < 9:
            if has_heavy_rain:
                status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Обложные осадки глушат утреннюю радиацию)"
            else:
                status_desc = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Ожидание старта инсоляции)"

        # 2. Окно дневной инсоляции закрыто (после 16:30 LT):
        elif rem_hours == "Окно закрыто" or local_time_val >= 16.5:
            status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Дневной пик пройден)"

        # 3. Активный дневной диапазон инсоляции (10:30 LT - 15:00 LT):
        elif 10.5 <= local_time_val <= 15.0:
            is_cloudy = any(c in raw_metar for c in ["BKN", "OVC", "RA", "DZ"])
            if rate_val < 0.4 and is_cloudy:
                status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Физический слом: темп < +0.4°C/ч и натекание облачности)"
            elif is_overheated:
                status_desc = (
                    "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b>\n"
                    "⚠️ <b>ПОКУПКА ОДИНОЧНОГО СТРАЙКА ЗДЕСЬ = СЛИВ ДЕПОЗИТА.</b> Рынок перегрет маркетмейкером, сиди на заборе."
                )
            elif fav_candidate and 25.0 <= fav_candidate["price_cents"] <= 48.0:
                status_desc = (
                    f"🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b> "
                    f"(Вход: <code>{fav_candidate['title']}</code> {fav_candidate['price_cents']:.0f}¢. "
                    f"Цель: продажа токена толпе на дневном разгоне, а не удержание до ночи)"
                )
            elif rate_val >= 0.5:
                status_desc = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Импульсный дневной прогрев)"
            else:
                status_desc = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Мониторинг дневной динамики)"

        # 4. Промежуток 09:00 - 10:30 LT (первый разогрев после восхода):
        else:
            if has_heavy_rain:
                status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Осадки блокируют утренний прогрев)"
            elif is_overheated:
                status_desc = (
                    "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b>\n"
                    "⚠️ <b>ПОКУПКА ОДИНОЧНОГО СТРАЙКА ЗДЕСЬ = СЛИВ ДЕПОЗИТА.</b> Рынок перегрет маркетмейкером, сиди на заборе."
                )
            elif fav_candidate and 25.0 <= fav_candidate["price_cents"] <= 48.0:
                status_desc = (
                    f"🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b> "
                    f"(Вход: <code>{fav_candidate['title']}</code> {fav_candidate['price_cents']:.0f}¢. "
                    f"Цель: продажа токена толпе на дневном разгоне, а не удержание до ночи)"
                )
            else:
                status_desc = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Утренний разгон инсоляции)"

        lines.append(f"👉 {status_desc}")

    return "\n".join(lines)


async def send_consolidated_digest(
    bot: Bot,
    cities_metrics: List[Dict[str, Any]],
    is_morning_base: bool,
    now_khv: datetime,
) -> None:
    """Собирает все 4 города в единый пост и отправляет админу и активным пользователям."""
    today_khv_str = now_khv.strftime("%d.%m.%Y")
    time_khv_str = now_khv.strftime("%H:%M")

    if is_morning_base:
        header = (
            f"🌅 <b>НОВЫЙ ТОРГОВЫЙ ДЕНЬ | БАЗОВЫЙ ПРОГНОЗ ({today_khv_str})</b>\n"
            f"<i>Региональный синоптический срез моделей (Время ХБР: {time_khv_str})</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        body_blocks = [build_morning_city_block(cm) for cm in cities_metrics if cm]
    else:
        header = (
            f"🔄 <b>ОБНОВЛЕНИЕ НА {time_khv_str} ХБР | ДИНАМИКА</b>\n"
            f"<i>Контроль темпа прогрева и статус открытых позиций</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

    # Получаем все активные позиции из базы данных
    active_positions = await asyncio.to_thread(get_all_active_positions)
    admin_id = getattr(config, "ADMIN_CHAT_ID", None)

    # 1. Отправка администратору
    if admin_id:
        if is_morning_base:
            full_msg = f"{header}\n\n" + "\n\n──────────────\n\n".join(body_blocks)
        else:
            # Персонализируем для админа, если у него есть позиции
            admin_blocks = []
            for cm in cities_metrics:
                admin_pos = next((p for p in active_positions if p["icao"] == cm["icao"] and p["user_id"] == admin_id), None)
                admin_blocks.append(build_dynamic_city_block(cm, admin_pos))
            full_msg = f"{header}\n\n" + "\n\n──────────────\n\n".join(admin_blocks)

        try:
            await bot.send_message(
                chat_id=admin_id,
                text=full_msg,
                parse_mode="HTML",
                reply_markup=express_scan_keyboard,
            )
        except Exception as e:
            logger.warning(f"Сбой отправки дайджеста админу {admin_id}: {e}")

    # 2. Отправка пользователям, держащим открытые позиции
    distinct_user_ids = {p["user_id"] for p in active_positions if p.get("user_id") and p.get("user_id") != admin_id}
    for uid in distinct_user_ids:
        if is_morning_base:
            user_msg = f"{header}\n\n" + "\n\n──────────────\n\n".join(body_blocks)
        else:
            user_blocks = []
            for cm in cities_metrics:
                user_pos = next((p for p in active_positions if p["icao"] == cm["icao"] and p["user_id"] == uid), None)
                user_blocks.append(build_dynamic_city_block(cm, user_pos))
            user_msg = f"{header}\n\n" + "\n\n──────────────\n\n".join(user_blocks)

        try:
            await bot.send_message(
                chat_id=uid,
                text=user_msg,
                parse_mode="HTML",
                reply_markup=express_scan_keyboard,
            )
        except Exception as e:
            logger.debug(f"Не удалось отправить дайджест пользователю {uid}: {e}")


async def run_auto_scanner(bot: Bot) -> None:
    """
    Основной цикл автоматического сканера.
    Синхронизирован со сводками METAR строго 2 раза в час: ровно в :02 и :32 минуты.
    Работает в активное торговое окно 10:00 - 00:00 по времени Хабаровска (UTC+10).
    """
    global _LAST_MORNING_DIGEST_DATE, _LAST_SENT_SLOT
    logger.info("🚀 Единый консолидированный автосканер v7.1 запущен (Расписание METAR: :02 и :32).")

    while True:
        try:
            now_khv = datetime.now(KHV_TZ)

            # 1. Проверяем ночное окно Хабаровска (вне 10:00 - 00:00 ХБР)
            if not is_khv_active_hours():
                # Рассчитываем точное время сна до 10:02 ХБР
                if now_khv.hour < 10:
                    wake_target = now_khv.replace(hour=10, minute=2, second=2, microsecond=0)
                else:
                    wake_target = (now_khv + timedelta(days=1)).replace(hour=10, minute=2, second=2, microsecond=0)
                sleep_night = max(10.0, (wake_target - now_khv).total_seconds())
                logger.info(
                    f"🌙 Ночное окно Хабаровска ({now_khv.strftime('%H:%M')} ХБР). "
                    f"Сканер спит до 10:02 ХБР ({int(sleep_night // 3600)}ч {int((sleep_night % 3600) // 60)}м)."
                )
                await asyncio.sleep(sleep_night)
                continue

            # 2. Проверяем, находимся ли мы прямо сейчас в контрольной минуте (:02 или :32)
            current_checkpoint_slot: Optional[str] = None
            if now_khv.minute in (2, 3):
                current_checkpoint_slot = f"{now_khv.strftime('%Y-%m-%d %H')}:02"
            elif now_khv.minute in (32, 33):
                current_checkpoint_slot = f"{now_khv.strftime('%Y-%m-%d %H')}:32"

            # 3. Если мы в контрольной точке И дайджест для этого слота еще не отправлялся — отправляем!
            if current_checkpoint_slot and current_checkpoint_slot != _LAST_SENT_SLOT:
                today_khv_str = now_khv.strftime("%Y-%m-%d")

                # Утренний базовый пост отправляется на первом слоте 10:00 ХБР
                is_morning_base = False
                if now_khv.hour == 10 and _LAST_MORNING_DIGEST_DATE != today_khv_str:
                    is_morning_base = True
                    _LAST_MORNING_DIGEST_DATE = today_khv_str
                    logger.info("🌅 Формирование утреннего базового прогноза (10:02 ХБР)...")
                else:
                    logger.info(f"🔄 Сбор планового METAR-обновления ({current_checkpoint_slot} ХБР)...")

                # Последовательный сбор метрик с интервалом 0.25 сек (защита от Open-Meteo 429)
                cities_metrics = []
                for icao in TARGET_CITIES.keys():
                    cm = await collect_city_metrics(icao)
                    cities_metrics.append(cm)
                    await asyncio.sleep(0.25)

                # Отправка дайджеста
                await send_consolidated_digest(bot, cities_metrics, is_morning_base, now_khv)

                # Защита от повторной отправки в этом же слоте
                _LAST_SENT_SLOT = current_checkpoint_slot
                logger.info(f"✅ Дайджест {current_checkpoint_slot} ХБР успешно разослан.")

            # 4. Расчет точного сна до СЛЕДУЮЩЕЙ контрольной точки (:02 или :32)
            now_after = datetime.now(KHV_TZ)
            sleep_secs, next_target, next_slot_str = get_next_sleep_seconds(now_after)

            logger.info(
                f"⏳ Следующий плановый дайджест в {next_target.strftime('%H:%M:%S')} ХБР "
                f"(сон: {int(sleep_secs)} сек / {sleep_secs/60:.1f} мин)."
            )
            await asyncio.sleep(sleep_secs)

        except asyncio.CancelledError:
            logger.info("🛑 Автосканер остановлен.")
            break
        except Exception as loop_err:
            logger.error(f"⚠️ Сбой в основном цикле автосканера: {loop_err}", exc_info=True)
            await asyncio.sleep(15)

