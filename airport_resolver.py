import airportsdata
from typing import Dict, Any, Optional

# Загружаем офлайн-базу аэропортов по ICAO кодам один раз при старте модуля
_AIRPORTS = airportsdata.load('ICAO')

FEET_TO_METERS = 0.3048

def resolve_airport(icao_code: str) -> Optional[Dict[str, Any]]:
    """
    Принимает ICAO-код аэропорта, валидирует его и возвращает расширенные метаданные:
    - icao
    - iata
    - name
    - city
    - country
    - lat
    - lon
    - elevation_m
    - timezone
    
    Если код не найден или неверен, возвращает None.
    """
    if not icao_code or not isinstance(icao_code, str):
        return None
    
    # Приводим к верхнему регистру и убираем лишние пробелы
    cleaned_icao = icao_code.strip().upper()
    
    # Проверяем базовый формат ICAO (4 символа)
    if len(cleaned_icao) != 4 or not cleaned_icao.isalpha():
        return None
    
    # Ищем аэропорт в базе
    airport = _AIRPORTS.get(cleaned_icao)
    if not airport:
        return None
    
    # Расчет высоты над уровнем моря в метрах (перевод из футов)
    elevation_ft = airport.get("elevation")
    elevation_m = round(float(elevation_ft) * FEET_TO_METERS, 1) if elevation_ft is not None else None

    # Собираем и структурируем проверенные данные
    return {
        "icao": cleaned_icao,
        "iata": airport.get("iata", ""),
        "name": airport.get("name", "Unknown Airport"),
        "city": airport.get("city", "Unknown City"),
        "country": airport.get("country", "Unknown Country"),
        "lat": round(float(airport["lat"]), 4),
        "lon": round(float(airport["lon"]), 4),
        "elevation_m": elevation_m,
        "timezone": airport.get("tz", "UTC")
    }

if __name__ == "__main__":
    test_codes = ["KJFK", "UHHH", "XXXX"]
    
    print("=== ЗАПУСК ТЕСТОВ AIRPORT RESOLVER ===\n")
    for code in test_codes:
        result = resolve_airport(code)
        print(f"Результат для '{code}':")
        print(result)
        print("-" * 50)