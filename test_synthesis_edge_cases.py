"""
Набор edge-case и контрактных тестов для openmeteo_service.py.
Все сетевые вызовы полностью замоканы через unittest.mock.
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pytest
import requests

from openmeteo_service import (
    HOURLY_VARIABLES,
    _get_expected_local_hours,
    _http_get_with_retry,
    _parse_retry_after,
    calculate_derived_metrics,
    fetch_model_updates_metadata,
    fetch_openmeteo_forecast,
)


# ==============================================================================
# ГРУППА A: Model Updates API (Сведения о run-времени моделей)
# ==============================================================================


@patch("openmeteo_service.requests.get")
def test_model_updates_valid_response(mock_get):
    """Корректный JSON с ключами ecmwfifs025, gfsseamless и конвертацией Epoch в ISO UTC."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "ecmwfifs025": {
            "last_run_initialisation_time": 1723464000,
            "temporal_resolution_seconds": 3600,
        },
        "gfsseamless": {
            "last_run_initialisation_time": 1723453200,
            "temporal_resolution_seconds": 3600,
        },
    }
    mock_get.return_value = mock_resp

    meta = fetch_model_updates_metadata()

    assert meta["ecmwf_hres"]["model_run_utc"] == "2024-08-12T12:00:00+00:00"
    assert meta["ecmwf_hres"]["temporal_resolution_seconds"] == 3600
    assert meta["gfs_global"]["model_run_utc"] == "2024-08-12T09:00:00+00:00"


@patch("openmeteo_service.requests.get")
def test_model_updates_missing_one_model(mock_get):
    """Отсутствие одной модели в JSON ответа не ломает другие модели."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "ecmwfifs025": {
            "last_run_initialisation_time": 1723464000,
            "temporal_resolution_seconds": 3600,
        }
    }
    mock_get.return_value = mock_resp

    meta = fetch_model_updates_metadata()
    assert meta["ecmwf_hres"]["model_run_utc"] is not None
    assert meta["gem_global"]["model_run_utc"] is None


@patch("openmeteo_service.requests.get")
def test_model_updates_api_failure_fallback(mock_get):
    """Полная недоступность Model Updates API не выбивает приложение и оставляет дефолты."""
    mock_get.side_effect = requests.RequestException("Network Error")

    meta = fetch_model_updates_metadata()
    assert meta["ecmwf_hres"]["model_run_utc"] is None
    assert meta["gem_global"]["model_run_utc"] is None


@patch("openmeteo_service.requests.get")
def test_model_updates_invalid_json_type(mock_get):
    """Некорректная структура JSON (список вместо словаря) не приводит к сбою."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = ["unexpected", "array"]
    mock_get.return_value = mock_resp

    meta = fetch_model_updates_metadata()
    assert meta["ecmwf_hres"]["model_run_utc"] is None


# ==============================================================================
# ГРУППА B: Hourly Arrays (Валидация почасовых массивов)
# ==============================================================================


@patch("openmeteo_service.fetch_model_updates_metadata")
@patch("openmeteo_service.requests.get")
def test_hourly_missing_variable(mock_get, mock_meta):
    """Отсутствующая переменная приводит к предупреждению (warning) и значению None."""
    mock_meta.return_value = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    times = [f"2026-08-12T{h:02d}:00" for h in range(24)]
    hourly_payload = {"time": times}

    # Пропускаем переменную 'shortwave_radiation'
    for var in HOURLY_VARIABLES:
        if var != "shortwave_radiation":
            hourly_payload[var] = [10.0] * 24

    mock_resp.json.return_value = {"hourly": hourly_payload}
    mock_get.return_value = mock_resp

    res = fetch_openmeteo_forecast(55.75, 37.61, "UTC", "2026-08-12")
    model_data = res["primary_models"]["ecmwf_hres"]

    assert model_data["status"]["available"] is True
    assert any("Variable 'shortwave_radiation' is missing" in w for w in model_data["status"]["warnings"])
    assert model_data["source_hourly"][0]["shortwave_radiation"] is None


@patch("openmeteo_service.fetch_model_updates_metadata")
@patch("openmeteo_service.requests.get")
def test_hourly_array_all_none(mock_get, mock_meta):
    """Массив полностью из None (null) генерирует warning."""
    mock_meta.return_value = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    times = [f"2026-08-12T{h:02d}:00" for h in range(24)]
    hourly_payload = {"time": times}
    for var in HOURLY_VARIABLES:
        hourly_payload[var] = [None] * 24 if var == "cloud_cover" else [5.0] * 24

    mock_resp.json.return_value = {"hourly": hourly_payload}
    mock_get.return_value = mock_resp

    res = fetch_openmeteo_forecast(55.75, 37.61, "UTC", "2026-08-12")
    model_data = res["primary_models"]["ecmwf_hres"]

    assert any("returned all null values" in w for w in model_data["status"]["warnings"])


@patch("openmeteo_service.fetch_model_updates_metadata")
@patch("openmeteo_service.requests.get")
def test_hourly_array_length_mismatch_short(mock_get, mock_meta):
    """Массив короче 'time': генерирует warning, недостающие значения становятся None."""
    mock_meta.return_value = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    times = [f"2026-08-12T{h:02d}:00" for h in range(24)]
    hourly_payload = {"time": times}
    for var in HOURLY_VARIABLES:
        hourly_payload[var] = [1.0] * 12 if var == "temperature_2m" else [1.0] * 24

    mock_resp.json.return_value = {"hourly": hourly_payload}
    mock_get.return_value = mock_resp

    res = fetch_openmeteo_forecast(55.75, 37.61, "UTC", "2026-08-12")
    model_data = res["primary_models"]["ecmwf_hres"]

    assert any("length mismatch" in w for w in model_data["status"]["warnings"])
    assert model_data["source_hourly"][0]["temperature_2m"] == 1.0
    assert model_data["source_hourly"][15]["temperature_2m"] is None


@patch("openmeteo_service.fetch_model_updates_metadata")
@patch("openmeteo_service.requests.get")
def test_hourly_array_length_mismatch_long(mock_get, mock_meta):
    """Массив длиннее 'time': генерирует warning, лишние элементы отсекаются."""
    mock_meta.return_value = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    times = [f"2026-08-12T{h:02d}:00" for h in range(24)]
    hourly_payload = {"time": times}
    for var in HOURLY_VARIABLES:
        hourly_payload[var] = [1.0] * 30 if var == "temperature_2m" else [1.0] * 24

    mock_resp.json.return_value = {"hourly": hourly_payload}
    mock_get.return_value = mock_resp

    res = fetch_openmeteo_forecast(55.75, 37.61, "UTC", "2026-08-12")
    model_data = res["primary_models"]["ecmwf_hres"]

    assert any("length mismatch" in w for w in model_data["status"]["warnings"])


@patch("openmeteo_service.fetch_model_updates_metadata")
@patch("openmeteo_service.requests.get")
def test_hourly_valid_arrays_no_warnings(mock_get, mock_meta):
    """Нормальные массивы не генерируют ложных warning."""
    mock_meta.return_value = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    times = [f"2026-08-12T{h:02d}:00" for h in range(24)]
    hourly_payload = {"time": times}
    for var in HOURLY_VARIABLES:
        hourly_payload[var] = [15.0] * 24

    mock_resp.json.return_value = {"hourly": hourly_payload}
    mock_get.return_value = mock_resp

    res = fetch_openmeteo_forecast(55.75, 37.61, "UTC", "2026-08-12")
    model_data = res["primary_models"]["ecmwf_hres"]

    assert len(model_data["status"]["warnings"]) == 0


@patch("openmeteo_service.fetch_model_updates_metadata")
@patch("openmeteo_service.requests.get")
def test_hourly_non_list_variable(mock_get, mock_meta):
    """Некорректное значение переменной (не список) обрабатывается корректно."""
    mock_meta.return_value = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    times = [f"2026-08-12T{h:02d}:00" for h in range(24)]
    hourly_payload = {"time": times}
    for var in HOURLY_VARIABLES:
        hourly_payload[var] = "invalid_string_instead_of_list" if var == "temperature_2m" else [1.0] * 24

    mock_resp.json.return_value = {"hourly": hourly_payload}
    mock_get.return_value = mock_resp

    res = fetch_openmeteo_forecast(55.75, 37.61, "UTC", "2026-08-12")
    model_data = res["primary_models"]["ecmwf_hres"]

    # Проверяем актуальный контракт warning для типов, отличных от list
    assert any(
        "returned invalid non-list type" in w
        for w in model_data["status"]["warnings"]
    )
    assert model_data["source_hourly"][0]["temperature_2m"] is None


# ==============================================================================
# ГРУППА C: Retry-After & Network Resilience
# ==============================================================================


def test_retry_after_delta_seconds():
    """Разбор секундного формата Retry-After (например, '120')."""
    assert _parse_retry_after("120") == 120.0
    assert _parse_retry_after("-10") == 0.0


def test_retry_after_http_date_format():
    """Динамический расчет задержки от будущей даты в формате HTTP-Date (RFC 1123)."""
    future_time = datetime.now(timezone.utc) + timedelta(seconds=120)
    http_date_str = future_time.strftime("%a, %d %b %Y %H:%M:%S GMT")

    delay = _parse_retry_after(http_date_str)
    assert delay is not None
    assert 110.0 <= delay <= 130.0


def test_retry_after_invalid_fallback():
    """Невалидный заголовок Retry-After возвращает None для перехода на backoff."""
    assert _parse_retry_after("invalid-format-string") is None


@patch("openmeteo_service.requests.get")
def test_no_retry_on_client_errors(mock_get):
    """HTTP 400/401/403/404 возвращаются сразу без повторных попыток."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_get.return_value = mock_resp

    res = _http_get_with_retry("http://test.com", {}, retries=3)
    assert res.status_code == 404
    assert mock_get.call_count == 1


@patch("openmeteo_service.time.sleep")
@patch("openmeteo_service.requests.get")
def test_retry_on_server_errors(mock_get, mock_sleep):
    """HTTP 500/502/503/504 приводят к повторным попыткам (retry)."""
    mock_resp_500 = MagicMock()
    mock_resp_500.status_code = 500
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200

    mock_get.side_effect = [mock_resp_500, mock_resp_200]

    res = _http_get_with_retry("http://test.com", {}, retries=3)
    assert res.status_code == 200
    assert mock_get.call_count == 2


@patch("openmeteo_service.time.sleep")
@patch("openmeteo_service.requests.get")
def test_http_429_retry_with_retry_after(mock_get, mock_sleep):
    """HTTP 429 парсит Retry-After и засыпает на указанное время."""
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.headers = {"Retry-After": "60"}

    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200

    mock_get.side_effect = [mock_resp_429, mock_resp_200]

    res = _http_get_with_retry("http://test.com", {}, retries=2)
    assert res.status_code == 200
    mock_sleep.assert_called_with(60.0)


@patch("openmeteo_service.time.sleep")
@patch("openmeteo_service.requests.get")
def test_http_429_retry_invalid_retry_after_fallback(mock_get, mock_sleep):
    """HTTP 429 при невалидном Retry-After переходит на exponential backoff."""
    mock_resp_429 = MagicMock()
    mock_resp_429.status_code = 429
    mock_resp_429.headers = {"Retry-After": "corrupted"}

    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200

    mock_get.side_effect = [mock_resp_429, mock_resp_200]

    res = _http_get_with_retry("http://test.com", {}, retries=2)
    assert res.status_code == 200
    # При стандартном факторе 1.0 на первой попытке задержка составляет 1.0 сек
    mock_sleep.assert_called_with(1.0)


# ==============================================================================
# ГРУППА D: Derived Metrics & Safe Math
# ==============================================================================


def test_derived_metrics_max_temp_and_first_peak_hour():
    """Максимальная температура и выбор ПЕРВОГО часа при дублировании максимума."""
    records = [
        {"time_local": "2026-08-12T08:00", "temperature_2m": 20.0, "precipitation": 0.0, "wind_speed_10m": 5.0, "wind_gusts_10m": 8.0},
        {"time_local": "2026-08-12T12:00", "temperature_2m": 28.5, "precipitation": 0.0, "wind_speed_10m": 5.0, "wind_gusts_10m": 8.0},
        {"time_local": "2026-08-12T15:00", "temperature_2m": 28.5, "precipitation": 0.0, "wind_speed_10m": 5.0, "wind_gusts_10m": 8.0},
    ]
    metrics = calculate_derived_metrics(records)
    assert metrics["max_temp_c"] == 28.5
    assert metrics["peak_hour_local"] == "12:00"


def test_derived_metrics_all_temps_none():
    """Все значения температуры равны None -> корректная обработка без упавшей ошибки."""
    records = [
        {"time_local": "2026-08-12T08:00", "temperature_2m": None, "precipitation": None, "wind_speed_10m": None, "wind_gusts_10m": None}
    ]
    metrics = calculate_derived_metrics(records)
    assert metrics["max_temp_c"] is None
    assert metrics["peak_hour_local"] is None


def test_derived_metrics_precipitation_zero_vs_none():
    """Различение чистых нулей [0.0, 0.0] -> 0.0 и отсутствия данных [None, None] -> None."""
    records_zeros = [
        {"time_local": "2026-08-12T08:00", "precipitation": 0.0},
        {"time_local": "2026-08-12T09:00", "precipitation": 0.0},
    ]
    assert calculate_derived_metrics(records_zeros)["total_precip_mm"] == 0.0

    records_nones = [
        {"time_local": "2026-08-12T08:00", "precipitation": None},
        {"time_local": "2026-08-12T09:00", "precipitation": None},
    ]
    assert calculate_derived_metrics(records_nones)["total_precip_mm"] is None


def test_derived_metrics_precipitation_mixed():
    """Смешанные значения [0.0, None, 1.5] корректно суммируются в 1.5."""
    records_mixed = [
        {"time_local": "2026-08-12T08:00", "precipitation": 0.0},
        {"time_local": "2026-08-12T09:00", "precipitation": None},
        {"time_local": "2026-08-12T10:00", "precipitation": 1.5},
    ]
    assert calculate_derived_metrics(records_mixed)["total_precip_mm"] == 1.5


def test_derived_metrics_wind_and_gust_with_none():
    """Ветер и порывы с значениями None выбирают корректные максимумы по правильным ключам."""
    records = [
        {"time_local": "2026-08-12T08:00", "wind_speed_10m": None, "wind_gusts_10m": 12.0},
        {"time_local": "2026-08-12T09:00", "wind_speed_10m": 15.5, "wind_gusts_10m": None},
        {"time_local": "2026-08-12T10:00", "wind_speed_10m": 10.0, "wind_gusts_10m": 25.0},
    ]
    metrics = calculate_derived_metrics(records)
    assert metrics["max_wind_speed_ms"] == 15.5
    assert metrics["max_wind_gust_ms"] == 25.0


# ==============================================================================
# ГРУППА E: DST (Daylight Saving Time) Hours
# ==============================================================================


def test_dst_expected_hours_normal_day():
    """Обычные сутки состоят из 24 часов."""
    assert _get_expected_local_hours("Europe/London", "2026-08-12") == 24


def test_dst_expected_hours_spring_forward():
    """Переход на летнее время (29 марта 2026 в Великобритании) -> 23 часа."""
    assert _get_expected_local_hours("Europe/London", "2026-03-29") == 23


def test_dst_expected_hours_fall_back():
    """Переход на зимнее время (25 октября 2026 в Великобритании) -> 25 часов."""
    assert _get_expected_local_hours("Europe/London", "2026-10-25") == 25


# ==============================================================================
# ГРУППА F: Target Date Filtering
# ==============================================================================


@patch("openmeteo_service.fetch_model_updates_metadata")
@patch("openmeteo_service.requests.get")
def test_target_date_strict_filtering(mock_get, mock_meta):
    """Отсечение часов, выходящих за границы целевой локальной даты."""
    mock_meta.return_value = {}
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    times = [
        "2026-08-11T23:00",
        "2026-08-12T00:00",
        "2026-08-12T01:00",
        "2026-08-12T02:00",
        "2026-08-13T00:00",
    ]
    hourly_payload = {"time": times}
    for var in HOURLY_VARIABLES:
        hourly_payload[var] = [15.0] * len(times)

    mock_resp.json.return_value = {"hourly": hourly_payload}
    mock_get.return_value = mock_resp

    res = fetch_openmeteo_forecast(55.75, 37.61, "UTC", "2026-08-12")
    model_data = res["primary_models"]["ecmwf_hres"]

    extracted_times = [r["time_local"] for r in model_data["source_hourly"]]
    assert len(extracted_times) == 3
    assert all(t.startswith("2026-08-12") for t in extracted_times)
    assert "2026-08-11T23:00" not in extracted_times