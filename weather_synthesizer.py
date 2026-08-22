"""
Модуль синтеза и форматирования погодных данных (Weather Data Package).
Сохраняет 100% исходных данных без усечения для последующего ИИ-анализа.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def synthesize_forecast(raw_forecast_payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Принимает сырой payload от openmeteo_service.py и возвращает
    структурированный консенсус-прогноз с аналитикой.
    """
    primary_models = raw_forecast_payload.get("primary_models", {})
    secondary_models = raw_forecast_payload.get("secondary_models", {})

    active_primary = {
        k: v for k, v in primary_models.items()
        if v.get("status", {}).get("available") and v.get("derived_metrics")
    }

    active_secondary = {
        k: v for k, v in secondary_models.items()
        if v.get("status", {}).get("available") and v.get("derived_metrics")
    }

    if not active_primary:
        return {
            "success": False,
            "error": "Ни одна из основных метеомоделей (Primary) недоступна.",
        }

    # Температурная аналитика по Primary моделям
    max_temps = [
        m["derived_metrics"]["max_temp_c"]
        for m in active_primary.values()
        if m["derived_metrics"]["max_temp_c"] is not None
    ]
    min_temps = [
        m["derived_metrics"]["min_temp_c"]
        for m in active_primary.values()
        if m["derived_metrics"]["min_temp_c"] is not None
    ]

    avg_max_temp = round(sum(max_temps) / len(max_temps), 1) if max_temps else None
    avg_min_temp = round(sum(min_temps) / len(min_temps), 1) if min_temps else None
    temp_spread = round(max(max_temps) - min(max_temps), 1) if len(max_temps) > 1 else 0.0

    # Анализ осадков по всем доступным моделям (Primary + Secondary GEM)
    all_active_models = {**active_primary, **active_secondary}
    precip_totals = {
        k: v["derived_metrics"]["total_precip_mm"]
        for k, v in all_active_models.items()
        if v["derived_metrics"]["total_precip_mm"] is not None
    }

    all_precip_values = list(precip_totals.values())
    avg_precip = round(sum(all_precip_values) / len(all_precip_values), 1) if all_precip_values else 0.0
    max_precip = max(all_precip_values) if all_precip_values else 0.0
    min_precip = min(all_precip_values) if all_precip_values else 0.0
    precip_spread = round(max_precip - min_precip, 1) if all_precip_values else 0.0

    models_with_rain = sum(1 for p in all_precip_values if p >= 0.5)
    total_models_count = len(all_precip_values)
    agreement_percent = int((models_with_rain / total_models_count) * 100) if total_models_count > 0 else 0

    if models_with_rain == 0:
        precip_consensus = "DRY"
    elif models_with_rain == total_models_count:
        precip_consensus = "RAIN"
    else:
        precip_consensus = "MIXED"

    # Пиковые порывы ветра (Primary)
    max_wind_gusts = [
        m["derived_metrics"]["max_wind_gust_ms"]
        for m in active_primary.values()
        if m["derived_metrics"]["max_wind_gust_ms"] is not None
    ]
    peak_gust = max(max_wind_gusts) if max_wind_gusts else 0.0

    # Оценка уровня уверенности
    confidence_level = "HIGH"
    confidence_reasons = []

    TEMP_SPREAD_HIGH_CONFLICT = 5.0
    TEMP_SPREAD_MEDIUM_CONFLICT = 2.5
    PRECIP_SPREAD_HIGH_CONFLICT = 15.0
    PRECIP_SPREAD_MEDIUM_CONFLICT = 5.0

    if precip_spread >= PRECIP_SPREAD_HIGH_CONFLICT:
        confidence_level = "LOW"
        confidence_reasons.append(
            f"Сильный разброс прогнозируемых осадков между моделями: {min_precip}–{max_precip} мм (spread {precip_spread} мм)"
        )
    elif 25 <= agreement_percent <= 75 and max_precip >= 5.0:
        confidence_level = "LOW"
        confidence_reasons.append(
            f"Конфликт моделей по факту выпадения осадков: только {agreement_percent}% моделей прогнозируют осадки >= 0.5 мм"
        )
    elif precip_spread >= PRECIP_SPREAD_MEDIUM_CONFLICT:
        confidence_level = "MEDIUM"
        confidence_reasons.append(
            f"Заметный разброс прогнозируемых осадков между моделями: {min_precip}–{max_precip} мм (spread {precip_spread} мм)"
        )

    if temp_spread >= TEMP_SPREAD_HIGH_CONFLICT:
        confidence_level = "LOW"
        confidence_reasons.append(f"Критический разброс пиковой температуры между Primary моделями: {temp_spread}°C")
    elif temp_spread >= TEMP_SPREAD_MEDIUM_CONFLICT and confidence_level != "LOW":
        confidence_level = "MEDIUM"
        confidence_reasons.append(f"Заметный разброс пиковой температуры между Primary моделями: {temp_spread}°C")

    if not confidence_reasons:
        confidence_reasons.append("Все метеомодели демонстрируют высокий уровень согласия")

    if avg_min_temp is not None and avg_max_temp is not None:
        temp_range_str = f"{avg_min_temp} ... {avg_max_temp}"
    else:
        temp_range_str = "Н/Д"

    return {
        "success": True,
        "target_date_local": raw_forecast_payload.get("target_date_local"),
        "timezone": raw_forecast_payload.get("timezone"),
        "consensus": {
            "temp_max_avg_c": avg_max_temp,
            "temp_min_avg_c": avg_min_temp,
            "temp_range_c": temp_range_str,
            "temp_spread_c": temp_spread,
            "precipitation_avg_mm": avg_precip,
            "precipitation_min_mm": min_precip,
            "precipitation_max_mm": max_precip,
            "precipitation_spread_mm": precip_spread,
            "precipitation_model_agreement_percent": agreement_percent,
            "precipitation_consensus": precip_consensus,
            "max_wind_gust_ms": peak_gust,
        },
        "confidence": {
            "level": confidence_level,
            "reasons": confidence_reasons,
        },
        "model_breakdown": {
            "primary_active_count": len(active_primary),
            "secondary_active_count": len(active_secondary),
            "individual_precip_mm": precip_totals,
        },
    }


def build_raw_data_package_dict(
    airport_data: Dict[str, Any],
    raw_forecast_payload: Dict[str, Any],
    synthesis_result: Dict[str, Any],
    noaa_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Формирует полный авторитарный JSON-пакет данных (RAW DATA PACKAGE).
    Включает 100% данных Open-Meteo, NOAA и метаданных без усечений.
    """
    return {
        "package_metadata": {
            "icao": airport_data.get("icao"),
            "iata": airport_data.get("iata"),
            "name": airport_data.get("name"),
            "city": airport_data.get("city"),
            "country": airport_data.get("country"),
            "coordinates": {
                "latitude": airport_data.get("lat"),
                "longitude": airport_data.get("lon"),
            },
            "elevation_m": airport_data.get("elevation_m"),
            "timezone": airport_data.get("timezone"),
            "target_date_local": raw_forecast_payload.get("target_date_local"),
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "synthesis_and_consensus": synthesis_result,
        "aviation_weather": {
            "metar": noaa_payload.get("metar", {}),
            "taf": noaa_payload.get("taf", {}),
        },
        "models_raw_data": {
            "target_date_local": raw_forecast_payload.get("target_date_local"),
            "timezone": raw_forecast_payload.get("timezone"),
            "expected_records_count": raw_forecast_payload.get("expected_records_count"),
            "primary_models": raw_forecast_payload.get("primary_models", {}),
            "secondary_models": raw_forecast_payload.get("secondary_models", {}),
        },
    }


def build_summary_caption(
    airport_data: Dict[str, Any],
    synthesis_result: Dict[str, Any],
    noaa_payload: Dict[str, Any],
    target_date_local: str,
) -> str:
    """
    Формирует компактное человекочитаемое Telegram-сообщение (Summary/Index).
    """
    name = airport_data.get("name", "Unknown Airport")
    icao = airport_data.get("icao", "----")
    city = airport_data.get("city", "Unknown City")
    country = airport_data.get("country", "")
    tz = airport_data.get("timezone", "UTC")

    lines = [
        f"📍 <b>Погодный отчёт: {name} (ICAO: <code>{icao}</code>)</b>",
        f"🌍 <i>{city}, {country} | Часовой пояс: {tz} | Дата: {target_date_local}</i>\n",
    ]

    if synthesis_result.get("success"):
        c = synthesis_result.get("consensus", {})
        conf = synthesis_result.get("confidence", {})
        conf_level = conf.get("level", "MEDIUM")
        conf_emoji = "🟢" if conf_level == "HIGH" else ("🟡" if conf_level == "MEDIUM" else "🔴")

        lines.append("📊 <b>Синтез метеомоделей (ECMWF, GFS, ICON, GEM):</b>")
        lines.append(f"• Диапазон температур: <b>{c.get('temp_range_c', 'Н/Д')} °C</b>")
        lines.append(f"• Осадки (среднее): <b>{c.get('precipitation_avg_mm', 0.0)} мм</b> ({c.get('precipitation_consensus', 'N/A')})")
        lines.append(f"• Согласие моделей: <b>{c.get('precipitation_model_agreement_percent', 0)}%</b>")
        lines.append(f"• Пиковый порыв ветра: <b>{c.get('max_wind_gust_ms', 0.0)} м/с</b>")
        lines.append(f"• Уровень уверенности: {conf_emoji} <b>{conf_level}</b>")

        reasons = conf.get("reasons", [])
        if reasons:
            lines.append(f"  └ <i>Анализ:</i> {reasons[0]}")
        lines.append("")
    else:
        err = synthesis_result.get("error", "Неизвестная ошибка")
        lines.append(f"⚠️ <i>Синтез моделей недоступен: {err}</i>\n")

    metar = noaa_payload.get("metar", {})
    if metar.get("available") and metar.get("raw"):
        lines.append("✈️ <b>Авиационная сводка (METAR):</b>")
        lines.append(f"<code>{metar['raw']}</code>\n")

    taf = noaa_payload.get("taf", {})
    if taf.get("available") and taf.get("raw"):
        lines.append("📝 <b>Прогноз по аэродрому (TAF):</b>")
        lines.append(f"<code>{taf['raw']}</code>\n")

    lines.append("📦 <i>Полный 24ч RAW DATA PACKAGE по всем 4 моделям прикреплен файлом ниже для Gemini Analyzer.</i>")

    return "\n".join(lines)