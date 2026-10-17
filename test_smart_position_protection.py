import asyncio
import time
from datetime import datetime
import zoneinfo
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from database import (
    save_user_public_wallet,
    add_position,
    delete_position,
    get_user_positions,
)
from auto_scanner import (
    check_and_execute_auto_sell,
    _POSITION_DROP_TRACKER,
    build_dynamic_city_block,
)


def test_anti_manipulation_stop_loss_debounce():
    """Проверяет, что стоп-лосс не срабатывает на мгновенную шпильку MM, а требует 45 секунд удержания обвала."""
    async def _test():
        test_uid = 987654331
        save_user_public_wallet(test_uid, "0x43537E8fFA90E0B37c1613d709BC9a1eefd673aa", "0xa7ed88c8d3cc77cbf569257af196c1e1044f3688")

        pos_id = add_position(test_uid, "EGLC", "21°C", "2026-10-15", entry_price=48.0, shares=10.0, token_id="9991")
        pos = {
            "id": pos_id,
            "user_id": test_uid,
            "icao": "EGLC",
            "outcomes": "21°C",
            "target_date": "2026-10-15",
            "entry_price": 48.0,
            "shares": 10.0,
            "peak_price": 48.0,
            "trailing_active": 0,
            "token_id": "9991",
            "last_alert": "",
            "is_paper": 1,
        }

        # Стакан обвалился с 48¢ до 32¢ (-16¢, -33%)
        city_data_drop = {
            "icao": "EGLC",
            "city_name": "🇬🇧 Лондон (EGLC)",
            "temp_c": 18.0,
            "rate_val": 0.4,
            "rate_str": "+0.4°C/ч",
            "rem_hours_str": "3.0 ч",
            "raw_metar": "METAR EGLC 151150Z AUTO 24010KT 18/12 Q1020",
            "local_dt": datetime(2026, 10, 15, 11, 50, tzinfo=zoneinfo.ZoneInfo("Europe/London")),
            "orderbook": [{"temp": 21.0, "price_cents": 32.0}],
        }

        bot = MagicMock()
        bot.send_message = AsyncMock()

        # 1. Первая фиксация обвала (t=0) - выход НЕ должен сработать сразу!
        _POSITION_DROP_TRACKER.clear()
        trigger_1 = await check_and_execute_auto_sell(bot, city_data_drop, pos)
        assert trigger_1 is None, "На первом тике дебаунса стоп-лосс не должен срабатывать мгновенно!"
        assert pos_id in _POSITION_DROP_TRACKER

        # 2. Шпилька выкупилась маркетмейкером обратно до 45¢ (откат всего 3¢)
        city_data_recovered = dict(city_data_drop)
        city_data_recovered["orderbook"] = [{"temp": 21.0, "price_cents": 45.0}]
        trigger_rec = await check_and_execute_auto_sell(bot, city_data_recovered, pos)
        assert trigger_rec is None
        assert pos_id not in _POSITION_DROP_TRACKER, "После выкупа шпильки таймер дебаунса должен сброситься!"

        # 3. Обвал стакана при СОЛНЕЧНОЙ погоде (манипуляция маркетмейкера):
        # Цена упала до 32¢ и держится 50 секунд, но погода ясная и прогрев идет (+0.4°C/ч).
        # Бот НЕ должен выходить и не должен давать маркетмейкеру выбить позицию!
        _POSITION_DROP_TRACKER[pos_id] = time.time() - 50.0
        trigger_sunny = await check_and_execute_auto_sell(bot, city_data_drop, pos)
        assert trigger_sunny is None, "При солнечной погоде и нормальном прогреве просадка стакана должна игнорироваться!"

        # 4. Обвал стакана ПОДТВЕРЖДЕН УХУДШЕНИЕМ ПОГОДЫ (полная остановка прогрева в полдень):
        # Осадков нет, но прогрев затух до нуля (0.0°C/ч при отставании 3.0°C)
        city_data_bad_weather = dict(city_data_drop)
        city_data_bad_weather["rate_val"] = 0.0
        trigger_sustained = await check_and_execute_auto_sell(bot, city_data_bad_weather, pos)
        assert trigger_sustained is not None
        assert "СТОП-ЛОСС ПО СИГНАЛУ СТАКАНА + ПОГОДА" in trigger_sustained
        assert pos_id not in _POSITION_DROP_TRACKER
        bot.send_message.assert_called_once()
        msg_text = bot.send_message.call_args[0][1]
        assert "Итоговый банкролл:" in msg_text

        delete_position(pos_id, test_uid)

    asyncio.run(_test())


def test_trailing_breakeven_lock():
    """Проверяет, что после активации трейлинга возврат цены к точке входа или ниже гарантированно фиксирует безубыток."""
    async def _test():
        test_uid = 987654332
        save_user_public_wallet(test_uid, "0x43537E8fFA90E0B37c1613d709BC9a1eefd673aa", "0xa7ed88c8d3cc77cbf569257af196c1e1044f3688")

        pos_id = add_position(test_uid, "EGLC", "21°C", "2026-10-15", entry_price=48.0, shares=10.0, token_id="9992")
        pos = {
            "id": pos_id,
            "user_id": test_uid,
            "icao": "EGLC",
            "outcomes": "21°C",
            "target_date": "2026-10-15",
            "entry_price": 48.0,
            "shares": 10.0,
            "peak_price": 63.5,  # Был на пике 63.5¢ (+32%)
            "trailing_active": 1,  # Трейлинг активен
            "token_id": "9992",
            "last_alert": "",
            "is_paper": 1,
        }

        # Цена откатила до цены входа (48¢)
        city_data_be = {
            "icao": "EGLC",
            "city_name": "🇬🇧 Лондон (EGLC)",
            "temp_c": 19.5,
            "rate_val": 0.4,
            "rate_str": "+0.4°C/ч",
            "rem_hours_str": "2.5 ч",
            "raw_metar": "METAR EGLC 151220Z AUTO 24010KT 19/12 Q1020",
            "local_dt": datetime(2026, 10, 15, 12, 20, tzinfo=zoneinfo.ZoneInfo("Europe/London")),
            "orderbook": [{"temp": 21.0, "price_cents": 47.0}],  # Ниже или на уровне входа 48¢
        }

        bot = MagicMock()
        bot.send_message = AsyncMock()

        trigger = await check_and_execute_auto_sell(bot, city_data_be, pos)
        assert trigger is not None
        assert "БЕЗУБЫТОК ПОСЛЕ ТРЕЙЛИНГА" in trigger

        delete_position(pos_id, test_uid)

    asyncio.run(_test())


def test_early_overheat_exit_trigger():
    """Проверяет ранний выход при достижении цели до 13:30 LT при активном солнце, когда овершут неизбежен."""
    async def _test():
        test_uid = 987654333
        save_user_public_wallet(test_uid, "0x43537E8fFA90E0B37c1613d709BC9a1eefd673aa", "0xa7ed88c8d3cc77cbf569257af196c1e1044f3688")

        pos_id = add_position(test_uid, "EGLC", "21°C", "2026-10-15", entry_price=48.0, shares=10.0, token_id="9993")
        pos = {
            "id": pos_id,
            "user_id": test_uid,
            "icao": "EGLC",
            "outcomes": "21°C",
            "target_date": "2026-10-15",
            "entry_price": 48.0,
            "shares": 10.0,
            "peak_price": 60.0,
            "trailing_active": 0,
            "token_id": "9993",
            "last_alert": "",
            "is_paper": 1,
        }

        # В 12:15 LT на станции уже ровно 21.0°C (цель), и темп прогрева +0.6°C/ч (солнце продолжает греть)
        city_data_heat = {
            "icao": "EGLC",
            "city_name": "🇬🇧 Лондон (EGLC)",
            "temp_c": 21.0,
            "rate_val": 0.6,
            "rate_str": "+0.6°C/ч",
            "rem_hours_str": "2.5 ч",
            "raw_metar": "METAR EGLC 151215Z AUTO 24010KT 21/12 Q1020",
            "local_dt": datetime(2026, 10, 15, 12, 15, tzinfo=zoneinfo.ZoneInfo("Europe/London")),
            "orderbook": [{"temp": 21.0, "price_cents": 62.0}],
        }

        bot = MagicMock()
        bot.send_message = AsyncMock()

        trigger = await check_and_execute_auto_sell(bot, city_data_heat, pos)
        assert trigger is not None
        assert "РАННИЙ ПЕРЕГРЕВ СТРАЙКА" in trigger

        delete_position(pos_id, test_uid)

    asyncio.run(_test())


def test_build_dynamic_city_block_verdicts():
    """Проверяет отображение вердиктов безубытка, раннего перегрева и стоп-лосса в регулярном дайджесте."""
    user_pos = {
        "outcomes": "21°C",
        "entry_price": 48.0,
        "peak_price": 63.5,
        "trailing_active": 1,
    }
    city_data = {
        "icao": "EGLC",
        "city_name": "🇬🇧 Лондон (EGLC)",
        "temp_c": 21.0,
        "rate_val": 0.5,
        "rate_str": "+0.5°C/ч",
        "rem_hours_str": "2.0 ч",
        "raw_metar": "METAR EGLC 151200Z AUTO 24010KT 21/12 Q1020",
        "local_dt": datetime(2026, 10, 15, 12, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London")),
        "orderbook": [{"temp": 21.0, "price_cents": 47.0}],
        "models_max": {},
        "physics_note": "Тест",
        "peak_str": "22.0°C",
    }
    block = build_dynamic_city_block(city_data, user_pos)
    assert "БЕЗУБЫТОК ПОСЛЕ ТРЕЙЛИНГА" in block
