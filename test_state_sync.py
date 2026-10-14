import pytest
from state_sync import export_state_payload, import_state_payload
from database import init_db, add_position, get_user_positions, set_trading_mode, record_daily_action, get_daily_actions

def test_export_and_import_state_payload():
    init_db()
    test_uid = 555444333
    set_trading_mode(test_uid, "PAPER")
    
    pos_id = add_position(
        user_id=test_uid,
        icao="LTAC",
        outcomes="21°C",
        target_date="2026-10-13",
        entry_price=63.0,
        shares=2.5,
        token_id="tok12345",
        is_paper=1,
        confidence_score=92.0,
    )
    record_daily_action(test_uid, "SCAN", details={"test": "cloud_sync_ok"})
    
    payload = export_state_payload()
    assert isinstance(payload, str)
    assert len(payload) > 50
    
    count = import_state_payload(payload)
    assert count > 0
    
    positions = get_user_positions(test_uid)
    matching = next((p for p in positions if p["icao"] == "LTAC" and p["outcomes"] == "21°C"), None)
    assert matching is not None
    assert matching["entry_price"] == 63.0
    assert matching["confidence_score"] == 92.0
    assert matching["is_paper"] == 1

    actions = get_daily_actions(user_id=test_uid)
    assert any(a["action_type"] == "SCAN" for a in actions)

    # Очистка тестовых данных
    import sqlite3
    from database import DB_PATH
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM user_positions WHERE user_id = ?", (test_uid,))
        conn.execute("DELETE FROM daily_actions_log WHERE user_id = ?", (test_uid,))
        conn.execute("DELETE FROM user_wallets WHERE user_id = ?", (test_uid,))
        conn.commit()

