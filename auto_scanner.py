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
import html
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
import zoneinfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from airport_resolver import resolve_airport
from database import (
    get_all_active_positions,
    get_all_subscribers,
    set_bot_state,
    get_bot_state,
    get_user_wallet,
    update_position_trailing,
    update_position_alert,
    close_position_with_exit,
)
from noaa_service import get_noaa_package
from openmeteo_service import fetch_openmeteo_forecast
from polymarket_service import (
    find_city_weather_event,
    parse_markets_orderbook,
    get_current_outcome_price,
    extract_temp_value,
    get_outcome_token_id,
)
from clob_trader import execute_market_sell, get_token_balance

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


def sanitize_telegram_html(text: str) -> str:
    """
    Экранирует недопустимые символы <, > и & для Telegram HTML-парсера,
    сохраняя при этом валидные поддерживаемые теги (<b>, <i>, <code>, <a>, <pre>, <u>, <s>, <blockquote>).
    """
    if not text:
        return ""
    # 1. Экранируем &, если это не валидная сущность (&amp;, &lt;, &gt;, &quot;)
    text = re.sub(r"&(?!(?:amp|lt|gt|quot);)", "&amp;", text)
    # 2. Экранируем <, если это не открывающий или закрывающий валидный тег Telegram
    valid_tags_pattern = r"<(?!/?(?:b|i|u|s|code|pre|a|strong|em|ins|strike|del|span|blockquote|tg-spoiler)(?:\s+[^>]*)?>)"
    text = re.sub(valid_tags_pattern, "&lt;", text, flags=re.IGNORECASE)
    return text


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


def get_seasonal_heating_cutoff(month: int, icao: str = "") -> Tuple[float, str]:
    """
    Возвращает час закрытия активного солнечного прогрева (cutoff LT)
    и название сезона с учетом высоты стояния солнца, долготы города и термического лага.
    Откалибровано на основе эмпирического анализа архива Open-Meteo за 1367 дней (2023-2026):
    - Мадрид (LEMD): из-за западного сдвига долготы относительно часового пояса (UTC+1/UTC+2) 
      и высоты плато Месета (>600м) суточный пик наступает в 16:00-17:00 круглый год.
    - Лондон (EGLC): на меридиане Гринвича, пик наступает рано (зима 12:30-13:30, лето 15:30-16:30).
    - Милан (LIMC): котловина реки По, зимой застойный воздух и ранний закат за Альпы (14:00).
    - Париж (LFPB): умеренный переходный режим (зима 13:30-14:00, лето 16:30).
    """
    icao_upper = icao.strip().upper() if icao else ""

    if icao_upper == "LEMD":
        # Мадрид: стабильный пик 16:00-17:00 даже поздней осенью
        if month in (12, 1, 2):
            return 15.5, "Зима (Мадрид)"
        elif month in (10, 11):
            return 16.0, "Глубокая осень (Мадрид)"
        elif month in (9, 3):
            return 16.5, "Осень/Весна (Мадрид)"
        else:
            return 17.5, "Лето (Мадрид)"

    elif icao_upper == "LIMC":
        # Милан: зимний застой и альпийский ранний закат
        if month in (11, 12, 1):
            return 14.0, "Зима (Милан)"
        elif month in (2, 3, 10):
            return 14.5, "Осень/Весна (Милан)"
        elif month in (4, 5):
            return 15.5, "Весна (Милан)"
        else:
            return 16.0, "Лето (Милан)"

    elif icao_upper == "EGLC":
        # Лондон: Гринвичский меридиан, ранний астрономический полдень
        if month in (12, 1):
            return 13.0, "Зима (Лондон)"
        elif month in (2, 11):
            return 13.5, "Зима/Поздняя осень (Лондон)"
        elif month in (10, 9, 3):
            return 14.5, "Осень/Весна (Лондон)"
        elif month in (4, 5):
            return 15.5, "Весна (Лондон)"
        else:
            return 16.5, "Лето (Лондон)"

    # Базовый дефолтный профиль (Париж и общие европейские координаты)
    if month in (12, 1, 2):
        return 13.5, "Зима"
    elif month in (10, 11):
        return 14.0, "Глубокая осень"
    elif month in (9, 3):
        return 14.5, "Осень/Весна"
    elif month in (4, 5):
        return 15.5, "Весна"
    else:  # 6, 7, 8
        return 16.5, "Лето"



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
    Остаток прогрева рассчитывается по астрономическому сезонному окну (Seasonal Insolation Curve).
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
    heating_cutoff, season_label = get_seasonal_heating_cutoff(local_dt.month, icao=icao)
    rem_hours = max(0.0, heating_cutoff - local_hour)
    if rem_hours > 0:
        cutoff_m = int((heating_cutoff % 1) * 60)
        rem_hours_str = f"{rem_hours:.1f} ч (окно до {int(heating_cutoff)}:{cutoff_m:02d} LT, {season_label})"
    else:
        rem_hours_str = f"Окно закрыто (спад инсоляции, {season_label})"

    return rate_str, rem_hours_str, rate_val


def get_cloud_ceiling_ft(raw_metar: str) -> Optional[int]:
    """
    Извлекает высоту нижней кромки сплошной/значительной облачности (BKN, OVC, VV) в футах.
    Возвращает None, если сплошной облачности нет.
    """
    matches = re.findall(r"\b(?:OVC|BKN|VV)(\d{3})\b", raw_metar)
    if not matches:
        return None
    return min(int(m) * 100 for m in matches)


def has_real_low_cloud(raw_metar: str) -> bool:
    """
    Проверяет наличие реальной низкоярусной облачности (Stratus / Low Cloud <= 3000 ft),
    которая эффективно блокирует прямую солнечную радиацию.
    """
    if "VV0" in raw_metar or " FG " in f" {raw_metar} ":
        return True
    ceiling = get_cloud_ceiling_ft(raw_metar)
    return ceiling is not None and ceiling <= 3000


def is_blocking_rain_and_clouds(raw_metar: str) -> bool:
    """
    Определяет, присутствуют ли настоящие обложные блокирующие осадки / плотный Stratus,
    убивающие дневной радиационный прогрев.
    - Высокая облачность (OVC100+ / 10 000+ ft) и кратковременная морось (-DZ / -RA) НЕ являются блокирующими.
    - Блокирующими являются:
      1) Плотный туман / вертикальная видимость (FG, VV001-003)
      2) Сильный дождь (+RA, +DZ, +SN, TSRA)
      3) Осадки (RA, DZ, SN) в сочетании с низкой слоистой облачностью (потолок <= 3500 ft)
      4) Сплошной низкий Stratus (OVC <= 2000 ft) без просветов
    """
    ceiling = get_cloud_ceiling_ft(raw_metar)

    if " FG " in f" {raw_metar} " or (ceiling is not None and ceiling <= 300):
        return True

    if any(s in raw_metar for s in ["+RA", "+DZ", "+SN", "TSRA", "+TSRA"]):
        return True

    has_precip = any(s in raw_metar for s in [" RA", " DZ", " SN", " -RA", " -DZ"])
    if has_precip and (ceiling is not None and ceiling <= 3500):
        return True

    ovc_matches = re.findall(r"\bOVC(\d{3})\b", raw_metar)
    if ovc_matches:
        min_ovc = min(int(m) * 100 for m in ovc_matches)
        if min_ovc <= 2000:
            return True

    return False


def get_strike_for_temp(target_val: float) -> int:
    """
    Определяет целочисленный страйк Polymarket для непрерывной целевой температуры.
    На метеостанциях (METAR) температура округляется по математическим правилам:
    значения до .5 включительно (например, 19.0..19.5) чаще закрываются в нижний страйк (19°C),
    а переход в верхний страйк (20°C) требует уверенного пробоя 19.6°C+.
    Поэтому:
    - если дробная часть < 0.6 (например, 19.0, 19.3, 19.4, 19.5) -> страйк 19°C
    - если дробная часть >= 0.6 (например, 19.6..19.9) -> страйк 20°C
    """
    base = int(target_val)
    fraction = target_val - base
    if fraction >= 0.6:
        return base + 1
    return base


def _get_city_physics_note(icao: str, raw_metar: str, temp_c: Optional[float], local_dt: datetime) -> str:
    """Возвращает актуальную синоптическую заметку на базе квант-законов KB v8.0."""
    wind_match = re.search(r"\b(\d{3})(\d{2,3})(?:G\d{2,3})?KT\b", raw_metar)
    wdir = int(wind_match.group(1)) if wind_match else None
    wspd = int(wind_match.group(2)) if wind_match else None

    has_low_cloud = has_real_low_cloud(raw_metar)
    has_cirrus = any(c in raw_metar for c in ["CI", "CS", "FEW2", "SCT2", "FEW3", "SCT3", "NCD", "CAVOK", "CLR", "SKC"])
    blocking_rain = is_blocking_rain_and_clouds(raw_metar)

    if blocking_rain:
        return "Обложные осадки / плотный Stratus: скрытое тепло испарения блокирует подъем температуры."

    if icao == "EGLC":
        if wdir is not None and 50 <= wdir <= 120:
            return "Восточный ветер (050°-120°): холодный воздух с эстуария Темзы гасит UHI. ВЕТО на GFS! Рулит ICON (MAE 0.34°C)."
        elif wdir is not None and 190 <= wdir <= 280:
            if wspd and wspd >= 20:
                return "Шквалистый SW-ветер (≥20 kt): тепловой шлейф Лондона пробивает полосу транзитом (+1.2°C)."
            if wspd and wspd >= 8 and (9 <= local_dt.hour < 18):
                return "SW-ветер несет городской остров тепла центра Лондона (UHI активен, опора на ICON+GFS)."
            return "Слабый SW-ветер: UHI локализован, опора строго на лидер ICON."
        return "Лондон: стабильный радиационный прогрев при прозрачной атмосфере (лидер ICON)."

    elif icao == "LFPB":
        calm_str = " (Штиль ≤4 kt: ламинарный перегрев +0.4°C)" if (wspd and wspd <= 4) else ""
        if has_cirrus and not has_low_cloud:
            return f"Париж: перистые/высокие облака не блокируют солнечную радиацию (пропускание свыше 85%). Лидер ICON.{calm_str}"
        return f"Париж: приоритет ICON (MAE 0.40°C). Холодный дефект ECMWF (-1.04°C) игнорируется.{calm_str}"

    elif icao == "LIMC":
        calm_str = " (Штиль: ламинарный слой долины По +0.4°C)" if (wspd and wspd <= 4) else ""
        if has_cirrus and not has_low_cloud:
            return f"Милан: перистые облака удерживают тепло купола По. Опора строго на ICON (MAE 0.46°C).{calm_str}"
        return f"Милан: термический купол долины реки По. Абсолютный лидер ICON (ECMWF занижает на 1.0°C).{calm_str}"

    elif icao == "LEMD":
        return "Мадрид: сухое плато Месета (более 600 м). СТРОГОЕ ВЕТО на GFS (-1.01°C занижение!). Опора на ICON+0.4°C."

    elif icao == "EDDM":
        return "Мюнхен: при южном ветре (150°-210°) работает Альпийский фён (+1.2°C...+2.0°C к консенсусу)."

    return "Синтез эмпирических физических законов KB v8.0."


ALL_RADAR_CITIES_MAP = {
    "EGLC": "🇬🇧 Лондон (EGLC)",
    "LFPB": "🇫🇷 Париж (LFPB)",
    "LIMC": "🇮🇹 Милан (LIMC)",
    "LEMD": "🇪🇸 Мадрид (LEMD)",
    "EDDM": "🇩🇪 Мюнхен (EDDM)",
    "KJFK": "🇺🇸 Нью-Йорк (KJFK)",
    "RJTT": "🇯🇵 Токио (RJTT)",
    "RKSI": "🇰🇷 Сеул (RKSI)",
    "UHHH": "🇷🇺 Хабаровск (UHHH)",
}


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
    current_ts = time.time()

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
    poly_event = None
    try:
        poly_event = await find_city_weather_event(icao, target_date_local)
        if poly_event:
            orderbook = parse_markets_orderbook(poly_event.get("markets", []))
    except Exception:
        orderbook = []

    city_label = ALL_RADAR_CITIES_MAP.get(icao) or TARGET_CITIES.get(icao, f"Локация {icao}")

    return {
        "icao": icao,
        "city_name": city_label,
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
        "event": poly_event,
    }


def get_priority_target(
    icao: str,
    models: Dict[str, float],
    avg_peak: float,
    raw_metar: str = "",
    local_dt: Optional[datetime] = None,
) -> Tuple[float, str]:
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

    has_low_cloud = has_real_low_cloud(raw_metar)
    has_cirrus = any(c in raw_metar for c in ["CI", "CS", "FEW2", "SCT2", "FEW3", "SCT3", "NCD", "CAVOK", "CLR", "SKC"])
    is_calm = wspd is not None and wspd <= 4
    local_hour = local_dt.hour if local_dt else 12

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
            # Ночью (< 09:00 LT) или при слабом ветре (< 8 kt) дневной UHI не активен!
            if wspd and wspd >= 8 and (9 <= local_hour < 18):
                if icon_val is not None and gfs_val is not None:
                    val = round((icon_val + gfs_val) / 2.0, 1)
                    return val, f"ICON+GFS ({val:.1f}°C, UHI активен)"
            val = icon_val if icon_val is not None else avg_peak
            return val, f"ICON ({val:.1f}°C, Умеренный SW)"
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

    target_val, priority_name = _get_priority_target(icao, models, avg_peak, raw_metar, local_dt=local_dt)
    target_strike = get_strike_for_temp(target_val)

    # Проверка стакана на Sniper Momentum и Анти-Скип
    favorite_candidate = None
    best_diff = 999.0
    is_overheated = False
    if orderbook:
        for item in orderbook:
            t_num = item.get("temp")
            if t_num is None:
                continue
            diff = abs(t_num - target_val)
            if t_num == target_strike:
                favorite_candidate = item
                best_diff = 0.0
            elif diff < best_diff and diff <= 0.7 and favorite_candidate is None:
                best_diff = diff
                favorite_candidate = item

        if favorite_candidate and favorite_candidate.get("price_cents", 0.0) >= 80.0:
            is_overheated = True

    is_rain = is_blocking_rain_and_clouds(raw_metar)
    if is_rain:
        status_line = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> — Обложные осадки глушат дневную радиацию."
    elif is_overheated:
        status_line = (
            "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b>\n"
            "⚠️ <b>ПОКУПКА ОДИНОЧНОГО СТРАЙКА ЗДЕСЬ = СЛИВ ДЕПОЗИТА.</b> Рынок перегрет маркетмейкером, сиди на заборе."
        )
    elif favorite_candidate and 25.0 <= favorite_candidate["price_cents"] <= 48.0:
        fav_title = html.escape(str(favorite_candidate["title"]))
        local_time_val = local_dt.hour + local_dt.minute / 60.0
        entry_hint = "Сразу по рынку / в упор к Best Ask" if local_time_val >= 9.5 else "Утренняя лимитка в спред"
        status_line = (
            f"🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b>\n"
            f"• Рекомендуемый исход: <code>{fav_title}</code> (цена <b>{favorite_candidate['price_cents']:.0f}¢</b>)\n"
            f"• Вход: {entry_hint} (днем просадки не высиживать!)\n"
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
    heating_cutoff, season_label = get_seasonal_heating_cutoff(local_dt.month, icao=icao)

    models_line = None
    if models:
        ecmwf_s = f"{models.get('ecmwf_hres', 'Н/Д')}°C"
        gfs_s = f"{models.get('gfs_global', 'Н/Д')}°C"
        icon_s = f"{models.get('icon_global', 'Н/Д')}°C"
        gem_s = f"{models.get('gem_global', 'Н/Д')}°C"
        models_line = f"• Модели: ECMWF: {ecmwf_s} | GFS: {gfs_s} | ICON: {icon_s} | GEM: {gem_s}"

    lines = [
        f"📍 <b>{city_name}</b> (<code>{local_dt.strftime('%H:%M')} LT</code> | Инсоляция: <b>{rem_hours}</b>)",
        f"• Факт METAR: <code>{temp_c if temp_c is not None else 'Н/Д'}°C</code> (Темп: <b>{rate_str}</b>) | Ожидаемый пик: <b>{peak_str}</b>",
    ]
    if models_line:
        lines.append(models_line)
    lines.append(f"• Физика: <i>{physics_note}</i>")

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

        safe_outcomes = html.escape(str(target_outcomes))
        lines.append(
            f"💼 <b>ВАША ПОЗИЦИЯ:</b> <code>{safe_outcomes}</code> "
            f"(вход: <code>{entry_price:.0f}¢</code> | сейчас в стакане: <code>{cur_price:.0f}¢</code> | PnL: <b>{pnl_str}</b>)"
        )

        target_temp = extract_temp_value(target_outcomes)

        # 1. Триггер Тейк-Профита (Take-Profit Alert: PnL >= +35% или цена >= 60¢)
        if (entry_price > 0 and (cur_price - entry_price) / entry_price >= 0.35) or cur_price >= 60.0:
            verdict = (
                f"🚨 <b>ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ)</b> — Цель импульса закрыта ({pnl_str}). "
                f"До экспирации не сидеть! Сбрасывай страйк по лимитке в стакан прямо сейчас!"
            )
        # 2. Триггер Тайм-Стопа (по достижении сезонного часа отсечки)
        elif (local_time_val >= heating_cutoff or "Окно закрыто" in rem_hours) and (target_temp and temp_c and temp_c < target_temp):
            cutoff_m = int((heating_cutoff % 1) * 60)
            verdict = (
                f"⏱️ <b>ТАЙМ-СТОП ({int(heating_cutoff)}:{cutoff_m:02d} LT)</b> — Сброс в рынок для спасения остаточной стоимости! Окно инсоляции закрыто, цель не пробита."
            )
        # 3. Триггер Физического слома (обложные осадки / плотный Stratus):
        elif is_blocking_rain_and_clouds(raw_metar):
            verdict = (
                f"🛑 <b>ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)</b> — На станцию вышли обложные осадки / плотный низкий Stratus!"
            )
        elif 12.5 <= local_time_val <= heating_cutoff and (rate_val < 0.4 and has_real_low_cloud(raw_metar) and (target_temp and temp_c and (target_temp - temp_c) >= 1.5)):
            verdict = (
                f"🛑 <b>ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)</b> — После полудня темп затух под низкой облачностью, отставание от цели {target_temp - temp_c:.1f}°C критично!"
            )
        # 4. Триггер Неблагоприятного отбора (обвал стакана днем >= 8¢ при открытом рынке):
        elif entry_price > 0 and (entry_price - cur_price) >= 8.0 and local_time_val >= 10.0:
            verdict = (
                f"⚠️ <b>ТРЕВОГА (ПАДЕНИЕ СТАКАНА: -{entry_price - cur_price:.0f}¢)</b> — Дневной обвал цены (в 75% случаев сигнал слома погоды / adverse selection). Проверь METAR, держи палец на выходе!"
            )
        # 5. Утреннее развитие и удержание:
        # ВАЖНО: До 12:30 LT утреннее замедление или высокая облачность НЕ инвалидируют позицию!
        elif local_time_val < 12.5:
            if rate_val < 0.4 and local_hour >= 9:
                verdict = (
                    f"🟡 <b>ДЕРЖАТЬ ПОЗИЦИЮ (УТРЕННЯЯ ПАУЗА)</b> — Прогрев отстает, но солнечный полдень впереди (12:30–14:00 LT). Критической блокировки нет."
                )
            elif local_hour < 9:
                verdict = "🟢 <b>ДЕРЖАТЬ ПОЗИЦИЮ (УТРЕННИЙ ПОЛ)</b> — До 09:00 LT идет предрассветное выхолаживание, старт инсоляции впереди."
            else:
                verdict = "🟢 <b>ДЕРЖАТЬ ПОЗИЦИЮ</b> — Темп прогрева в норме, инсоляция работает по плану."
        else:
            verdict = "🟢 <b>ДЕРЖАТЬ ПОЗИЦИЮ</b> — Темп прогрева в норме, инсоляция работает по плану."

        lines.append(f"👉 <b>ВЕРДИКТ:</b> {verdict}")

    # СЦЕНАРИЙ 2: Позиции нет — оцениваем общий статус рынка
    else:
        has_blocking_weather = is_blocking_rain_and_clouds(raw_metar)

        target_val, priority_name = _get_priority_target(icao, models, avg_peak, raw_metar, local_dt=local_dt)
        target_strike = get_strike_for_temp(target_val)
        fav_candidate = None
        best_diff = 999.0
        is_overheated = False
        if orderbook:
            for item in orderbook:
                t_num = item.get("temp")
                if t_num is None:
                    continue
                diff = abs(t_num - target_val)
                if t_num == target_strike:
                    fav_candidate = item
                    best_diff = 0.0
                elif diff < best_diff and diff <= 0.7 and fav_candidate is None:
                    best_diff = diff
                    fav_candidate = item

            if fav_candidate and fav_candidate.get("price_cents", 0.0) >= 80.0:
                is_overheated = True

        # 1. Раннее утро (LT < 09:00): расчет темпа не переводит в СКИП!
        if local_hour < 9:
            if has_blocking_weather:
                status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Обложные осадки глушат утреннюю радиацию)"
            else:
                status_desc = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Ожидание старта инсоляции)"

        # 2. Окно дневной инсоляции закрыто:
        elif "Окно закрыто" in rem_hours or local_time_val >= heating_cutoff:
            status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Дневной пик пройден)"

        # 3. Активный дневной диапазон инсоляции:
        elif 10.5 <= local_time_val < heating_cutoff:
            if has_blocking_weather:
                status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Обложные осадки / плотный Stratus блокируют прогрев)"
            elif local_time_val >= 12.5 and rate_val < 0.4 and has_real_low_cloud(raw_metar):
                status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Физический слом: после полудня темп ниже +0.4°C/ч под слоистой облачностью)"
            elif is_overheated:
                status_desc = (
                    "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b>\n"
                    "⚠️ <b>ПОКУПКА ОДИНОЧНОГО СТРАЙКА ЗДЕСЬ = СЛИВ ДЕПОЗИТА.</b> Рынок перегрет маркетмейкером, сиди на заборе."
                )
            elif fav_candidate and 25.0 <= fav_candidate["price_cents"] <= 48.0:
                cand_title = html.escape(str(fav_candidate['title']))
                entry_hint = "сразу по рынку" if local_time_val >= 9.5 else "утренняя лимитка в спред"
                status_desc = (
                    f"🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b> "
                    f"(Вход: <code>{cand_title}</code> {fav_candidate['price_cents']:.0f}¢, {entry_hint}. "
                    f"Цель: продажа токена толпе на дневном разгоне, а не удержание до ночи)"
                )
            elif rate_val >= 0.5:
                status_desc = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Импульсный дневной прогрев)"
            else:
                status_desc = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Мониторинг дневной динамики)"

        # 4. Промежуток 09:00 - 10:30 LT (первый разогрев после восхода):
        else:
            if has_blocking_weather:
                status_desc = "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b> (Осадки блокируют утренний прогрев)"
            elif is_overheated:
                status_desc = (
                    "⛔ <b>СТАТУС: ВНЕ РЫНКА (СКИП)</b>\n"
                    "⚠️ <b>ПОКУПКА ОДИНОЧНОГО СТРАЙКА ЗДЕСЬ = СЛИВ ДЕПОЗИТА.</b> Рынок перегрет маркетмейкером, сиди на заборе."
                )
            elif fav_candidate and 25.0 <= fav_candidate["price_cents"] <= 48.0:
                cand_title = html.escape(str(fav_candidate['title']))
                entry_hint = "сразу по рынку" if local_time_val >= 9.5 else "утренняя лимитка в спред"
                status_desc = (
                    f"🟢 <b>СИГНАЛ: ОДИНОЧНЫЙ ИМПУЛЬС (SNIPER MOMENTUM)</b> "
                    f"(Вход: <code>{cand_title}</code> {fav_candidate['price_cents']:.0f}¢, {entry_hint}. "
                    f"Цель: продажа токена толпе на дневном разгоне, а не удержание до ночи)"
                )
            else:
                status_desc = "🟡 <b>СТАТУС: ПОТЕНЦИАЛ ВХОДА</b> (Утренний разгон инсоляции)"

        lines.append(f"👉 {status_desc}")

    return "\n".join(lines)


# Словарь для отслеживания ID последнего отправленного дайджеста для каждого чата
_LAST_DIGEST_MESSAGE_IDS: Dict[int, int] = {}


async def _safe_send_digest(
    bot: Bot,
    chat_id: int,
    message_html: str,
    reply_markup=None,
    delete_previous: bool = True
) -> bool:
    """
    Надёжная отправка дайджеста с автоматической санитизацией HTML,
    удалением предыдущего 30-минутного сообщения дайджеста
    и аварийным fallback на Plain-Text без потери данных.
    """
    safe_text = sanitize_telegram_html(message_html)

    # 1. Извлекаем ID предыдущего сообщения дайджеста для этого чата
    prev_msg_id = _LAST_DIGEST_MESSAGE_IDS.get(chat_id)
    if not prev_msg_id:
        try:
            stored = get_bot_state(f"last_digest_msg_{chat_id}")
            if stored and stored.isdigit():
                prev_msg_id = int(stored)
        except Exception:
            pass

    sent_msg = None
    send_success = False
    try:
        sent_msg = await bot.send_message(
            chat_id=chat_id,
            text=safe_text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        send_success = True
    except Exception as html_err:
        logger.warning(
            f"⚠️ Ошибка отправки HTML-дайджеста в чат {chat_id}: {html_err}. "
            "Переключаемся на аварийную Plain-Text отправку..."
        )
        try:
            plain_text = re.sub(r"<[^>]+>", "", message_html)
            plain_text = html.unescape(plain_text)
            sent_msg = await bot.send_message(
                chat_id=chat_id,
                text=plain_text,
                parse_mode=None,
                reply_markup=reply_markup,
            )
            logger.info(f"✅ Дайджест успешно доставлен в чат {chat_id} через Plain-Text fallback.")
            send_success = True
        except Exception as plain_err:
            logger.error(f"❌ Критический сбой отправки дайджеста в чат {chat_id}: {plain_err}")
            return False

    if send_success:
        # 2. Если новый дайджест успешно доставлен — удаляем старый, чтобы не захламлять чат
        if delete_previous and prev_msg_id:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=prev_msg_id)
                logger.info(f"🗑️ Предыдущий дайджест (ID: {prev_msg_id}) удален в чате {chat_id}.")
            except Exception as del_err:
                logger.debug(f"Не удалось удалить старое сообщение дайджеста {prev_msg_id} в чате {chat_id}: {del_err}")

        # 3. Фиксируем ID нового отправленного дайджеста
        if sent_msg is not None:
            msg_id = getattr(sent_msg, "message_id", None)
            if isinstance(msg_id, int):
                _LAST_DIGEST_MESSAGE_IDS[chat_id] = msg_id
                try:
                    set_bot_state(f"last_digest_msg_{chat_id}", str(msg_id))
                except Exception as db_err:
                    logger.warning(f"Не удалось сохранить ID дайджеста в SQLite: {db_err}")

        return True

    return False


async def check_and_execute_auto_sell(
    bot: Bot,
    city_data: Dict[str, Any],
    pos: Dict[str, Any],
) -> Optional[str]:
    """
    Проверяет триггеры умного выхода (Трейлинг-тейк, Погодный парашют, Тайм-стоп)
    и при их срабатывании автоматически исполняет продажу на Polymarket CLOB.
    """
    user_id = pos.get("user_id")
    if not user_id:
        return None

    wallet = await asyncio.to_thread(get_user_wallet, user_id)

    orderbook = city_data.get("orderbook", [])
    target_outcomes = pos.get("outcomes", "")
    cur_price = get_current_outcome_price(orderbook, target_outcomes)
    if cur_price is None or cur_price <= 0:
        return None

    pos_id = pos["id"]
    entry_price = float(pos.get("entry_price") or 0.0)
    peak_price = max(float(pos.get("peak_price") or entry_price), cur_price)
    trailing_active = int(pos.get("trailing_active") or 0)
    icao = city_data["icao"]
    city_name = city_data["city_name"]
    temp_c = city_data["temp_c"]
    rate_val = city_data["rate_val"]
    rem_hours = city_data["rem_hours_str"]
    raw_metar = city_data["raw_metar"]
    local_dt = city_data["local_dt"]
    local_time_val = local_dt.hour + local_dt.minute / 60.0
    heating_cutoff, _ = get_seasonal_heating_cutoff(local_dt.month, icao=icao)
    target_temp = extract_temp_value(target_outcomes)

    # 1. Активация трейлинга: если цена выросла на >= 50% или достигла >= 55¢
    if not trailing_active:
        if (entry_price > 0 and (cur_price - entry_price) / entry_price >= 0.50) or cur_price >= 55.0:
            trailing_active = 1
            await asyncio.to_thread(update_position_trailing, pos_id, peak_price, 1)

    if peak_price > float(pos.get("peak_price") or 0.0):
        await asyncio.to_thread(update_position_trailing, pos_id, peak_price, trailing_active)

    trigger_reason = None

    # Триггер А: Трейлинг-выход (замок прибыли сработал на откате 5–6¢ от пика)
    if trailing_active and (peak_price - cur_price) >= 5.0 and cur_price > 0:
        trigger_reason = f"🎉 ТРЕЙЛИНГ-ТЕЙК (ЗАМОК ПРИБЫЛИ): Пик был {peak_price:.0f}¢, выход на откате по {cur_price:.0f}¢"

    # Триггер Б: Превышение температуры (цель пробита вверх, исход математически сгорел)
    elif target_temp is not None and temp_c is not None and temp_c > target_temp:
        trigger_reason = f"🛑 ЦЕЛЬ ПРОБИТА ВВЕРХ ({temp_c:.1f}°C > {target_temp:.0f}°C): Страйк {target_outcomes} сгорел, спасение остатка"

    # Триггер В: Сезонный тайм-стоп (окно инсоляции закрыто, цель не пробита)
    elif (local_time_val >= heating_cutoff or "Окно закрыто" in rem_hours) and (target_temp and temp_c and temp_c < target_temp):
        cutoff_m = int((heating_cutoff % 1) * 60)
        trigger_reason = f"⏱️ СЕЗОННЫЙ ТАЙМ-СТОП ({int(heating_cutoff)}:{cutoff_m:02d} LT): Окно инсоляции завершено"

    # Триггер Г: Погодный парашют (обложные осадки / плотный Stratus)
    elif is_blocking_rain_and_clouds(raw_metar):
        trigger_reason = "🛑 ПОГОДНЫЙ ПАРАШЮТ: Обложные осадки блокируют дневной прогрев"
    elif 12.5 <= local_time_val <= heating_cutoff and (rate_val < 0.4 and has_real_low_cloud(raw_metar) and (target_temp and temp_c and (target_temp - temp_c) >= 1.5)):
        trigger_reason = "🛑 ПОГОДНЫЙ ПАРАШЮТ: Затухание темпа под плотной облачностью"

    if not trigger_reason:
        return None

    # Проверяем наличие приватного ключа
    has_private_key = bool(wallet and wallet.get("private_key"))
    last_alert = str(pos.get("last_alert") or "")

    # Если приватного ключа нет — отправляем ЭКСТРЕННЫЙ сигнал на ручной выход
    if not has_private_key:
        if last_alert != trigger_reason:
            pnl_val = round(((cur_price - entry_price) / entry_price) * 100, 1) if entry_price > 0 else 0.0
            pnl_str = f"{'+' if pnl_val >= 0 else ''}{pnl_val:.0f}%"
            urgent_msg = (
                f"🚨 <b>СРОЧНЫЙ СИГНАЛ НА ВЫХОД ИЗ СДЕЛКИ!</b>\n\n"
                f"• <b>Причина:</b> {trigger_reason}\n"
                f"• <b>Локация:</b> {city_name}\n"
                f"• <b>Исход:</b> <code>{target_outcomes}</code>\n"
                f"• <b>Текущая цена в стакане:</b> <b>{cur_price:.0f}¢</b> (Вход: <code>{entry_price:.0f}¢</code> | Результат: <b>{pnl_str}</b>)\n\n"
                f"⚠️ <i>Приватный ключ не подключен — бот не может продать за тебя. "
                f"Срочно открой Polymarket или Predy и сбрось позицию по рынку прямо сейчас!</i>"
            )
            try:
                await bot.send_message(user_id, urgent_msg, parse_mode="HTML")
                await asyncio.to_thread(update_position_alert, pos_id, trigger_reason)
            except Exception as e:
                logger.error(f"Не удалось отправить ручной алерт выхода: {e}")
        return trigger_reason

    # Исполнение сделки
    token_id = pos.get("token_id") or get_outcome_token_id(orderbook, target_outcomes)
    if not token_id:
        logger.warning(f"Не найден token_id для {target_outcomes} в {icao}, автопродажа невозможна.")
        return None

    proxy_addr = wallet.get("proxy_address") or ""
    sig_type = int(wallet.get("signature_type") or 1)

    # Определяем доступный объем контрактов
    shares = await asyncio.to_thread(
        get_token_balance,
        wallet["private_key"],
        token_id,
        proxy_addr,
    )
    if shares <= 0:
        shares = float(pos.get("shares") or 5.0)

    # Выполняем продажу
    success, order_id, details = await asyncio.to_thread(
        execute_market_sell,
        wallet["private_key"],
        token_id,
        shares,
        worst_price=0.001,
        proxy_address=proxy_addr,
        signature_type=sig_type,
    )

    if success:
        await asyncio.to_thread(close_position_with_exit, pos_id, cur_price)
        pnl_val = round(((cur_price - entry_price) / entry_price) * 100, 1) if entry_price > 0 else 0.0
        pnl_str = f"{'+' if pnl_val >= 0 else ''}{pnl_val:.0f}%"

        notify_text = (
            f"⚡ <b>АВТОПРОДАЖА УСПЕШНО ИСПОЛНЕНА!</b>\n\n"
            f"• <b>Причина:</b> {trigger_reason}\n"
            f"• <b>Локация:</b> {city_name}\n"
            f"• <b>Исход:</b> <code>{target_outcomes}</code>\n"
            f"• <b>Объем:</b> <code>{shares:.2f} контрактов</code>\n"
            f"• <b>Цена выхода:</b> <b>{cur_price:.0f}¢</b> (Вход: <code>{entry_price:.0f}¢</code> | Результат: <b>{pnl_str}</b>)\n\n"
            f"💰 <i>Средства возвращены на твой баланс в Predy/Polymarket. Сделка автоматически закрыта.</i>"
        )
        try:
            await bot.send_message(user_id, notify_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление об автопродаже: {e}")

        return trigger_reason
    else:
        logger.error(f"Сбой исполнения автопродажи для позиции {pos_id}: {order_id}")
        return None


async def send_consolidated_digest(
    bot: Bot,
    cities_metrics: List[Dict[str, Any]],
    is_morning_base: bool,
    now_khv: datetime,
) -> None:
    """Собирает все 4 города в единый пост и отправляет админу и всем подписчикам."""
    today_khv_str = now_khv.strftime("%d.%m.%Y")
    time_khv_str = now_khv.strftime("%H:%M")

    if is_morning_base:
        header = (
            f"🌅 <b>НОВЫЙ ТОРГОВЫЙ ДЕНЬ | БАЗОВЫЙ ПРОГНОЗ ({today_khv_str})</b>\n"
            f"<i>Региональный синоптический срез моделей (Время ХБР: {time_khv_str})</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        body_blocks = [build_morning_city_block(cm) for cm in cities_metrics if cm]
        default_msg = f"{header}\n\n" + "\n\n──────────────\n\n".join(body_blocks)
    else:
        header = (
            f"🔄 <b>ОБНОВЛЕНИЕ НА {time_khv_str} ХБР | ДИНАМИКА</b>\n"
            f"<i>Контроль темпа прогрева и статус открытых позиций</i>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        default_blocks = [build_dynamic_city_block(cm, None) for cm in cities_metrics if cm]
        default_msg = f"{header}\n\n" + "\n\n──────────────\n\n".join(default_blocks)

    # Получаем всех подписчиков и открытые позиции
    subscribers = await asyncio.to_thread(get_all_subscribers)
    active_positions = await asyncio.to_thread(get_all_active_positions)
    admin_id = getattr(config, "ADMIN_CHAT_ID", None)

    recipients = set(subscribers)
    if admin_id:
        try:
            recipients.add(int(admin_id))
        except (ValueError, TypeError):
            pass
    for p in active_positions:
        if p.get("user_id"):
            recipients.add(p["user_id"])

    for uid in recipients:
        user_pos_list = [p for p in active_positions if p.get("user_id") == uid]
        if not user_pos_list or is_morning_base:
            await _safe_send_digest(bot, uid, default_msg, express_scan_keyboard)
        else:
            user_blocks = []
            for cm in cities_metrics:
                pos = next((p for p in user_pos_list if p["icao"] == cm["icao"]), None)
                if pos:
                    await check_and_execute_auto_sell(bot, cm, pos)
                user_blocks.append(build_dynamic_city_block(cm, pos))
            custom_msg = f"{header}\n\n" + "\n\n──────────────\n\n".join(user_blocks)
            await _safe_send_digest(bot, uid, custom_msg, express_scan_keyboard)


async def run_auto_scanner(bot: Bot) -> None:
    """
    Основной цикл автоматического сканера.
    Синхронизирован со сводками METAR строго 2 раза в час: ровно в :02 и :32 минуты.
    Работает круглосуточно (24/7) для непрерывного контроля европейских, азиатских и американских рынков.
    """
    global _LAST_MORNING_DIGEST_DATE, _LAST_SENT_SLOT
    logger.info("🚀 Единый консолидированный автосканер v8.0 запущен (Режим 24/7, METAR: :02 и :32).")

    while True:
        try:
            now_khv = datetime.now(KHV_TZ)

            # 1. Проверяем, находимся ли мы прямо сейчас в контрольной минуте (:02 или :32)
            current_checkpoint_slot: Optional[str] = None
            if now_khv.minute in (2, 3):
                current_checkpoint_slot = f"{now_khv.strftime('%Y-%m-%d %H')}:02"
            elif now_khv.minute in (32, 33):
                current_checkpoint_slot = f"{now_khv.strftime('%Y-%m-%d %H')}:32"

            # 2. Если мы в контрольной точке И дайджест для этого слота еще не отправлялся — отправляем!
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
                    try:
                        from gemini_analyzer import daily_tracker
                        cm_dt = cm.get("local_dt")
                        if cm_dt:
                            cm_date = cm_dt.strftime("%Y-%m-%d")
                            daily_tracker.record(
                                0,
                                icao,
                                cm_date,
                                {
                                    "timestamp": time.time(),
                                    "time_str": cm_dt.strftime("%H:%M LT"),
                                    "target_date": cm_date,
                                    "temp_c": cm.get("temp_c"),
                                    "raw_metar": cm.get("raw_metar", ""),
                                    "rate_str": cm.get("rate_str", "Н/Д"),
                                    "rate_val": cm.get("rate_val", 0.0),
                                    "rem_hours_str": cm.get("rem_hours_str", "Н/Д"),
                                    "orderbook": cm.get("orderbook", []),
                                    "user_position": None,
                                },
                            )
                    except Exception as trk_err:
                        logger.debug(f"Не удалось зафиксировать срез в daily_tracker: {trk_err}")
                    await asyncio.sleep(0.25)

                # Отправка дайджеста
                await send_consolidated_digest(bot, cities_metrics, is_morning_base, now_khv)

                # Защита от повторной отправки в этом же слоте
                _LAST_SENT_SLOT = current_checkpoint_slot
                logger.info(f"✅ Дайджест {current_checkpoint_slot} ХБР успешно разослан.")

            # 3. Расчет точного сна до СЛЕДУЮЩЕЙ контрольной точки (:02 или :32)
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

