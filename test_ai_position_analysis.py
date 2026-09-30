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


def test_quick_position_callbacks():
    async def _test_flow():
        from handlers import process_quick_pos_save, process_quick_pos_close
        from database import init_db, get_user_positions, delete_position

        init_db()
        user_id = 777888
        # Clean up test user positions first
        existing = get_user_positions(user_id)
        for p in existing:
            delete_position(p["id"], user_id)

        # 1. Test quick pos save
        mock_callback = AsyncMock()
        mock_callback.from_user.id = user_id
        mock_callback.data = "qps:EGLC:25C:18:2026-09-30"
        mock_callback.message.edit_text = AsyncMock()
        mock_callback.answer = AsyncMock()

        await process_quick_pos_save(mock_callback)

        positions = get_user_positions(user_id)
        assert len(positions) == 1
        assert positions[0]["icao"] == "EGLC"
        assert positions[0]["outcomes"] == "25°C"
        assert positions[0]["entry_price"] == 18.0
        assert positions[0]["target_date"] == "2026-09-30"

        # 2. Test quick pos close
        pos_id = positions[0]["id"]
        close_callback = AsyncMock()
        close_callback.from_user.id = user_id
        close_callback.data = f"quick_pos_close:{pos_id}:EGLC"
        close_callback.answer = AsyncMock()

        with patch("handlers.process_express_scan_callback", new_callable=AsyncMock) as mock_scan:
            await process_quick_pos_close(close_callback)
            mock_scan.assert_called_once()

        closed_positions = get_user_positions(user_id)
        assert len(closed_positions) == 0

    asyncio.run(_test_flow())

