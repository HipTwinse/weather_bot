import zoneinfo
from datetime import datetime
import pytest

from auto_scanner import (
    get_priority_target,
    _calc_raw_priority_target,
    calculate_candidate_confidence,
)


def test_tokyo_south_wind_warm_advection():
    """Проверяет, что сильный южный/юго-западный ветер в Токио (RJTT) добавляет буст к цели из-за теплой адвекции."""
    models = {
        "icon_global": 25.0,
        "ecmwf_hres": 25.0,
        "gfs_global": 25.0,
        "gem_global": 25.0,
    }
    
    # 1. Обычный слабый ветер
    metar_calm = "METAR RJTT 190000Z 20005KT 9999 FEW030 22/16 Q1022 NOSIG"
    val_calm, reason_calm = _calc_raw_priority_target("RJTT", models, 25.0, metar_calm)
    assert val_calm == 25.0

    # 2. Штормовой южный ветер с порывами (21016G26KT) - заток теплого воздуха
    metar_gale = "METAR RJTT 190000Z 21016G26KT 9999 FEW030 25/16 Q1022 NOSIG"
    val_gale, reason_gale = _calc_raw_priority_target("RJTT", models, 25.0, metar_gale)
    assert val_gale == 26.0
    assert "Южная адвекция" in reason_gale


def test_morning_surge_overshoot_protection():
    """Проверяет, что при слишком высоком утреннем темпе прогрева цель автоматически сдвигается вверх."""
    models = {"icon_global": 25.0, "ecmwf_hres": 25.0}
    local_dt = datetime(2026, 10, 19, 9, 30, tzinfo=zoneinfo.ZoneInfo("Asia/Tokyo"))
    
    # Факт 24.5°C при базовой модели 25.0°C, но темп +1.4°C/ч при 5 часах солнца впереди
    target_val, reason = get_priority_target(
        "RJTT",
        models=models,
        avg_peak=25.0,
        raw_metar="METAR RJTT 190030Z 20015KT 9999 FEW030 24/16 Q1022 NOSIG",
        local_dt=local_dt,
        current_temp=24.5,
        rate_val=1.4,
        heating_cutoff=14.5,
    )
    # 24.5 + 1.4 * 2.5 * 0.7 = 24.5 + 2.45 = ~26.95°C -> 27.0°C!
    assert target_val >= 26.5
    assert "Утренний разгон" in reason


def test_price_ceiling_confidence_scoring():
    """Проверяет, что токен дороже 55¢ штрафуется как перегретый рынок маркетмейкера."""
    models = {"icon_global": 20.0, "ecmwf_hres": 20.0}
    raw_metar = "METAR EGLC 190900Z 22008KT CAVOK 17/10 Q1020"
    
    # Контракт 45¢ (в оптимальном диапазоне 25-50¢)
    score_ok, _, reasons_ok = calculate_candidate_confidence(
        "EGLC", models, raw_metar, target_strike=20, cand_price_cents=45.0, rate_val=0.6, local_hour=10.0
    )
    assert score_ok >= 70.0
    assert any("Оптимальный диапазон входа" in r for r in reasons_ok)

    # Контракт 58¢ (дороже нового потолка 55¢)
    score_high, _, reasons_high = calculate_candidate_confidence(
        "EGLC", models, raw_metar, target_strike=20, cand_price_cents=58.0, rate_val=0.6, local_hour=10.0
    )
    assert any("Рынок перегрет маркетмейкером" in r for r in reasons_high)
    assert score_high < score_ok


def test_seoul_continental_wind_warm_boost():
    """Проверяет, что при ветре с материка (северо-восток 010-130°) и чистом небе (CAVOK) Сеул получает буст +2.0°C."""
    models = {
        "icon_global": 17.0,
        "ecmwf_hres": 17.0,
    }
    
    # 1. Ветер с Желтого моря (28008KT) - морской бриз, базовая цель
    metar_sea = "METAR RKSI 200400Z 28008KT CAVOK 16/11 Q1019 NOSIG"
    val_sea, reason_sea = _calc_raw_priority_target("RKSI", models, 17.0, metar_sea)
    assert val_sea == 17.0
    assert "Бриз Желтого моря" in reason_sea

    # 2. Ветер с материка (04008KT) при CAVOK - прогрев суши +2.0°C
    metar_land = "METAR RKSI 200000Z 04008KT CAVOK 14/11 Q1019 NOSIG"
    val_land, reason_land = _calc_raw_priority_target("RKSI", models, 17.0, metar_land)
    assert val_land == 19.0
    assert "Материковый вынос Сеул" in reason_land
