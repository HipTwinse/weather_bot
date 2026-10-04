import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from handlers import (
    process_express_scan_callback,
    process_city_callback,
    ai_cities_inline_keyboard,
    cities_inline_keyboard,
)

def test_express_scan_callback_success():
    async def _test():
        cb = MagicMock()
        cb.data = "express_scan:EGLC"
        cb.from_user.id = 9999
        cb.answer = AsyncMock()

        status_msg = MagicMock()
        status_msg.edit_text = AsyncMock()
        status_msg.answer = AsyncMock()
        cb.message.reply = AsyncMock(return_value=status_msg)
        cb.message.answer = AsyncMock()

        with patch("handlers.fetch_openmeteo_forecast", return_value={"primary_models": {"icon_global": {"status": {"available": True}, "derived_metrics": {"max_temp_c": 21.0}}}}), \
             patch("handlers.get_noaa_package", return_value={"metar": {"temp_c": 15.0, "raw": "EGLC 041000Z 12005KT 15/10 Q1015"}}), \
             patch("handlers.find_city_weather_event", new_callable=AsyncMock) as mock_find_event, \
             patch("handlers.analyze_city_weather_ai", new_callable=AsyncMock) as mock_ai:
            
            mock_find_event.return_value = {
                "slug": "london-temp",
                "id": "123",
                "markets": [{"outcome": "21°C", "last_price": 0.40, "best_bid": 0.38, "best_ask": 0.42, "volume": 1000}],
            }
            mock_ai.return_value = "⚡ <b>Квант-Анализ AI</b>\n\nВсе параметры в норме."

            await process_express_scan_callback(cb)

            cb.answer.assert_called_once()
            assert status_msg.edit_text.called
            call_text = status_msg.edit_text.call_args[0][0]
            assert "Квант-Анализ AI" in call_text or "ЭКСПРЕСС-АНАЛИЗ" in call_text

    asyncio.run(_test())

def test_icao_callback_runs_weather_pipeline():
    async def _test():
        cb = MagicMock()
        cb.data = "icao:EGLC"
        cb.from_user.id = 9999
        cb.from_user.username = "test_user"
        cb.answer = AsyncMock()
        cb.message = MagicMock()

        with patch("handlers._execute_weather_pipeline", new_callable=AsyncMock) as mock_pipe:
            mock_state = MagicMock()
            mock_state.clear = AsyncMock()
            await process_city_callback(cb, mock_state)
            cb.answer.assert_called_once()
            mock_pipe.assert_called_once_with("EGLC", cb.message)

    asyncio.run(_test())

def test_keyboards_callback_routes():
    for row in ai_cities_inline_keyboard.inline_keyboard:
        for btn in row:
            assert btn.callback_data.startswith("express_scan:"), f"Button {btn.text} has unexpected callback {btn.callback_data}"

    for row in cities_inline_keyboard.inline_keyboard:
        for btn in row:
            assert btn.callback_data.startswith("icao:"), f"Button {btn.text} has unexpected callback {btn.callback_data}"
