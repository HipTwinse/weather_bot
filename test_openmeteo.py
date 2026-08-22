"""
Тестовый скрипт для запуска физических сетевых запросов к Open-Meteo API.
Выполняет проверку аэропортов UHHH (Хабаровск) и KJFK (Нью-Йорк).
Строго соблюдает действующий контракт openmeteo_service.py и airport_resolver.py.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from airport_resolver import resolve_airport
from openmeteo_service import fetch_openmeteo_forecast


def print_model_summary(model_key: str, model_payload: dict):
    """
    Выводит структурированную сводку по модели прогноза погоды
    в полном соответствии с действующим контрактом openmeteo_service.py.
    """
    info = model_payload.get("model_info", {})
    status = model_payload.get("status", {})
    prov = model_payload.get("data_provenance", {})
    derived = model_payload.get("derived_metrics", {})

    print(f"\n  🔹 [{info.get('name', model_key)}] (ID: {info.get('id')})")
    print(
        f"     • Status: Available={status.get('available')} | "
        f"HTTP {status.get('http_status')} | "
        f"Records={len(model_payload.get('source_hourly', []))}"
    )
    print(f"     • Warnings: {status.get('warnings') if status.get('warnings') else 'None'}")
    print(f"     • Error: {status.get('error')}")
    print(
        f"     • Resolution & Provenance: Native={prov.get('native_temporal_resolution_seconds')}s | "
        f"API={info.get('api_temporal_resolution')} | "
        f"Interpolated={prov.get('temporally_interpolated')}"
    )
    print(
        f"     • Derived: MaxTemp={derived.get('max_temp_c')}°C at {derived.get('peak_hour_local')} | "
        f"Precip={derived.get('total_precip_mm')}mm"
    )


@pytest.mark.parametrize("icao", ["UHHH", "KJFK"])
def test_airport(icao: str):
    """
    Интеграционный live-тест физического сетевого запроса к Open-Meteo.
    Проверяет успешность резолвинга аэропорта и корректность структуры ответа API.
    """
    print(f"\n=======================================================")
    print(f"  RUNNING LIVE TEST FOR AIRPORT: {icao}")
    print(f"=======================================================")

    # 1. Проверка работы airport_resolver
    airport = resolve_airport(icao)
    if airport is None:
        pytest.fail(f"❌ Airport {icao} not found in airport_resolver!")

    iana_tz = airport["timezone"]
    lat = airport["lat"]
    lon = airport["lon"]
    city = airport["city"]
    country = airport["country"]

    target_date = datetime.now(ZoneInfo(iana_tz)).strftime("%Y-%m-%d")

    print(f"📍 City: {city} ({country})")
    print(f"🌐 Coordinates: Lat {lat}, Lon {lon}")
    print(f"⏰ Local Timezone: {iana_tz} | Target Local Date: {target_date}")
    print("📡 Sending live HTTP requests to Open-Meteo...")

    # 2. Выполнение реального сетевого запроса
    result = fetch_openmeteo_forecast(
        latitude=lat,
        longitude=lon,
        iana_timezone=iana_tz,
        target_date_local=target_date
    )

    # 3. Валидация базового контракта верхнего уровня
    assert "primary_models" in result, "Отсутствует ключ 'primary_models' в ответе API"
    assert "secondary_models" in result, "Отсутствует ключ 'secondary_models' в ответе API"
    assert result.get("target_date_local") == target_date, "Несовпадение target_date_local в ответе"

    print(f"\n✅ Forecast successfully retrieved for target date: {result['target_date_local']}")

    print("\n--- PRIMARY MODELS ---")
    for key, payload in result.get("primary_models", {}).items():
        print_model_summary(key, payload)

    print("\n--- SECONDARY MODELS ---")
    for key, payload in result.get("secondary_models", {}).items():
        print_model_summary(key, payload)

    # 4. Проверка и дамп образца данных флагманской модели ECMWF
    ecmwf_model = result.get("primary_models", {}).get("ecmwf_hres")
    assert ecmwf_model is not None, "Модель ecmwf_hres отсутствует в primary_models"
    assert ecmwf_model["status"]["available"] is True, f"Модель ECMWF недоступна: {ecmwf_model['status'].get('error')}"
    
    hourly_records = ecmwf_model.get("source_hourly", [])
    assert len(hourly_records) > 0, "Массив source_hourly для ECMWF пуст"

    print("\n🔎 First Hour Sample Record (ECMWF):")
    print(json.dumps(hourly_records[0], indent=4, ensure_ascii=False))


if __name__ == "__main__":
    # Сохраняем возможность прямого ручного запуска через `python test_openmeteo.py`
    for test_icao in ["UHHH", "KJFK"]:
        test_airport(test_icao)