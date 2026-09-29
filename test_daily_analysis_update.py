import pytest
import asyncio
import time
from datetime import datetime
import zoneinfo
from unittest.mock import AsyncMock, patch

from gemini_analyzer import (
    DailyAnalysisTracker,
    daily_tracker,
    compute_orderbook_shifts,
    compute_weather_delta,
    analyze_city_weather_ai,
    SYSTEM_PROMPT_V8_0,
)


def test_tracker_record_and_get():
    tracker = DailyAnalysisTracker()
    snap1 = {
        "timestamp": 1000.0,
        "time_str": "10:30 LT",
        "target_date": "2026-09-29",
        "temp_c": 12.0,
    }
    tracker.record(user_id=123, icao="EGLC", date_str="2026-09-29", snapshot=snap1)

    # Запрос для того же пользователя
    res = tracker.get_latest(user_id=123, icao="EGLC", date_str="2026-09-29")
    assert res is not None
    assert res["temp_c"] == 12.0
    assert res["time_str"] == "10:30 LT"

    # Запрос для другого пользователя (должен сработать fallback на общий срез 0)
    res_other = tracker.get_latest(user_id=999, icao="EGLC", date_str="2026-09-29")
    assert res_other is not None
    assert res_other["temp_c"] == 12.0

    # Запрос для другого города (нет данных)
    assert tracker.get_latest(user_id=123, icao="LFPB", date_str="2026-09-29") is None


def test_tracker_end_of_day_reset():
    tracker = DailyAnalysisTracker()
    snap_yesterday = {
        "timestamp": 1000.0,
        "time_str": "15:00 LT",
        "target_date": "2026-09-28",
        "temp_c": 18.0,
    }
    tracker.record(user_id=123, icao="EGLC", date_str="2026-09-28", snapshot=snap_yesterday)

    # При наступлении нового дня замер за вчера должен автоматически сброситься
    res_today = tracker.get_latest(user_id=123, icao="EGLC", date_str="2026-09-29")
    assert res_today is None

    # Проверяем, что вчерашние записи удалены
    assert len(tracker._history) == 0


def test_compute_orderbook_shifts():
    prev_ob = [
        {"title": "22°C", "temp": 22.0, "price_cents": 35.0},
        {"title": "23°C", "temp": 23.0, "price_cents": 42.0},
        {"title": "24°C", "temp": 24.0, "price_cents": 15.0},
    ]
    curr_ob = [
        {"title": "22°C", "temp": 22.0, "price_cents": 35.2},  # Изменение < 1.0¢ (игнорируем)
        {"title": "23°C", "temp": 23.0, "price_cents": 55.0},  # Рост +13¢
        {"title": "24°C", "temp": 24.0, "price_cents": 8.0},   # Падение -7¢
    ]

    shifts = compute_orderbook_shifts(prev_ob, curr_ob)
    assert "23°C: 42¢ ➔ 55¢ (+13¢)" in shifts
    assert "24°C: 15¢ ➔ 8¢ (-7¢)" in shifts
    assert "22°C" not in shifts


def test_compute_weather_delta():
    t0 = 1700000000.0
    t1 = t0 + 3600.0  # Прошел 1 час (60 мин)

    prev_snap = {
        "timestamp": t0,
        "time_str": "11:00 LT",
        "temp_c": 12.0,
        "rate_str": "+0.8°C/ч",
        "orderbook": [{"title": "23°C", "temp": 23.0, "price_cents": 30.0}],
        "user_position": {
            "outcomes": "23°C",
            "cur_price": 30.0,
            "pnl_str": "+10%",
        },
    }

    curr_snap = {
        "timestamp": t1,
        "time_str": "12:00 LT",
        "temp_c": 13.5,
        "rate_str": "+1.5°C/ч",
        "orderbook": [{"title": "23°C", "temp": 23.0, "price_cents": 48.0}],
        "user_position": {
            "outcomes": "23°C",
            "cur_price": 48.0,
            "pnl_str": "+60%",
        },
    }

    delta = compute_weather_delta(prev_snap, curr_snap)
    assert delta["is_update"] is True
    assert delta["elapsed_min"] == 60
    assert delta["time_prev"] == "11:00 LT"
    assert delta["time_curr"] == "12:00 LT"
    assert delta["temp_prev"] == 12.0
    assert delta["temp_curr"] == 13.5
    assert delta["temp_diff"] == 1.5
    assert delta["temp_diff_str"] == "+1.5°C"
    assert delta["interval_rate_str"] == "+1.50°C/ч"
    assert "23°C: 30¢ ➔ 48¢ (+18¢)" in delta["orderbook_shifts_str"]
    assert "цена 30¢ ➔ 48¢, PnL +10% ➔ +60%" in delta["pos_delta_str"]


def test_analyze_city_weather_ai_first_and_second_call():
    daily_tracker.clear_all()

    async def _run():
        city_pack = {
            "icao": "EGLC",
            "city_name": "Лондон",
            "local_dt": datetime(2026, 9, 29, 10, 30, tzinfo=zoneinfo.ZoneInfo("Europe/London")),
            "target_date": "2026-09-29",
            "user_id": 42,
            "temp_c": 12.0,
            "raw_metar": "EGLC 290930Z 24008KT CAVOK 12/08 Q1015",
            "models_max": {"icon_global": 16.0, "ecmwf_hres": 15.5},
            "rate_str": "+0.8°C/ч",
            "rate_val": 0.8,
            "rem_hours_str": "6.0 ч",
            "orderbook": [{"title": "16°C", "temp": 16.0, "price_cents": 35.0}],
            "user_position": None,
        }

        with patch("gemini_analyzer.is_gemini_configured", return_value=True), \
             patch("gemini_analyzer.ask_gemini_model", new_callable=AsyncMock) as mock_ask:
            mock_ask.return_value = "<b>Ответ Gemini 1</b>"

            # 1. Первый запрос (утренний)
            res1 = await analyze_city_weather_ai(city_pack, scenario="A")
            assert res1 == "<b>Ответ Gemini 1</b>"
            prompt1 = mock_ask.call_args[0][0]
            assert "🔄 ДНЕВНОЕ ОБНОВЛЕНИЕ" not in prompt1

            # 2. Второй запрос через 90 минут (в тот же день)
            city_pack_update = dict(city_pack)
            city_pack_update["local_dt"] = datetime(2026, 9, 29, 12, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London"))
            city_pack_update["temp_c"] = 14.0
            city_pack_update["orderbook"] = [{"title": "16°C", "temp": 16.0, "price_cents": 52.0}]

            # Подправляем timestamp предыдущего снимка в трекере, чтобы эмулировать 90 минут разницы
            prev_in_tracker = daily_tracker.get_latest(42, "EGLC", "2026-09-29")
            assert prev_in_tracker is not None
            prev_in_tracker["timestamp"] = time.time() - 5400.0  # 90 мин назад

            mock_ask.reset_mock()
            mock_ask.return_value = "<b>Ответ Gemini 2 (Обновление)</b>"

            res2 = await analyze_city_weather_ai(city_pack_update, scenario="A")
            assert res2 == "<b>Ответ Gemini 2 (Обновление)</b>"
            prompt2 = mock_ask.call_args[0][0]

            # Проверяем, что во втором запросе включился Сценарий Г и блок сравнения
            assert "🔄 ДНЕВНОЕ ОБНОВЛЕНИЕ / СРАВНЕНИЕ С ПРОШЛЫМ АНАЛИЗОМ" in prompt2
            assert "Это ПОВТОРНЫЙ запрос анализа по городу Лондон за сегодня (2026-09-29)" in prompt2
            assert "12.0°C до 14.0°C (+2.0°C)" in prompt2
            assert "16°C: 35¢ ➔ 52¢ (+17¢)" in prompt2
            assert "СЦЕНАРИЮ Г" in prompt2

    asyncio.run(_run())
