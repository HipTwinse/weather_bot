"""
Модуль сбора, валидации и агрегации метеоданных (Phase 3 Engine).
Оптимизирован для высокоскоростного параллельного опроса моделей (ThreadPoolExecutor)
с надежными таймаутами для исключения сбоев 'available: false'.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from typing import Optional
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
            "native_temporal_resolution_seconds": 10800,
            "api_temporal_resolution": "hourly"
        },
        "data_provenance": {
            "native_timestep_seconds": 10800,
            "api_timestep_seconds": 3600,
            "temporally_interpolated": True
        }
    }
}


def _parse_retry_after(retry_after_header: Optional[str]) -> Optional[float]:
    if not retry_after_header:
        return None
    try:
        val = float(retry_after_header)
        return max(0.0, val)
    except (ValueError, TypeError):
        pass

    try:
        target_dt = parsedate_to_datetime(retry_after_header)
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=timezone.utc)
        now_dt = datetime.now(timezone.utc)
        delay = (target_dt - now_dt).total_seconds()
        return max(0.0, delay)
    except Exception:
        return None


def _http_get_with_retry(url: str, params: dict, retries: int = 2, timeout: int = 8) -> Optional[requests.Response]:
    """Сетевой запрос с достаточным таймаутом (8s) и User-Agent."""
    headers = {"User-Agent": "WeatherAlphaBot/6.1 (Automated Market Analysis)"}
    retryable_statuses = {500, 502, 503, 504}
    backoff_delay = 0.5

    for attempt in range(retries + 1):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response

            if response.status_code == 429:
                if attempt < retries:
                    retry_after = response.headers.get("Retry-After")
                    parsed_delay = _parse_retry_after(retry_after)
                    delay_to_use = parsed_delay if parsed_delay is not None else backoff_delay
                    time.sleep(delay_to_use)
                    backoff_delay *= 2.0
                    continue
                return response

            if response.status_code in retryable_statuses:
                if attempt < retries:
                    time.sleep(backoff_delay)
                    backoff_delay *= 2.0
                    continue
                return response

            return response

        except (requests.Timeout, requests.ConnectionError, requests.RequestException):
            if attempt < retries:
                time.sleep(backoff_delay)
                backoff_delay *= 2.0
                continue
            return None

    return None


def _get_expected_local_hours(iana_timezone: str, target_date_local: str) -> int:
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
        
        return int(round((dt_end_utc - dt_start_utc).total_seconds() / 3600.0))
    except Exception:
        return 24


def calculate_derived_metrics(hourly_records: list) -> dict:
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

    temps = [r["temperature_2m"] for r in hourly_records if r.get("temperature_2m") is not None]
    precips = [r["precipitation"] for r in hourly_records if r.get("precipitation") is not None]
    winds = [r["wind_speed_10m"] for r in hourly_records if r.get("wind_speed_10m") is not None]
    gusts = [r["wind_gusts_10m"] for r in hourly_records if r.get("wind_gusts_10m") is not None]

    raw_max_temp = max(temps) if temps else None
    max_temp = round(raw_max_temp, 1) if raw_max_temp is not None else None
    min_temp = round(min(temps), 1) if temps else None

    peak_hour = None
    if raw_max_temp is not None:
        for r in hourly_records:
            if r.get("temperature_2m") == raw_max_temp:
                peak_hour = r["time_local"].split("T")[1][:5]
                break

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


def _fetch_single_model(model_key: str, cfg: dict, url: str, base_params: dict, retrieved_at_utc: str, expected_hours: int, target_date_local: str):
    """Изолированный параллельный запрос для одной численной модели."""
    params = base_params.copy()
    params["models"] = cfg["api_param"]
    
    # 8 секунд таймаута и 2 ретрая предотвращают сбои при медленном ответе Open-Meteo
    resp = _http_get_with_retry(url, params=params, retries=2, timeout=8)
    
    status_info = {
        "available": False,
        "http_status": resp.status_code if resp else None,
        "error": None,
        "warnings": [],
        "retrieved_at_utc": retrieved_at_utc,
        "model_run_utc": None,
        "model_available_utc": None,
        "model_modified_utc": None,
        "temporal_resolution_seconds": cfg["data_provenance"]["native_timestep_seconds"],
        "update_interval_seconds": None
    }
    
    source_hourly_list = []
    derived_metrics = None

    if resp and resp.status_code == 200:
        try:
            data = resp.json()
            hourly_raw = data.get("hourly", {})
            times = hourly_raw.get("time", []) if isinstance(hourly_raw, dict) else []

            param_arrays = {}
            for var in HOURLY_VARIABLES:
                arr = hourly_raw.get(var) if isinstance(hourly_raw, dict) else None
                param_arrays[var] = arr if isinstance(arr, list) else []

            for idx, t_str in enumerate(times):
                if isinstance(t_str, str) and t_str.startswith(target_date_local):
                    rec = {"time_local": t_str}
                    for var in HOURLY_VARIABLES:
                        arr = param_arrays[var]
                        rec[var] = arr[idx] if idx < len(arr) else None
                    source_hourly_list.append(rec)

            if len(source_hourly_list) > 0:
                status_info["available"] = True
                derived_metrics = calculate_derived_metrics(source_hourly_list)
            else:
                status_info["error"] = "No hourly records found for target_date_local."
        except Exception as e:
            status_info["error"] = f"Failed to parse API response: {str(e)}"
    else:
        status_info["error"] = "HTTP Request failed or timed out."

    return model_key, cfg["is_primary"], {
        "model_info": cfg["model_info"],
        "data_provenance": cfg["data_provenance"],
        "status": status_info,
        "source_hourly": source_hourly_list,
        "derived_metrics": derived_metrics
    }


def fetch_openmeteo_forecast(
    latitude: float,
    longitude: float,
    iana_timezone: str,
    target_date_local: str
) -> dict:
    """Параллельный опрос всех 4 моделей через ThreadPoolExecutor."""
    retrieved_at_utc = datetime.now(timezone.utc).isoformat()
    expected_hours = _get_expected_local_hours(iana_timezone, target_date_local)
    
    url = "https://api.open-meteo.com/v1/forecast"
    base_params = {
        "latitude": latitude,
        "longitude": longitude,
        "timezone": iana_timezone,
        "hourly": ",".join(HOURLY_VARIABLES),
        "forecast_days": 3
    }

    primary_models_res = {}
    secondary_models_res = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(
                _fetch_single_model, 
                m_key, cfg, url, base_params, retrieved_at_utc, expected_hours, target_date_local
            )
            for m_key, cfg in MODEL_CONFIGS.items()
        ]
        for f in futures:
            m_key, is_primary, payload = f.result()
            if is_primary:
                primary_models_res[m_key] = payload
            else:
                secondary_models_res[m_key] = payload

    return {
        "target_date_local": target_date_local,
        "timezone": iana_timezone,
        "expected_records_count": expected_hours,
        "primary_models": primary_models_res,
        "secondary_models": secondary_models_res
    }