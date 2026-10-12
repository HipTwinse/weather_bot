import asyncio
import pytest
from unittest.mock import patch, AsyncMock
from datetime import datetime
import zoneinfo

from auto_scanner import generate_midday_standby_briefing, sanitize_telegram_html


def test_generate_midday_standby_briefing():
    async def _test():
        london_dt = datetime.now(zoneinfo.ZoneInfo("Europe/London"))
        metrics = [
            {
                "icao": "EGLC",
                "city_name": "🇬🇧 Лондон (EGLC)",
                "temp_c": 15.0,
                "local_dt": london_dt,
                "raw_metar": "METAR EGLC 120950Z 08006KT 9999 -RA FEW015 BKN025 15/12 Q1015",
                "rate_val": 0.4,
                "models_max": {"icon_global": 18.2, "ecmwf_hres": 17.8, "gfs_global": 18.0},
                "avg_peak": 18.0,
                "orderbook": [{"temp": 18, "price_cents": 51.0, "token_id": "tok1", "title": "18°C"}],
            },
            {
                "icao": "LIMC",
                "city_name": "🇮🇹 Милан (LIMC)",
                "temp_c": 16.0,
                "local_dt": london_dt,
                "raw_metar": "METAR LIMC 120950Z 00000KT CAVOK 16/10 Q1018",
                "rate_val": 0.6,
                "models_max": {"icon_global": 20.8, "ecmwf_hres": 18.0, "gfs_global": 21.0, "gem_global": 17.5},
                "avg_peak": 19.5,
                "orderbook": [{"temp": 20, "price_cents": 35.0, "token_id": "tok2", "title": "20°C"}],
            },
            {
                "icao": "LFPB",
                "city_name": "🇫🇷 Париж (LFPB)",
                "temp_c": 12.0,
                "local_dt": london_dt,
                "raw_metar": "METAR LFPB 120950Z 24005KT 9999 BKN030 12/08 Q1016",
                "rate_val": 0.3,
                "models_max": {"icon_global": 18.0, "ecmwf_hres": 17.2, "gfs_global": 18.1},
                "avg_peak": 17.8,
                "orderbook": [{"temp": 18, "price_cents": 13.0, "token_id": "tok3", "title": "18°C"}],
            },
            {
                "icao": "LEMD",
                "city_name": "🇪🇸 Мадрид (LEMD)",
                "temp_c": 16.5,
                "local_dt": london_dt,
                "raw_metar": "METAR LEMD 120950Z 02004KT CAVOK 16/06 Q1020",
                "rate_val": 1.2,
                "models_max": {"icon_global": 26.5, "ecmwf_hres": 26.0, "gfs_global": 25.0},
                "avg_peak": 26.2,
                "orderbook": [{"temp": 27, "price_cents": 58.0, "token_id": "tok4", "title": "27°C"}],
            },
        ]

        # Тест формирования детерминированного отчета
        with patch("gemini_analyzer.is_gemini_configured", return_value=False):
            briefing = await generate_midday_standby_briefing(metrics)
            assert "СТАТУС ПО ТОРГАМ НА 19:00 ХБР" in briefing
            assert "Лондон" in briefing
            assert "Милан" in briefing
            assert "Париж" in briefing
            assert "Мадрид" in briefing
            assert "Почему бот осторожничает:" in briefing
            assert "За чем следит:" in briefing
            assert "Наиболее вероятная точка входа:" in briefing

    asyncio.run(_test())
