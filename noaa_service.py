import requests
import time
from typing import Dict, Any, Optional
from datetime import datetime, timezone

NOAA_METAR_URL = "https://aviationweather.gov/api/data/metar"
NOAA_TAF_URL = "https://aviationweather.gov/api/data/taf"
REQUEST_TIMEOUT = 10  # Жесткий таймаут на один запрос
MAX_RETRIES = 2       # Количество повторных попыток при сбоях (всего 3 попытки)
RETRY_DELAY = 1.0     # Пауза в секундах между попытками


def _format_utc_timestamp(timestamp_val: Optional[Any]) -> Optional[str]:
    """Вспомогательная функция для безопасного приведения меток времени к UTC ISO 8601."""
    if not timestamp_val:
        return None
    try:
        if isinstance(timestamp_val, (int, float)):
            dt = datetime.fromtimestamp(timestamp_val, tz=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif isinstance(timestamp_val, str):
            clean_str = timestamp_val.strip()
            if clean_str.endswith("Z"):
                return clean_str
            dt = datetime.fromisoformat(clean_str.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(timestamp_val)
    return None


def _fetch_with_retry(url: str) -> tuple[Optional[requests.Response], Optional[str]]:
    """
    Вспомогательная функция выполняет HTTP GET запрос с retry-механикой
    для временных Timeout и HTTP 5xx ошибок.
    """
    last_error = None
    for attempt in range(1 + MAX_RETRIES):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            
            # Если 5xx серверная ошибка — пробуем еще раз
            if 500 <= response.status_code < 600:
                last_error = f"NOAA HTTP Error {response.status_code}"
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY)
                    continue
            
            return response, None

        except requests.exceptions.Timeout:
            last_error = "NOAA Request Timeout (10s limit exceeded)"
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
                continue
        except Exception as e:
            return None, f"Unexpected error: {str(e)}"

    return None, last_error


def get_metar(icao_code: str) -> Dict[str, Any]:
    """
    Запрашивает METAR данные с сервера NOAA для указанного ICAO кода.
    Использует retry-механику при сбоях.
    """
    url = f"{NOAA_METAR_URL}?ids={icao_code}&format=json"
    response, error = _fetch_with_retry(url)

    if error:
        return {
            "available": False,
            "error": error,
            "raw": None,
            "observation_time_utc": None,
            "temp_c": None,
            "dewpoint_c": None,
            "wind_dir_degrees": None,
            "wind_speed_kts": None
        }

    if response is None:
        return {
            "available": False,
            "error": "No response from NOAA",
            "raw": None,
            "observation_time_utc": None,
            "temp_c": None,
            "dewpoint_c": None,
            "wind_dir_degrees": None,
            "wind_speed_kts": None
        }

    # HTTP 204 означает, что сервер штатно обработал запрос, но данных по ICAO нет
    if response.status_code == 204:
        return {
            "available": False,
            "error": "No METAR data found for given ICAO",
            "raw": None,
            "observation_time_utc": None,
            "temp_c": None,
            "dewpoint_c": None,
            "wind_dir_degrees": None,
            "wind_speed_kts": None
        }

    if response.status_code != 200:
        return {
            "available": False,
            "error": f"NOAA HTTP Error {response.status_code}",
            "raw": None,
            "observation_time_utc": None,
            "temp_c": None,
            "dewpoint_c": None,
            "wind_dir_degrees": None,
            "wind_speed_kts": None
        }

    data = response.json()
    if not data or not isinstance(data, list):
        return {
            "available": False,
            "error": "No METAR data found for given ICAO",
            "raw": None,
            "observation_time_utc": None,
            "temp_c": None,
            "dewpoint_c": None,
            "wind_dir_degrees": None,
            "wind_speed_kts": None
        }

    metar_item = data[0]
    
    # Приоритет: obsTime (точный момент наблюдения) -> reportTime (fallback)
    obs_time_raw = metar_item.get("obsTime") or metar_item.get("reportTime")
    obs_time_utc = _format_utc_timestamp(obs_time_raw)

    return {
        "available": True,
        "error": None,
        "raw": metar_item.get("rawOb", ""),
        "observation_time_utc": obs_time_utc,
        "temp_c": metar_item.get("temp"),
        "dewpoint_c": metar_item.get("dewp"),
        "wind_dir_degrees": metar_item.get("wdir"),
        "wind_speed_kts": metar_item.get("wspd")
    }


def get_taf(icao_code: str) -> Dict[str, Any]:
    """
    Запрашивает RAW TAF и базовые временные метки с сервера NOAA.
    Использует retry-механику при сбоях.
    """
    url = f"{NOAA_TAF_URL}?ids={icao_code}&format=json"
    response, error = _fetch_with_retry(url)

    if error:
        return {
            "available": False,
            "error": error,
            "raw": None,
            "issue_time_utc": None,
            "valid_from_utc": None,
            "valid_to_utc": None
        }

    if response is None:
        return {
            "available": False,
            "error": "No response from NOAA",
            "raw": None,
            "issue_time_utc": None,
            "valid_from_utc": None,
            "valid_to_utc": None
        }

    # HTTP 204 означает, что TAF для данного ICAO отсутствует
    if response.status_code == 204:
        return {
            "available": False,
            "error": "No TAF data found for given ICAO",
            "raw": None,
            "issue_time_utc": None,
            "valid_from_utc": None,
            "valid_to_utc": None
        }

    if response.status_code != 200:
        return {
            "available": False,
            "error": f"NOAA HTTP Error {response.status_code}",
            "raw": None,
            "issue_time_utc": None,
            "valid_from_utc": None,
            "valid_to_utc": None
        }

    data = response.json()
    if not data or not isinstance(data, list):
        return {
            "available": False,
            "error": "No TAF data found for given ICAO",
            "raw": None,
            "issue_time_utc": None,
            "valid_from_utc": None,
            "valid_to_utc": None
        }

    taf_item = data[0]

    return {
        "available": True,
        "error": None,
        "raw": taf_item.get("rawTAF", ""),
        "issue_time_utc": _format_utc_timestamp(taf_item.get("issueTime")),
        "valid_from_utc": _format_utc_timestamp(taf_item.get("validTimeFrom")),
        "valid_to_utc": _format_utc_timestamp(taf_item.get("validTimeTo"))
    }


def get_noaa_package(icao_code: str) -> Dict[str, Any]:
    """Объединенная функция для получения полного пакета METAR + TAF по ICAO."""
    cleaned_icao = icao_code.strip().upper() if icao_code else ""
    return {
        "icao": cleaned_icao,
        "metar": get_metar(cleaned_icao),
        "taf": get_taf(cleaned_icao)
    }


if __name__ == "__main__":
    import json
    
    test_codes = ["KJFK", "UHHH", "XXXX"]
    print("=== ЗАПУСК ТЕСТОВ NOAA SERVICE (С RETRY) ===\n")
    
    for code in test_codes:
        result = get_noaa_package(code)
        print(f"--- Результат для {code} ---")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print("\n" + "="*50 + "\n")