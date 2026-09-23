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
    """Возвращает актуальную синоптическую заметку на базе правил KB v7.1."""
    if icao == "EGLC":
        is_sw = ("2" in raw_metar[:10] or "SW" in raw_metar) and "KT" in raw_metar
        match_kt = re.search(r"(\d{2})KT", raw_metar)
        wind_kt = int(match_kt.group(1)) if match_kt else 0
        if is_sw and wind_kt >= 28:
            return "Шквалистый SW (>=28 kt): UHI пробивает Доклендс транзитом (+1.2...+1.6°C к моделям, верхняя планка GFS)."
        elif is_sw and wind_kt >= 10:
            return "SW-ветер несет городской остров тепла Доклендса (+0.8...+1.2°C к европейским моделям)."
        return "Лондон: ориентир на модель GFS при окнах солнца между осадками."

    elif icao == "LFPB":
        return "Париж: GFS в приоритете для сухого радиационного прогрева Иль-де-Франс."

    elif icao == "LIMC":
        utc_hour = local_dt.astimezone(zoneinfo.ZoneInfo("UTC")).hour
        if 4 <= utc_hour <= 6 and temp_c is not None and temp_c >= 18.0:
            return "Милан: утренний METAR >=18°C! Нижние страйки блокируются, фокус на верхний консенсус."
        return "Милан: термический купол долины реки По (приоритет ICON/GEM при штиле)."

    elif icao == "LEMD":
        return "Мадрид: плато Месета (>600м), дневной ветер срывает перегрев (опора на медиану ECMWF/GFS)."

    return "Синтез физических моделей."


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


def _get_priority_target(icao: str, models: Dict[str, float], avg_peak: float) -> Tuple[float, str]:
    """Определяет приоритетный ориентир температуры по правилам KB v7.1."""
    if icao in ["EGLC", "LFPB"]:
        gfs_val = models.get("gfs_global")
        if gfs_val is not None:
            return gfs_val, f"GFS ({gfs_val:.1f}°C)"
        return avg_peak, f"Консенсус ({avg_peak:.1f}°C)"
    elif icao == "LIMC":
        icon_val = models.get("icon_global")
        if icon_val is not None:
            return icon_val, f"ICON ({icon_val:.1f}°C)"
        return avg_peak, f"Консенсус ({avg_peak:.1f}°C)"
    elif icao == "EDDM":
        return avg_peak + 1.2, f"Альпийский фён ({avg_peak+1.2:.1f}°C)"
    else:
        return avg_peak, f"Медиана ({avg_peak:.1f}°C)"


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

    target_val, priority_name = _get_priority_target(icao, models, avg_peak)

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

        target_val, priority_name = _get_priority_target(icao, models, avg_peak)
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

                # Параллельный сбор метрик по всем 4 городам
                metrics_tasks = [collect_city_metrics(icao) for icao in TARGET_CITIES.keys()]
                cities_metrics = await asyncio.gather(*metrics_tasks, return_exceptions=False)

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

