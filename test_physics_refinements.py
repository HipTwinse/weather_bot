import pytest
from datetime import datetime
import zoneinfo

from auto_scanner import (
    get_cloud_ceiling_ft,
    has_real_low_cloud,
    is_blocking_rain_and_clouds,
    get_strike_for_temp,
    get_priority_target,
    build_dynamic_city_block,
    build_morning_city_block,
)


def test_cloud_ceiling_extraction():
    # OVC110 = 11,000 ft
    assert get_cloud_ceiling_ft("LFPB 280800Z 21008KT 9999 -RA OVC110 15/13 Q1017") == 11000
    # OVC012 = 1,200 ft
    assert get_cloud_ceiling_ft("LFPB 280900Z 20008KT 4000 RA OVC012 14/13 Q1017") == 1200
    # BKN025 = 2,500 ft
    assert get_cloud_ceiling_ft("EGLC 281200Z 09010KT 9999 BKN025 18/10 Q1018") == 2500
    # CAVOK / CLR = None
    assert get_cloud_ceiling_ft("EGLC 281400Z 10008KT CAVOK 19/09 Q1018") is None


def test_has_real_low_cloud():
    # High clouds (11,000 ft) must NOT be considered real low cloud
    assert not has_real_low_cloud("LFPB 280800Z 21008KT 9999 -RA OVC110 15/13 Q1017")
    # Low clouds (1,200 ft) MUST be considered real low cloud
    assert has_real_low_cloud("LFPB 280900Z 20008KT 4000 RA OVC012 14/13 Q1017")
    # Fog / Vertical visibility 200 ft
    assert has_real_low_cloud("EGLL 280600Z 00000KT 0300 FG VV002 08/08 Q1019")
    # CAVOK
    assert not has_real_low_cloud("LFPB 281400Z CAVOK 24/11 Q1017")


def test_paris_28_sep_not_blocked():
    # Paris 28 Sep 08:00Z and 09:00Z METARs with high overcast and light rain/drizzle
    paris_0800 = "LFPB 280800Z 21008KT 9999 -RA OVC110 15/13 Q1017"
    paris_0900 = "LFPB 280900Z 20008KT 9999 -DZ OVC120 16/13 Q1017"

    # Both must NOT trigger blocking rain/clouds
    assert not is_blocking_rain_and_clouds(paris_0800)
    assert not is_blocking_rain_and_clouds(paris_0900)


def test_genuine_blocking_weather():
    # Dense low Stratus
    assert is_blocking_rain_and_clouds("EGLL 280900Z 12005KT 3000 BR OVC004 12/11 Q1018")
    # Heavy rain
    assert is_blocking_rain_and_clouds("LFPG 281200Z 24015KT 3000 +RA BKN015 OVC030 14/13 Q1015")
    # Fog
    assert is_blocking_rain_and_clouds("EGLC 280500Z 00000KT 0200 FG VV001 07/07 Q1020")
    # Rain with low overcast
    assert is_blocking_rain_and_clouds("EGLC 281000Z 18010KT 5000 RA OVC018 15/14 Q1016")


def test_get_strike_for_temp():
    # Fractional part < 0.6 resolves to lower integer strike
    assert get_strike_for_temp(19.0) == 19
    assert get_strike_for_temp(19.3) == 19
    assert get_strike_for_temp(19.49) == 19
    assert get_strike_for_temp(19.5) == 19

    # Fractional part >= 0.6 resolves to next integer strike
    assert get_strike_for_temp(19.6) == 20
    assert get_strike_for_temp(19.8) == 20
    assert get_strike_for_temp(23.4) == 23
    assert get_strike_for_temp(23.7) == 24
    assert get_strike_for_temp(24.0) == 24


def test_london_night_wind_vs_day_wind():
    models = {"icon_global": 19.3, "gfs_global": 19.6, "ecmwf_hres": 18.8}
    night_metar = "EGLC 280020Z 24006KT 9999 FEW048 11/08 Q1019"
    day_metar = "EGLC 281100Z 24012KT 9999 FEW048 16/08 Q1019"

    # Night time (01:20 LT): light SW wind must NOT inflate target with GFS; should stick to ICON (19.3°C)
    night_dt = datetime(2026, 9, 28, 1, 20, tzinfo=zoneinfo.ZoneInfo("Europe/London"))
    target_val_night, note_night = get_priority_target("EGLC", models, 19.2, night_metar, local_dt=night_dt)
    assert target_val_night == 19.3
    assert "ICON" in note_night

    # Day time (12:00 LT) with moderate SW wind (12 kt >= 8 kt): active UHI blends ICON + GFS
    day_dt = datetime(2026, 9, 28, 12, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London"))
    target_val_day, note_day = get_priority_target("EGLC", models, 19.2, day_metar, local_dt=day_dt)
    assert target_val_day == 19.5
    assert "ICON+GFS" in note_day


def test_london_thames_barrier():
    models = {"icon_global": 19.3, "gfs_global": 20.2, "ecmwf_hres": 18.8}
    metar = "EGLC 280950Z 10010KT 9999 FEW045 16/09 Q1018"
    day_dt = datetime(2026, 9, 28, 10, 50, tzinfo=zoneinfo.ZoneInfo("Europe/London"))

    target_val, note = get_priority_target("EGLC", models, 19.4, metar, local_dt=day_dt)
    assert target_val == 19.3
    assert "Барьер Темзы" in note


def test_morning_invalidation_guard_before_noon():
    # User holds 24°C in Paris. At 10:30 LT rate is 0.2°C/h under high clouds OVC110 with -DZ.
    # It must NOT trigger an emergency exit, but give morning pause hold!
    local_dt = datetime(2026, 9, 28, 10, 30, tzinfo=zoneinfo.ZoneInfo("Europe/Paris"))
    city_data = {
        "icao": "LFPB",
        "city_name": "Париж (Ле Бурже)",
        "local_dt": local_dt,
        "temp_c": 16.0,
        "models_max": {"icon_global": 24.2, "gfs_global": 23.6},
        "rate_str": "+0.2°C/ч",
        "rate_val": 0.2,
        "rem_hours_str": "6.5 ч",
        "peak_str": "24.2°C",
        "avg_peak": 24.0,
        "physics_note": "Париж: перистые/высокие облака не блокируют солнечную радиацию.",
        "raw_metar": "LFPB 280830Z 20008KT 9999 -DZ OVC110 16/13 Q1017",
        "orderbook": [
            {"temp": 23, "price_cents": 20.0, "title": "23°C", "yes_price": 0.20},
            {"temp": 24, "price_cents": 42.0, "title": "24°C", "yes_price": 0.42},
            {"temp": 25, "price_cents": 15.0, "title": "25°C", "yes_price": 0.15},
        ],
    }
    user_pos = {
        "outcomes": "24°C",
        "entry_price": 35.0,
        "target_date": "2026-09-28",
    }

    block = build_dynamic_city_block(city_data, user_pos)
    # Must NOT trigger emergency exit
    assert "ЭКСТРЕННЫЙ ВЫХОД" not in block
    assert "УТРЕННЯЯ ПАУЗА" in block


def test_afternoon_invalidation_triggers_on_real_low_cloud():
    # At 13:00 LT rate is 0.1°C/h under dense low Stratus OVC010 and temp is 17°C while target is 24°C (deficit 7°C >= 1.5°C)
    local_dt = datetime(2026, 9, 28, 13, 0, tzinfo=zoneinfo.ZoneInfo("Europe/Paris"))
    city_data = {
        "icao": "LFPB",
        "city_name": "Париж (Ле Бурже)",
        "local_dt": local_dt,
        "temp_c": 17.0,
        "models_max": {"icon_global": 24.2, "gfs_global": 23.6},
        "rate_str": "+0.1°C/ч",
        "rate_val": 0.1,
        "rem_hours_str": "4.0 ч",
        "peak_str": "24.2°C",
        "avg_peak": 24.0,
        "physics_note": "Обложные осадки / плотный Stratus.",
        "raw_metar": "LFPB 281100Z 20008KT 4000 RA OVC010 17/15 Q1017",
        "orderbook": [
            {"temp": 24, "price_cents": 10.0, "title": "24°C", "yes_price": 0.10},
        ],
    }
    user_pos = {
        "outcomes": "24°C",
        "entry_price": 35.0,
        "target_date": "2026-09-28",
    }

    block = build_dynamic_city_block(city_data, user_pos)
    assert "ЭКСТРЕННЫЙ ВЫХОД (ИНВАЛИДАЦИЯ)" in block
