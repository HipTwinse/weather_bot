import pytest
import sqlite3
import json
import zlib
import base64

from database import (
    DEFAULT_PAPER_DEPOSIT,
    get_paper_balance,
    set_paper_deposit,
    calculate_risk_managed_order_size,
    init_db,
    add_position,
    close_position_with_exit,
    DB_PATH,
)
from state_sync import export_state_payload, import_state_payload


def test_default_paper_deposit_constant():
    assert DEFAULT_PAPER_DEPOSIT == 50.0


def test_calculate_risk_managed_order_size():
    # Bankroll = $50.0
    # Conf 95 -> 10% = $5.00
    res_high = calculate_risk_managed_order_size(
        balance=50.0,
        conf_score=95,
        price_cents=25.0,
        max_portfolio_exposure_pct=0.50,
        open_positions_allocated_usd=0.0
    )
    assert res_high["allowed"] is True
    assert res_high["risk_pct"] == 10.0
    assert res_high["cost_usd"] == 5.0
    assert res_high["shares"] == 20.0

    # Conf 85 -> 7% = $3.50
    res_med = calculate_risk_managed_order_size(
        balance=50.0,
        conf_score=85,
        price_cents=50.0,
        max_portfolio_exposure_pct=0.50,
        open_positions_allocated_usd=0.0
    )
    assert res_med["allowed"] is True
    assert res_med["risk_pct"] == 7.0
    assert res_med["cost_usd"] == 3.50
    assert res_med["shares"] == 7.0

    # Conf 75 -> 5% = $2.50
    res_low = calculate_risk_managed_order_size(
        balance=50.0,
        conf_score=75,
        price_cents=20.0,
        max_portfolio_exposure_pct=0.50,
        open_positions_allocated_usd=0.0
    )
    assert res_low["allowed"] is True
    assert res_low["risk_pct"] == 5.0
    assert res_low["cost_usd"] == 2.50
    assert res_low["shares"] == 12.5

    # Exposure cap test: bankroll $50, max cap 50% ($25). Already allocated $24.
    # Allowed remaining is $1.00, which is below min $1.08 -> allowed should be False
    res_capped = calculate_risk_managed_order_size(
        balance=50.0,
        conf_score=95,
        price_cents=25.0,
        max_portfolio_exposure_pct=0.50,
        open_positions_allocated_usd=24.0
    )
    assert res_capped["allowed"] is False
    assert res_capped["cost_usd"] == 0.0
    assert res_capped["shares"] == 0.0


def test_paper_balance_calculation_and_storage():
    init_db()
    user_id = 999999123
    
    # Clean any prior state for this test user
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM bot_state WHERE key IN (?, ?, ?)", (
            f"paper_deposit_{user_id}", f"paper_equity_{user_id}", f"paper_closed_pnl_{user_id}"
        ))
        conn.execute("DELETE FROM user_positions WHERE user_id = ?", (user_id,))
        conn.commit()

    bal = get_paper_balance(user_id)
    assert bal["initial_deposit"] == 50.0
    assert bal["closed_pnl_usd"] == 0.0
    assert bal["open_allocated_usd"] == 0.0
    assert bal["total_equity"] == 50.0
    assert bal["free_balance"] == 50.0
    assert bal["roi_percent"] == 0.0

    # Test set_paper_deposit
    set_paper_deposit(user_id, 100.0)
    bal_custom = get_paper_balance(user_id)
    assert bal_custom["initial_deposit"] == 100.0
    assert bal_custom["total_equity"] == 100.0
    assert bal_custom["free_balance"] == 100.0

    # Cleanup
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM bot_state WHERE key IN (?, ?, ?)", (
            f"paper_deposit_{user_id}", f"paper_equity_{user_id}", f"paper_closed_pnl_{user_id}"
        ))
        conn.commit()


def test_paper_equity_persistence_after_pruning():
    """Проверяет, что прибыль не сгорает даже если старые сделки удалены из локальной таблицы."""
    init_db()
    user_id = 999999888
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM bot_state WHERE key IN (?, ?, ?)", (
            f"paper_deposit_{user_id}", f"paper_equity_{user_id}", f"paper_closed_pnl_{user_id}"
        ))
        conn.execute("DELETE FROM user_positions WHERE user_id = ?", (user_id,))
        conn.commit()

    # Стартовый банкролл $50
    bal0 = get_paper_balance(user_id)
    assert bal0["total_equity"] == 50.0

    # Открываем бумажную сделку: 10 акций по 30 центов ($3.00)
    pos_id = add_position(
        user_id=user_id,
        icao="LIML",
        outcomes="18°C",
        target_date="2026-10-21",
        entry_price=30.0,
        shares=10.0,
        token_id="tok123",
        is_paper=1,
        confidence_score=85.0
    )

    # Закрываем сделку в плюс: выход 50 центов (+20 центов * 10 акций = +$2.00)
    close_position_with_exit(pos_id, exit_price=50.0, exit_reason="TARGET_HIT")

    bal1 = get_paper_balance(user_id)
    assert bal1["total_equity"] == 52.0
    assert bal1["closed_pnl_usd"] == 2.0
    assert bal1["roi_percent"] == 4.0

    # Имитируем очистку/урезание старых сделок из user_positions (как при рестарте Render)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM user_positions WHERE id = ?", (pos_id,))
        conn.commit()

    # Баланс ДОЛЖЕН сохраниться благодаря bot_state
    bal2 = get_paper_balance(user_id)
    assert bal2["total_equity"] == 52.0
    assert bal2["closed_pnl_usd"] == 2.0

    # Cleanup
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM bot_state WHERE key IN (?, ?, ?)", (
            f"paper_deposit_{user_id}", f"paper_equity_{user_id}", f"paper_closed_pnl_{user_id}"
        ))
        conn.commit()


def test_state_sync_includes_bot_state():
    init_db()
    user_id = 888888456
    set_paper_deposit(user_id, 75.0)

    payload = export_state_payload()
    assert isinstance(payload, str)
    decompressed = json.loads(zlib.decompress(base64.b64decode(payload.encode("ascii"))).decode("utf-8"))
    assert "bot_state" in decompressed
    assert any(item["key"] == f"paper_deposit_{user_id}" and item["value"] == "75.0" for item in decompressed["bot_state"])

    count = import_state_payload(payload)
    assert count > 0

    assert get_paper_balance(user_id)["initial_deposit"] == 75.0

    # Cleanup
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM bot_state WHERE key = ?", (f"paper_deposit_{user_id}",))
        conn.commit()
