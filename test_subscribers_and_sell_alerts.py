import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from database import (
    register_subscriber,
    get_all_subscribers,
    save_user_public_wallet,
    get_user_wallet,
    add_position,
    get_user_positions,
    delete_position,
)
from auto_scanner import check_and_execute_auto_sell, send_consolidated_digest
from datetime import datetime
import zoneinfo

def test_subscriber_registration():
    test_uid = 987654321
    register_subscriber(test_uid, "test_trader")
    subscribers = get_all_subscribers()
    assert test_uid in subscribers


def test_public_wallet_storage():
    test_uid = 987654322
    evm_addr = "0x43537E8fFA90E0B37c1613d709BC9a1eefd673aa"
    proxy_addr = "0xa7ed88c8d3cc77cbf569257af196c1e1044f3688"
    save_user_public_wallet(test_uid, evm_addr, proxy_addr)
    
    wallet = get_user_wallet(test_uid)
    assert wallet is not None
    assert wallet["wallet_address"] == evm_addr
    assert wallet["proxy_address"] == proxy_addr
    assert wallet["private_key"] == ""  # No private key for public wallet


def test_manual_sell_alert_on_overshoot():
    """Проверяет, что при пробитии цели вверх и отсутствии приватного ключа бот шлет срочный алерт в Telegram."""
    async def _test():
        test_uid = 987654323
        save_user_public_wallet(test_uid, "0x43537E8fFA90E0B37c1613d709BC9a1eefd673aa", "0xa7ed88c8d3cc77cbf569257af196c1e1044f3688")
        
        pos_id = add_position(test_uid, "EGLC", "21°C", "2026-10-04", entry_price=37.0, shares=2.7, token_id="101642")
        pos = {
            "id": pos_id,
            "user_id": test_uid,
            "icao": "EGLC",
            "outcomes": "21°C",
            "target_date": "2026-10-04",
            "entry_price": 37.0,
            "shares": 2.7,
            "peak_price": 37.0,
            "trailing_active": 0,
            "token_id": "101642",
            "last_alert": "",
        }

        # Текущая температура 22.0°C (превысила цель 21°C)
        city_data = {
            "icao": "EGLC",
            "city_name": "🇬🇧 Лондон (EGLC)",
            "temp_c": 22.0,
            "rate_val": 0.5,
            "rate_str": "+0.5°C/ч",
            "rem_hours_str": "Окно закрыто",
            "raw_metar": "METAR EGLC 041450Z AUTO 26007KT 22/10 Q1027",
            "local_dt": datetime(2026, 10, 4, 15, 0, tzinfo=zoneinfo.ZoneInfo("Europe/London")),
            "orderbook": [{"temp": 21.0, "price_cents": 15.0}],
        }

        bot = MagicMock()
        bot.send_message = AsyncMock()

        trigger = await check_and_execute_auto_sell(bot, city_data, pos)
        assert trigger is not None
        assert "ЦЕЛЬ ПРОБИТА ВВЕРХ" in trigger
        bot.send_message.assert_called_once()
        msg_text = bot.send_message.call_args[0][1]
        assert "СРОЧНЫЙ СИГНАЛ НА ВЫХОД" in msg_text
        assert "21°C" in msg_text

        delete_position(pos_id, test_uid)

    asyncio.run(_test())


def test_digest_sent_to_all_subscribers():
    """Проверяет, что send_consolidated_digest рассылает сообщения всем активным подписчикам."""
    async def _test():
        sub_1 = 111001
        sub_2 = 111002
        register_subscriber(sub_1, "sub1")
        register_subscriber(sub_2, "sub2")

        bot = MagicMock()
        bot.send_message = AsyncMock()

        cities_metrics = [
            {
                "icao": "EGLC",
                "city_name": "🇬🇧 Лондон (EGLC)",
                "temp_c": 18.0,
                "rate_val": 0.6,
                "rate_str": "+0.6°C/ч",
                "rem_hours_str": "3.5ч (до 14:30)",
                "peak_str": "21°C",
                "avg_peak": 21.0,
                "models_max": {},
                "physics_note": "Норма",
                "raw_metar": "METAR EGLC",
                "local_dt": datetime.now(zoneinfo.ZoneInfo("Europe/London")),
                "orderbook": [],
            }
        ]

        now_khv = datetime.now(zoneinfo.ZoneInfo("Asia/Vladivostok"))
        with patch("auto_scanner._safe_send_digest", new_callable=AsyncMock) as mock_send:
            await send_consolidated_digest(bot, cities_metrics, is_morning_base=False, now_khv=now_khv)
            called_uids = [call.args[1] for call in mock_send.call_args_list]
            assert sub_1 in called_uids
            assert sub_2 in called_uids

    asyncio.run(_test())
