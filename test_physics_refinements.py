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


def test_seasonal_heating_cutoff():
    from auto_scanner import get_seasonal_heating_cutoff

    # Winter (Dec, Jan, Feb): 13:30 LT (13.5)
    for m in [12, 1, 2]:
        cutoff, label = get_seasonal_heating_cutoff(m)
        assert cutoff == 13.5
        assert label == "Зима"

    # Late Autumn (Oct, Nov): 14:00 LT (14.0)
    for m in [10, 11]:
        cutoff, label = get_seasonal_heating_cutoff(m)
        assert cutoff == 14.0
        assert label == "Глубокая осень"

    # Early Autumn (Sep) / Early Spring (Mar): 14:30 LT (14.5)
    for m in [9, 3]:
        cutoff, label = get_seasonal_heating_cutoff(m)
        assert cutoff == 14.5
        assert label == "Осень/Весна"

    # Mid/Late Spring (Apr, May): 15:30 LT (15.5)
    for m in [4, 5]:
        cutoff, label = get_seasonal_heating_cutoff(m)
        assert cutoff == 15.5
        assert label == "Весна"

    # Summer (Jun, Jul, Aug): 16:30 LT (16.5)
    for m in [6, 7, 8]:
        cutoff, label = get_seasonal_heating_cutoff(m)
        assert cutoff == 16.5
        assert label == "Лето"


def test_seasonal_dynamics_calculation():
    from auto_scanner import _calculate_dynamics, _MORNING_BASELINES

    # 1. September midday (12:00 LT, cutoff 14:30)
    dt_sep_noon = datetime(2026, 9, 29, 12, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London"))
    _MORNING_BASELINES["TEST_SEP"] = {
        "date": "2026-09-29",
        "temp": 18.0,
        "timestamp": dt_sep_noon.timestamp() - 7200,  # 2 hours ago
    }
    rate_str, rem_hours_str, rate_val = _calculate_dynamics(
        "TEST_SEP", 22.0, dt_sep_noon, dt_sep_noon.timestamp()
    )
    assert rate_val == 2.0
    assert "+2.0°C/ч" in rate_str
    assert "2.5 ч" in rem_hours_str
    assert "окно до 14:30 LT" in rem_hours_str

    # 2. September afternoon after cutoff (15:00 LT >= 14:30)
    dt_sep_afternoon = datetime(2026, 9, 29, 15, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London"))
    _, rem_hours_str_afternoon, _ = _calculate_dynamics(
        "TEST_SEP", 22.5, dt_sep_afternoon, dt_sep_afternoon.timestamp()
    )
    assert "Окно закрыто" in rem_hours_str_afternoon
    assert "Осень/Весна" in rem_hours_str_afternoon

    # 3. Winter afternoon (January 14:00 LT >= 13:30 cutoff)
    dt_jan_afternoon = datetime(2026, 1, 15, 14, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London"))
    _MORNING_BASELINES["TEST_JAN"] = {
        "date": "2026-01-15",
        "temp": 4.0,
        "timestamp": dt_jan_afternoon.timestamp() - 7200,
    }
    _, rem_hours_str_jan, _ = _calculate_dynamics(
        "TEST_JAN", 7.0, dt_jan_afternoon, dt_jan_afternoon.timestamp()
    )
    assert "Окно закрыто" in rem_hours_str_jan
    assert "Зима" in rem_hours_str_jan


def test_gemini_prompt_v8_contains_seasonal_law_and_london_barrier():
    from gemini_analyzer import SYSTEM_PROMPT_V8_0

    assert "ЗАКОН 6: СЕЗОННОЕ ОКНО ИНСОЛЯЦИИ" in SYSTEM_PROMPT_V8_0
    assert "ЗАКОН 7: ДНЕВНОЙ ДОЖДЬ" in SYSTEM_PROMPT_V8_0
    assert "ВОСТОЧНЫЙ БАРЬЕР ТЕМЗЫ" in SYSTEM_PROMPT_V8_0
    assert "Heating Cutoff" in SYSTEM_PROMPT_V8_0
    assert "СКИП МАРКЕТА (ОКНО ПРОГРЕВА ЗАКРЫТО)" in SYSTEM_PROMPT_V8_0


def test_city_aware_seasonal_cutoffs():
    from auto_scanner import get_seasonal_heating_cutoff

    # Madrid: later peak due to western longitude / CET offset
    madrid_cutoff_winter, label_w = get_seasonal_heating_cutoff(1, icao="LEMD")
    assert madrid_cutoff_winter == 15.5
    assert "Мадрид" in label_w

    madrid_cutoff_summer, label_s = get_seasonal_heating_cutoff(7, icao="LEMD")
    assert madrid_cutoff_summer == 17.5

    # London: Greenwich meridian early sunset in December
    london_cutoff_dec, label_lon = get_seasonal_heating_cutoff(12, icao="EGLC")
    assert london_cutoff_dec == 13.0
    assert "Лондон" in label_lon


def test_orderbook_execution_rules_and_adverse_selection():
    from auto_scanner import build_dynamic_city_block, build_morning_city_block
    from gemini_analyzer import SYSTEM_PROMPT_V8_0

    # 1. Prompt includes Adverse Selection and Morning vs Daytime rules
    assert "РАЗДЕЛЕНИЕ ВХОДА (УТРО VS ДЕНЬ / ЗАЩИТА ОТ НЕБЛАГОПРИЯТНОГО ОТБОРА)" in SYSTEM_PROMPT_V8_0
    assert "ЗАКОН «ПАДАЮЩЕГО НОЖА» (ADVERSE SELECTION)" in SYSTEM_PROMPT_V8_0

    # 2. Daytime drop of >=8c triggers adverse selection alert for held position
    local_dt = datetime(2026, 9, 28, 11, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London"))
    city_data = {
        "icao": "EGLC",
        "city_name": "Лондон (Сити)",
        "local_dt": local_dt,
        "temp_c": 21.0,
        "models_max": {"icon_global": 24.2, "gfs_global": 23.6},
        "rate_str": "+0.5°C/ч",
        "rate_val": 0.5,
        "rem_hours_str": "3.5 ч",
        "peak_str": "24.2°C",
        "avg_peak": 24.0,
        "physics_note": "Ясно.",
        "raw_metar": "EGLC 281000Z 12005KT CAVOK 21/14 Q1018",
        "orderbook": [
            {"temp": 24, "price_cents": 28.0, "title": "24°C", "yes_price": 0.28},
        ],
    }
    user_pos = {
        "outcomes": "24°C",
        "entry_price": 40.0,  # Dropped from 40c to 28c (-12c drop)
        "target_date": "2026-09-28",
    }
    block = build_dynamic_city_block(city_data, user_pos)
    assert "ТРЕВОГА (ПАДЕНИЕ СТАКАНА: -12¢)" in block
    assert "adverse selection" in block

    # 3. Morning block differentiates entry modes
    morning_city_data = dict(city_data)
    morning_city_data["favorite_candidate"] = {"temp": 24, "price_cents": 35.0, "title": "24°C"}
    # Midday (11:00 LT) -> immediate entry
    morning_block = build_morning_city_block(morning_city_data)
    assert "Сразу по рынку / в упор к Best Ask" in morning_block

    # Early morning (08:00 LT) -> morning limit
    early_dt = datetime(2026, 9, 28, 8, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London"))
    early_city_data = dict(city_data)
    early_city_data["local_dt"] = early_dt
    early_city_data["favorite_candidate"] = {"temp": 24, "price_cents": 35.0, "title": "24°C"}
    early_block = build_morning_city_block(early_city_data)
    assert "Утренняя лимитка в спред" in early_block


def test_auto_trader_and_wallet_management():
    from eth_account import Account
    from clob_trader import validate_private_key, clean_private_key
    from database import (
        save_user_wallet,
        get_user_wallet,
        delete_user_wallet,
        add_position,
        get_user_positions,
        update_position_trailing,
        close_position_with_exit,
    )

    # 1. Invalid private key
    ok, addr, err = validate_private_key("invalid_key_123")
    assert not ok
    assert "Неверный формат" in err

    # 2. Valid generated private key
    acc = Account.create()
    test_pk = acc.key.hex()
    ok, addr, err = validate_private_key(test_pk)
    assert ok
    assert addr.lower() == acc.address.lower()
    assert err == ""

    # 3. Database wallet persistence
    test_uid = 99912345
    save_user_wallet(test_uid, test_pk, addr)
    w = get_user_wallet(test_uid)
    assert w is not None
    assert w["wallet_address"] == addr

    # 4. Position trailing and auto-exit database tracking
    pos_id = add_position(
        user_id=test_uid,
        icao="LIMC",
        outcomes="23°C",
        target_date="2026-10-03",
        entry_price=35.0,
        shares=5.0,
        token_id="123456789",
    )
    positions = get_user_positions(test_uid)
    matching = [p for p in positions if p["id"] == pos_id]
    assert len(matching) == 1
    assert matching[0]["token_id"] == "123456789"
    assert matching[0]["peak_price"] == 35.0

    # Update trailing
    update_position_trailing(pos_id, peak_price=78.0, trailing_active=1)
    positions = get_user_positions(test_uid)
    matching = [p for p in positions if p["id"] == pos_id]
    assert matching[0]["peak_price"] == 78.0
    assert matching[0]["trailing_active"] == 1

    # Close with exit
    close_position_with_exit(pos_id, exit_price=74.0)
    positions = get_user_positions(test_uid)
    matching = [p for p in positions if p["id"] == pos_id]
    assert len(matching) == 0  # Position is now CLOSED

    # Cleanup wallet
    delete_user_wallet(test_uid)
    assert get_user_wallet(test_uid) is None


def test_polymarket_proxy_resolution_and_balance():
    from clob_trader import resolve_polymarket_proxy, get_wallet_collateral_balance
    from database import save_user_wallet, get_user_wallet, delete_user_wallet

    eoa_addr = "0x43537E8fFA90E0B37c1613d709BC9a1eefd673aa"
    proxy = resolve_polymarket_proxy(eoa_addr)
    assert proxy is not None
    assert proxy.lower() == "0xa7ed88c8d3cc77cbf569257af196c1e1044f3688".lower()

    # Balance check on proxy
    bal = get_wallet_collateral_balance(wallet_address=eoa_addr, proxy_address=proxy)
    assert bal > 0.0

    # Auto-resolution in get_user_wallet
    test_uid = 99999111
    delete_user_wallet(test_uid)
    save_user_wallet(test_uid, "dummy_key", eoa_addr)
    loaded = get_user_wallet(test_uid)
    assert loaded is not None
    assert loaded["proxy_address"].lower() == proxy.lower()
    assert loaded["signature_type"] == 1
    delete_user_wallet(test_uid)




