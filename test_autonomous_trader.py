"""
Тесты полноценного автономного торгового робота:
1. Расчет рейтинга уверенности кандидатов (calculate_candidate_confidence).
2. Разделение сделок на Реальные и Бумажные (get_user_positions_split).
3. Переключение режимов торговли (REAL <-> PAPER).
4. Расчет детальной статистики и винрейта (get_trading_stats).
5. Автосинхронизация кошелька и апгрейд бумажных сделок в реальные.
"""

import asyncio
import os
import sqlite3
import unittest.mock as mock
from datetime import datetime
import pytest

from auto_scanner import calculate_candidate_confidence
from database import (
    DB_PATH,
    add_position,
    delete_position,
    get_user_positions,
    get_user_positions_split,
    get_trading_mode,
    set_trading_mode,
    close_position_with_exit,
    get_trading_stats,
    save_user_wallet,
    delete_user_wallet,
)


def test_calculate_candidate_confidence_scoring():
    # 1. Сильный консенсус моделей, идеальная цена (35 центов), утро
    models = {
        "icon_global": 22.1,
        "gfs_global": 22.3,
        "ecmwf_hres": 22.0,
    }
    raw_metar = "EGLC 120800Z 24008KT CAVOK 17/10 Q1018"
    score, label, reasons = calculate_candidate_confidence(
        icao="EGLC",
        models=models,
        raw_metar=raw_metar,
        target_strike=22,
        cand_price_cents=35.0,
        rate_val=0.6,
        local_hour=9,
    )
    assert score >= 75.0
    assert "ВЫСОКАЯ УВЕРЕННОСТЬ" in label
    assert any("Консенсус моделей" in r for r in reasons)
    assert any("Оптимальный диапазон" in r for r in reasons)

    # 2. Блокирующий дождь -> резкий штраф к уверенности
    rain_metar = "EGLC 120800Z 24008KT +RA BKN008 15/14 Q1018"
    score_rain, label_rain, reasons_rain = calculate_candidate_confidence(
        icao="EGLC",
        models=models,
        raw_metar=rain_metar,
        target_strike=22,
        cand_price_cents=35.0,
        rate_val=0.1,
        local_hour=11,
    )
    assert score_rain < 50.0
    assert "СКИП" in label_rain
    assert any("осадки" in r.lower() for r in reasons_rain)


def test_trading_mode_switch():
    test_uid = 888123001
    delete_user_wallet(test_uid)

    # Проверяем переключение
    set_trading_mode(test_uid, "REAL")
    assert get_trading_mode(test_uid) == "REAL"

    set_trading_mode(test_uid, "PAPER")
    assert get_trading_mode(test_uid) == "PAPER"


def test_user_positions_split_and_stats():
    test_uid = 888123002
    delete_user_wallet(test_uid)

    # Очищаем старые тестовые позиции
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute("DELETE FROM user_positions WHERE user_id = ?", (test_uid,))
        conn.commit()

    # 1. Добавляем реальную сделку
    id_real = add_position(
        user_id=test_uid,
        icao="EGLC",
        outcomes="22°C",
        target_date="2026-10-12",
        entry_price=35.0,
        shares=3.1,
        token_id="tok_real_1",
        is_paper=0,
        confidence_score=85.0,
    )

    # 2. Добавляем бумажную сделку
    id_paper = add_position(
        user_id=test_uid,
        icao="LIMC",
        outcomes="24°C",
        target_date="2026-10-12",
        entry_price=28.0,
        shares=3.8,
        token_id="tok_paper_1",
        is_paper=1,
        confidence_score=78.0,
    )

    real_list, paper_list = get_user_positions_split(test_uid)
    assert len(real_list) == 1
    assert real_list[0]["id"] == id_real
    assert real_list[0]["is_paper"] == 0

    assert len(paper_list) == 1
    assert paper_list[0]["id"] == id_paper
    assert paper_list[0]["is_paper"] == 1

    # 3. Закрываем реальную сделку в плюс (Тейк-профит)
    close_position_with_exit(id_real, exit_price=60.0, exit_reason="Тейк-профит разгона")

    # 4. Закрываем бумажную сделку в минус (Инвалидация)
    close_position_with_exit(id_paper, exit_price=15.0, exit_reason="Энтальпийный замок")

    # 5. Проверяем расчет статистики
    stats = get_trading_stats(test_uid)
    assert stats["real"]["total"] == 1
    assert stats["real"]["wins"] == 1
    assert stats["real"]["losses"] == 0
    assert stats["real"]["winrate"] == 100.0
    assert stats["real"]["total_pnl_usd"] > 0

    assert stats["paper"]["total"] == 1
    assert stats["paper"]["wins"] == 0
    assert stats["paper"]["losses"] == 1
    assert stats["paper"]["winrate"] == 0.0

    assert len(stats["recent_trades"]) == 2

    # Очистка
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute("DELETE FROM user_positions WHERE user_id = ?", (test_uid,))
        conn.commit()


def test_wallet_sync_upgrades_paper_to_real():
    from handlers import sync_user_polymarket_positions

    test_uid = 888123003
    save_user_wallet(test_uid, "dummy_key", "0x1111111111111111111111111111111111111111", "0x2222222222222222222222222222222222222222")

    # Создаем бумажную позицию по EGLC 20°C
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute("DELETE FROM user_positions WHERE user_id = ?", (test_uid,))
        conn.commit()

    pos_id = add_position(
        user_id=test_uid,
        icao="EGLC",
        outcomes="20°C",
        target_date="2026-10-12",
        entry_price=30.0,
        shares=3.0,
        token_id="tok_mock_20",
        is_paper=1,
    )

    real_pos, paper_pos = get_user_positions_split(test_uid)
    assert len(real_pos) == 0
    assert len(paper_pos) == 1

    # Симулируем ответ от data-api.polymarket.com, будто на кошельке обнаружена купленная позиция
    fake_positions = [
        {
            "title": "Will London highest temperature be 20°C on October 12?",
            "size": 5.0,
            "redeemable": False,
            "endDate": "2026-10-12",
            "avgPrice": 0.32,
            "asset": "tok_mock_20",
        }
    ]

    async def run_sync():
        with mock.patch("aiohttp.ClientSession.get") as mock_get:
            mock_resp = mock.AsyncMock()
            mock_resp.status = 200
            mock_resp.json = mock.AsyncMock(return_value=fake_positions)
            mock_get.return_value.__aenter__.return_value = mock_resp

            await sync_user_polymarket_positions(test_uid)

    asyncio.run(run_sync())

    # Проверяем: сделка должна стать РЕАЛЬНОЙ (is_paper = 0), объем обновлен до 5.0
    real_pos_after, paper_pos_after = get_user_positions_split(test_uid)
    assert len(real_pos_after) == 1
    assert len(paper_pos_after) == 0
    assert real_pos_after[0]["id"] == pos_id
    assert real_pos_after[0]["shares"] == 5.0
    assert real_pos_after[0]["is_paper"] == 0

    # Очистка
    delete_user_wallet(test_uid)
    with sqlite3.connect(DB_PATH) as conn:
        conn.cursor().execute("DELETE FROM user_positions WHERE user_id = ?", (test_uid,))
        conn.commit()
