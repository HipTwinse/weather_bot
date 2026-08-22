"""
Тестовый скрипт проверки синтеза и консенсус-анализа.
"""

import json
from openmeteo_service import fetch_openmeteo_forecast
from weather_synthesizer import synthesize_forecast

def run_synthesis_test(airport_code: str, lat: float, lon: float, tz: str, date_str: str):
    print(f"\n=======================================================")
    print(f" 🧪 SYNTHESIS TEST FOR AIRPORT: {airport_code}")
    print(f"=======================================================")
    
    # 1. Запрашиваем данные
    raw_data = fetch_openmeteo_forecast(lat, lon, tz, date_str)
    
    # 2. Синтезируем сводку
    synthesized = synthesize_forecast(raw_data)
    
    # 3. Выводим красивый отчёт
    print(json.dumps(synthesized, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    # Хабаровск (UHHH)
    run_synthesis_test("UHHH", 48.5280, 135.1880, "Asia/Vladivostok", "2026-08-11")
    
    # Нью-Йорк (KJFK)
    run_synthesis_test("KJFK", 40.6399, -73.7787, "America/New_York", "2026-08-11")