import pytest
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch
import zoneinfo

from gemini_analyzer import analyze_city_weather_ai, SYSTEM_PROMPT_V8_0
from polymarket_service import get_current_outcome_price, extract_temp_value


def test_system_prompt_has_position_guidelines():
    assert "8. ПРАВИЛА ОЦЕНКИ ОТКРЫТОЙ ПОЗИЦИИ ТРЕЙДЕРА (HOLD vs EXIT)" in SYSTEM_PROMPT_V8_0
    assert "ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ)" in SYSTEM_PROMPT_V8_0
    assert "ТАЙМ-СТОП (13:30 LT)" in SYSTEM_PROMPT_V8_0
    assert "ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)" in SYSTEM_PROMPT_V8_0
    assert "ДЕРЖАТЬ ПОЗИЦИЮ" in SYSTEM_PROMPT_V8_0


def test_analyze_city_weather_ai_includes_user_position():
    async def _run():
        city_pack = {
            "icao": "EGLC",
            "city_name": "Лондон",
            "local_dt": datetime(2026, 9, 29, 11, 30, tzinfo=zoneinfo.ZoneInfo("Europe/London")),
            "temp_c": 19.5,
            "raw_metar": "EGLC 291030Z 24008KT CAVOK 20/12 Q1015",
            "models_max": {"icon_global": 23.2, "ecmwf_hres": 22.0, "gfs_global": 23.5},
            "rate_str": "+1.2°C/ч",
            "rate_val": 1.2,
            "rem_hours_str": "5.5 ч",
            "orderbook": [
                {"title": "23°C", "temp": 23.0, "price_cents": 42.0},
                {"title": "24°C", "temp": 24.0, "price_cents": 18.0},
            ],
            "user_position": {
                "outcomes": "23°C",
                "entry_price": 28.0,
                "cur_price": 42.0,
                "pnl_str": "+50%",
                "pnl_val": 50.0,
                "target_temp": 23.0,
                "target_date": "2026-09-29",
                "calculated_verdict": "🚨 ТЕЙК-ПРОФИТ (ВЫХОДИ ЛИМИТКОЙ)",
            },
        }

        with patch("gemini_analyzer.is_gemini_configured", return_value=True), \
             patch("gemini_analyzer.ask_gemini_model", new_callable=AsyncMock) as mock_ask:
            mock_ask.return_value = "<b>Ответ AI</b>"

            res = await analyze_city_weather_ai(city_pack, scenario="B")
            assert res == "<b>Ответ AI</b>"

            mock_ask.assert_called_once()
            sent_prompt = mock_ask.call_args[0][0]
            assert "ВАША ОТКРЫТАЯ ПОЗИЦИЯ В ЭТОМ ГОРОДЕ" in sent_prompt
            assert "23°C" in sent_prompt
            assert "+50%" in sent_prompt
            assert "Цена входа: 28.0¢" in sent_prompt
            assert "42.0¢" in sent_prompt
            assert "ТЕЙК-ПРОФИТ" in sent_prompt

    asyncio.run(_run())


def test_get_current_outcome_price():
    orderbook = [
        {"title": "21°C", "temp": 21.0, "price_cents": 10.0},
        {"title": "22°C", "temp": 22.0, "price_cents": 35.0},
        {"title": "23°C", "temp": 23.0, "price_cents": 55.0},
    ]
    assert get_current_outcome_price(orderbook, "22°C") == 35.0
    assert get_current_outcome_price(orderbook, "23") == 55.0
    assert get_current_outcome_price(orderbook, "25°C") is None
