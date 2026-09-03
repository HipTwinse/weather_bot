"""
Модуль сбора, валидации и агрегации метеоданных (Phase 3 Engine).

Отвечает за:
1. Взаимодействие с Open-Meteo API с защитой от сбоев (Rate Limiting, Retries, Retry-After delta-seconds / HTTP-date).
2. Запрос полных 12 физических параметров атмосферы и валидацию длин массивов.
3. Безопасный парсинг метаданных прогонов моделей из Model Updates API.
4. Точный расчет ожидаемой длительности суток с учетом DST (перехода часов).
5. Строгую фильтрацию по целевой локальной дате (target_date_local).
6. Семантически корректный расчет агрегированных производных метрик (derived metrics).
7. Формирование обогащенного контракта (model_info, data_provenance, status).

ВАЖНО: Модуль не содержит торговой логики, Telegram-ботов или расчетов EV.
"""

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import zoneinfo
import requests

# Полный список 12 запрашиваемых почасовых параметров
HOURLY_VARIABLES = [
    "temperature_2m",
    "precipitation",
    "wind_speed_10m",
    "wind_gusts_10m",
    "dew_point_2m",
    "relative_humidity_2m",
    "cloud_cover",
    "cloud_cover_low",
    "cloud_cover_mid",
    "cloud_cover_high",
    "wind_direction_10m",
    "shortwave_radiation"
]

# Выверенная конфигурация поддерживаемых метеомоделей с точными метаданными
MODEL_CONFIGS = {
    "ecmwf_hres": {
        "api_param": "ecmwf_ifs025",
        "metadata_key": "ecmwfifs025",
        "is_primary": True,
        "model_info": {
            "id": "ecmwf_ifs025",
            "name": "ECMWF IFS 0.25°",
            "spatial_resolution_km": 25.0,
            "native_temporal_resolution_seconds": 3600,
            "api_temporal_resolution": "hourly"
        },
        "data_provenance": {
            "native_timestep_seconds": 3600,
            "api_timestep_seconds": 3600,
            "temporally_interpolated": False
        }
    },
    "gfs_global": {
        "api_param": "gfs_seamless",
        "metadata_key": "gfsseamless",
        "is_primary": True,
        "model_info": {
            "id": "gfs_seamless",
            "name": "GFS Global Seamless",
            "spatial_resolution_km": 28.0,
            "native_temporal_resolution_seconds": 3600,
            "api_temporal_resolution": "hourly"
        },
        "data_provenance": {
            "native_timestep_seconds": 3600,
            "api_timestep_seconds": 3600,
            "temporally_interpolated": False
        }
    },
    "icon_global": {
        "api_param": "icon_seamless",
        "metadata_key": "iconseamless",
        "is_primary": True,
        "model_info": {
            "id": "icon_seamless",
            "name": "DWD ICON Global",
            "spatial_resolution_km": 13.0,
            "native_temporal_resolution_seconds": 3600,
            "api_temporal_resolution": "hourly"
        },
        "data_provenance": {
            "native_timestep_seconds": 3600,
            "api_timestep_seconds": 3600,
            "temporally_interpolated": False
        }
    },
    "gem_global": {
        "api_param": "gem_global",
        "metadata_key": "gemglobal",
        "is_primary": False,
        "model_info": {
            "id": "gem_global",
            "name": "CMC GEM Global",
            "spatial_resolution_km": 15.0,
            "native_temporal_resolution_seconds": 10800,  # 3 hours native
            "api_temporal_resolution": "hourly"
        },
        "data_provenance": {
            "native_timestep_seconds": 10800,
            "api_timestep_seconds": 3600,
            "temporally_interpolated": True  # Open-Meteo интерполирует с 3h до 1h
        }
    }
}

# Заголовок для предотвращения сброса соединения со стороны Cloudflare WAF
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json"
}


def _parse_retry_after(retry_after_header: str | None) -> float | None:
    """
    Безопасно разбирает заголовок Retry-After.
    Поддерживает:
    1. Delta-seconds (например: "120")
    2. HTTP-date / RFC 1123 (например: "Wed, 12 Aug 2026 17:40:00 GMT")
    
    Возвращает несекундную задержку >= 0.0 или None при невалидном значении.
    """
    if not retry_after_header:
        return None

    # Попытка 1: Delta-seconds
    try:
        val = float(retry_after_header)
        return max(0.0, val)
    except (ValueError, TypeError):
        pass

    # Попытка 2: HTTP-date (RFC 1123)
    try:
        target_dt = parsedate_to_datetime(retry_after_header)
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        delay = (target_dt - now_dt).total_seconds()
        return max(0.0, delay)
    except Exception:
        return None


def _http_get_with_retry(url: str, params: dict, retries: int = 3, timeout: int = 10) -> requests.Response | None:
    """
    Безопасный HTTP-запрос с обработкой Retry-After и экспоненциальной задержкой.

    Правила retry:
    - HTTP 429: проверяется заголовок Retry-After (delta-seconds или HTTP-date).
      Если валиден, используется он, иначе применяется exponential backoff.
    - HTTP 500, 502, 503, 504: exponential backoff (1s -> 2s -> 4s).
    - HTTP 400, 401, 403, 404: мгновенный возврат (без retry).
    - Таймауты и сетевые ошибки (ConnectionError, Timeout): exponential backoff.
    """
    retryable_statuses = {500, 502, 503, 504}
    backoff_delay = 1.0

    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
            
            # Успешный ответ
            if response.status_code == 200:
                return response

            # Обработка Rate Limit (HTTP 429)
            if response.status_code == 429:
                if attempt < retries:
                    retry_after = response.headers.get("Retry-After")
                    parsed_delay = _parse_retry_after(retry_after)
                    delay_to_use = parsed_delay if parsed_delay is not None else backoff_delay
                    
                    time.sleep(delay_to_use)
                    backoff_delay *= 2.0
                    continue
                return response

            # Временные ошибки сервера (50x)
            if response.status_code in retryable_statuses:
                if attempt < retries:
                    time.sleep(backoff_delay)
                    backoff_delay *= 2.0
                    continue
                return response

            # Невосстановимые клиентские ошибки (400, 401, 403, 404 и др.)
            return response

        except (requests.Timeout, requests.ConnectionError, requests.RequestException):
            if attempt < retries:
                time.sleep(backoff_delay)
                backoff_delay *= 2.0
                continue
            return None

    return None


def _safe_convert_timestamp(val) -> str | None:
    """
    Безопасная конвертация Unix timestamp (int/float) в ISO 8601 UTC строку.
    Возвращает None при невалидных типах или ошибках.
    """
    if val is None:
        return None
    try:
        ts = float(val)
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def fetch_model_updates_metadata() -> dict:
    """
    Запрашивает метаданные прогонов моделей из публичного Open-Meteo Model Updates API.
    
    Реализует гибридный полиморфный парсер:
    - Использует эндпоинт customer-model-updates.open-meteo.com.
    - Извлекает данные по точно сопоставленным metadata_key без подчеркиваний.
    - Конвертирует Unix timestamp в ISO 8601 UTC.
    - При отсутствии данных, ошибке API или сети возвращает None для соответствующих полей.
    - Ни при каких обстоятельствах не блокирует и не объявляет модели unavailable.
    """
    url = "https://customer-model-updates.open-meteo.com/v1/model-updates"
    resp = _http_get_with_retry(url, params={}, retries=1, timeout=5)
    
    default_meta = {
        model_key: {
            "model_run_utc": None,
            "model_modified_utc": None,
            "model_available_utc": None,
            "temporal_resolution_seconds": None,
            "update_interval_seconds": None
        }
        for model_key in MODEL_CONFIGS
    }

    if not resp or resp.status_code != 200:
        return default_meta

    try:
        data = resp.json()
        if not isinstance(data, dict):
            return default_meta

        for model_key, cfg in MODEL_CONFIGS.items():
            meta_key = cfg.get("metadata_key", cfg["api_param"])
            m_data = data.get(meta_key) or data.get(cfg["api_param"]) or data.get(model_key)
            
            if isinstance(m_data, dict):
                def_res = cfg["data_provenance"]["native_timestep_seconds"]
                parsed_res = m_data.get("temporal_resolution_seconds")
                parsed_interval = m_data.get("update_interval_seconds")
                
                default_meta[model_key] = {
                    "model_run_utc": _safe_convert_timestamp(m_data.get("last_run_initialisation_time")),
                    "model_modified_utc": _safe_convert_timestamp(m_data.get("last_run_modification_time")),
                    "model_available_utc": _safe_convert_timestamp(m_data.get("last_run_availability_time")),
                    "temporal_resolution_seconds": int(parsed_res) if isinstance(parsed_res, (int, float)) else def_res,
                    "update_interval_seconds": int(parsed_interval) if isinstance(parsed_interval, (int, float)) else None
                }
        return default_meta
    except Exception:
        return default_meta


def _get_expected_local_hours(iana_timezone: str, target_date_local: str) -> int:
    """
    Вычисляет точное количество часов в локальных сутках с учетом перехода DST.
    Возвращает 24 для обычного дня, 23 в день перехода на летнее время, 25 — на зимнее.
    """
    try:
        tz = zoneinfo.ZoneInfo(iana_timezone)
        dt_start_local = datetime.strptime(f"{target_date_local} 00:00:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        
        from datetime import timedelta
        dt_end_local = datetime.strptime(
            f"{(dt_start_local.date() + timedelta(days=1)).strftime('%Y-%m-%d')} 00:00:00", 
            "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=tz)
        
        dt_start_utc = dt_start_local.astimezone(timezone.utc)
        dt_end_utc = dt_end_local.astimezone(timezone.utc)
        
        diff_hours = int(round((dt_end_utc - dt_start_utc).total_seconds() / 3600.0))
        return diff_hours
    except Exception:
        # Безопасный фолбек при невалидном имени таймзоны
        return 24


def calculate_derived_metrics(hourly_records: list) -> dict:
    """
    Рассчитывает агрегированные производные метрики на основе 12 почасовых параметров.
    Безопасно обрабатывает отсутствующие данные (None).
    """
    if not hourly_records:
        return {
            "max_temp_c": None,
            "min_temp_c": None,
            "peak_hour_local": None,
            "peak_window_max_temp_c": None,
            "peak_window_hour_local": None,
            "total_precip_mm": None,
            "max_wind_speed_ms": None,
            "max_wind_gust_ms": None
        }

    # Выбор присутствующих числовых значений
    temps = [r["temperature_2m"] for r in hourly_records if r.get("temperature_2m") is not None]
    precips = [r["precipitation"] for r in hourly_records if r.get("precipitation") is not None]
    winds = [r["wind_speed_10m"] for r in hourly_records if r.get("wind_speed_10m") is not None]
    gusts = [r["wind_gusts_10m"] for r in hourly_records if r.get("wind_gusts_10m") is not None]

    raw_max_temp = max(temps) if temps else None
    max_temp = round(raw_max_temp, 1) if raw_max_temp is not None else None
    min_temp = round(min(temps), 1) if temps else None

    # Поиск первого пикового часа для суточного максимума температуры по raw_max_temp
    peak_hour = None
    if raw_max_temp is not None:
        for r in hourly_records:
            if r.get("temperature_2m") == raw_max_temp:
                peak_hour = r["time_local"].split("T")[1][:5]
                break

    # Метрики дневного окна (10:00 - 18:00 локального времени)
    window_temps = []
    window_records = []
    for r in hourly_records:
        time_part = r["time_local"].split("T")[1][:2]
        if time_part.isdigit():
            hour_val = int(time_part)
            if 10 <= hour_val <= 18 and r.get("temperature_2m") is not None:
                window_temps.append(r["temperature_2m"])
                window_records.append(r)

    raw_peak_window_max = max(window_temps) if window_temps else None
    peak_window_max_temp = round(raw_peak_window_max, 1) if raw_peak_window_max is not None else None
    peak_window_hour = None
    if raw_peak_window_max is not None:
        for r in window_records:
            if r.get("temperature_2m") == raw_peak_window_max:
                peak_window_hour = r["time_local"].split("T")[1][:5]
                break

    # СЕМАНТИКА ОСАДКОВ:
    # 1) [0.0, 0.0] -> 0.0 мм
    # 2) [0.0, None, 1.5] -> 1.5 мм
    # 3) [None, None] / [] -> None (не подменяем "нет данных" на 0.0)
    total_precip = round(sum(precips), 2) if precips else None

    max_wind = round(max(winds), 1) if winds else None
    max_gust = round(max(gusts), 1) if gusts else None

    return {
        "max_temp_c": max_temp,
        "min_temp_c": min_temp,
        "peak_hour_local": peak_hour,
        "peak_window_max_temp_c": peak_window_max_temp,
        "peak_window_hour_local": peak_window_hour,
        "total_precip_mm": total_precip,
        "max_wind_speed_ms": max_wind,
        "max_wind_gust_ms": max_gust
    }


def fetch_openmeteo_forecast(
    latitude: float,
    longitude: float,
    iana_timezone: str,
    target_date_local: str
) -> dict:
    """
    Запрашивает прогноз из Open-Meteo API для 4 моделей и формирует полный Phase 3 payload.
    """
    retrieved_at_utc = datetime.now(timezone.utc).isoformat()
    expected_hours = _get_expected_local_hours(iana_timezone, target_date_local)
    
    # Получаем метаданные обновлений прогонов моделей из защищенного парсера
    updates_meta = fetch_model_updates_metadata()

    primary_models_res = {}
    secondary_models_res = {}

    url = "https://api.open-meteo.com/v1/forecast"
    hourly_param_str = ",".join(HOURLY_VARIABLES)

    for model_key, cfg in MODEL_CONFIGS.items():
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "timezone": iana_timezone,
            "models": cfg["api_param"],
            "hourly": hourly_param_str,
            "forecast_days": 3
        }

        resp = _http_get_with_retry(url, params=params)
        
        model_updates = updates_meta.get(model_key, {})
        
        status_info = {
            "available": False,
            "http_status": resp.status_code if resp else None,
            "error": None,
            "warnings": [],
            "retrieved_at_utc": retrieved_at_utc,
            "model_run_utc": model_updates.get("model_run_utc"),
            "model_available_utc": model_updates.get("model_available_utc"),
            "model_modified_utc": model_updates.get("model_modified_utc"),
            "temporal_resolution_seconds": model_updates.get("temporal_resolution_seconds"),
            "update_interval_seconds": model_updates.get("update_interval_seconds")
        }
        
        source_hourly_list = []
        derived_metrics = None

        if resp and resp.status_code == 200:
            try:
                data = resp.json()
                hourly_raw = data.get("hourly", {})
                times = hourly_raw.get("time", []) if isinstance(hourly_raw, dict) else []

                # Считываем и строго валидируем все 12 массивов параметров
                param_arrays = {}
                for var in HOURLY_VARIABLES:
                    arr = hourly_raw.get(var) if isinstance(hourly_raw, dict) else None
                    if arr is None:
                        status_info["warnings"].append(
                            f"Variable '{var}' is missing in API response for model {model_key}."
                        )
                        param_arrays[var] = []
                    elif not isinstance(arr, list):
                        status_info["warnings"].append(
                            f"Variable '{var}' returned invalid non-list type for model {model_key}."
                        )
                        param_arrays[var] = []
                    elif len(arr) != len(times):
                        status_info["warnings"].append(
                            f"Variable '{var}' length mismatch ({len(arr)} vs {len(times)}) for model {model_key}."
                        )
                        param_arrays[var] = arr  # Сохраняем доступные элементы, недостающие станут None
                    else:
                        if all(val is None for val in arr):
                            status_info["warnings"].append(
                                f"Variable '{var}' returned all null values for model {model_key}."
                            )
                        param_arrays[var] = arr

                # Фильтрация строго по целевой локальной дате (target_date_local)
                for idx, t_str in enumerate(times):
                    if isinstance(t_str, str) and t_str.startswith(target_date_local):
                        rec = {"time_local": t_str}
                        for var in HOURLY_VARIABLES:
                            arr = param_arrays[var]
                            # Гарантия: берем элемент только если arr — настоящий валидный список
                            if isinstance(arr, list) and idx < len(arr):
                                rec[var] = arr[idx]
                            else:
                                rec[var] = None
                        
                        source_hourly_list.append(rec)

                rec_count = len(source_hourly_list)
                if rec_count != expected_hours:
                    status_info["warnings"].append(
                        f"Unexpected records_count: {rec_count}. Expected exactly {expected_hours} hours."
                    )

                if rec_count > 0:
                    status_info["available"] = True
                    derived_metrics = calculate_derived_metrics(source_hourly_list)
                else:
                    status_info["error"] = "No hourly records found for target_date_local."

            except Exception as e:
                status_info["error"] = f"Failed to parse API response: {str(e)}"
        else:
            if not resp:
                status_info["error"] = "HTTP Request failed (Timeout or Network Error)."
            else:
                status_info["error"] = f"HTTP Error status code: {resp.status_code}"

        model_payload = {
            "model_info": cfg["model_info"],
            "data_provenance": cfg["data_provenance"],
            "status": status_info,
            "source_hourly": source_hourly_list,
            "derived_metrics": derived_metrics
        }

        if cfg["is_primary"]:
            primary_models_res[model_key] = model_payload
        else:
            secondary_models_res[model_key] = model_payload

    return {
        "target_date_local": target_date_local,
        "timezone": iana_timezone,
        "expected_records_count": expected_hours,
        "primary_models": primary_models_res,
        "secondary_models": secondary_models_res
    }