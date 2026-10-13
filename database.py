"""
Модуль локальной базы данных SQLite для хранения открытых торговых позиций.
Работает полностью автономно, безопасно и бесплатно (Zero-Cost).
"""

import sqlite3
from pathlib import Path
from typing import List, Dict, Any, Optional

DB_PATH = Path(__file__).resolve().parent / "positions.db"


def init_db() -> None:
    """Инициализирует таблицу открытых позиций при первом запуске с поддержкой миграций."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                icao TEXT NOT NULL,
                outcomes TEXT NOT NULL,
                target_date TEXT NOT NULL,
                entry_price REAL DEFAULT 0.0,
                status TEXT DEFAULT 'OPEN',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Безопасная неразрушающая миграция колонок, если таблица была создана ранее
        cursor.execute("PRAGMA table_info(user_positions)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if "entry_price" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN entry_price REAL DEFAULT 0.0")
        if "status" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN status TEXT DEFAULT 'OPEN'")
        if "shares" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN shares REAL DEFAULT 0.0")
        if "peak_price" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN peak_price REAL DEFAULT 0.0")
        if "trailing_active" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN trailing_active INTEGER DEFAULT 0")
        if "token_id" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN token_id TEXT DEFAULT ''")
        if "last_alert" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN last_alert TEXT DEFAULT ''")
        if "is_paper" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN is_paper INTEGER DEFAULT 0")
        if "confidence_score" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN confidence_score REAL DEFAULT 0.0")
        if "exit_price" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN exit_price REAL DEFAULT 0.0")
        if "exit_reason" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN exit_reason TEXT DEFAULT ''")
        if "pnl_cents" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN pnl_cents REAL DEFAULT 0.0")
        if "pnl_percent" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN pnl_percent REAL DEFAULT 0.0")
        if "closed_at" not in existing_cols:
            cursor.execute("ALTER TABLE user_positions ADD COLUMN closed_at TIMESTAMP")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                private_key TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                proxy_address TEXT DEFAULT '',
                signature_type INTEGER DEFAULT 1,
                trading_mode TEXT DEFAULT 'PAPER',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("PRAGMA table_info(user_wallets)")
        wallet_cols = {row[1] for row in cursor.fetchall()}
        if "proxy_address" not in wallet_cols:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN proxy_address TEXT DEFAULT ''")
        if "signature_type" not in wallet_cols:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN signature_type INTEGER DEFAULT 1")
        if "trading_mode" not in wallet_cols:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN trading_mode TEXT DEFAULT 'PAPER'")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_subscribers (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_snapshots (
                user_id INTEGER NOT NULL,
                icao TEXT NOT NULL,
                target_date TEXT NOT NULL,
                baseline_json TEXT,
                latest_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, icao, target_date)
            )
        """)
        conn.commit()

        # Автоматическая регистрация администратора из config, если задан
        try:
            import config
            admin_id = getattr(config, "ADMIN_CHAT_ID", None)
            if admin_id:
                cursor.execute("""
                    INSERT INTO bot_subscribers (user_id, username, is_active)
                    VALUES (?, 'admin', 1)
                    ON CONFLICT(user_id) DO UPDATE SET is_active = 1
                """, (int(admin_id),))
                wallet_key = getattr(config, "WALLET_PRIVATE_KEY", None)
                wallet_addr = getattr(config, "WALLET_ADDRESS", None)
                if wallet_key:
                    try:
                        from clob_trader import validate_private_key, resolve_polymarket_proxy
                        valid, derived_addr, _ = validate_private_key(wallet_key)
                        if valid:
                            use_addr = derived_addr or (wallet_addr or "")
                            proxy = resolve_polymarket_proxy(use_addr) or use_addr
                            sig = 1 if (proxy and proxy.lower() != use_addr.lower()) else 0
                            cursor.execute("""
                                INSERT INTO user_wallets (user_id, private_key, wallet_address, proxy_address, signature_type, updated_at)
                                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                                ON CONFLICT(user_id) DO UPDATE SET
                                    private_key = excluded.private_key,
                                    wallet_address = excluded.wallet_address,
                                    proxy_address = excluded.proxy_address,
                                    signature_type = excluded.signature_type,
                                    updated_at = CURRENT_TIMESTAMP
                            """, (int(admin_id), wallet_key, use_addr, proxy, sig))
                    except Exception:
                        pass
                elif wallet_addr:
                    cursor.execute("""
                        INSERT INTO user_wallets (user_id, private_key, wallet_address, proxy_address, signature_type, updated_at)
                        VALUES (?, '', ?, '', 1, CURRENT_TIMESTAMP)
                        ON CONFLICT(user_id) DO NOTHING
                    """, (int(admin_id), str(wallet_addr).strip()))
                conn.commit()
        except Exception:
            pass


def add_position(
    user_id: int,
    icao: str,
    outcomes: str,
    target_date: str,
    entry_price: float = 0.0,
    shares: float = 0.0,
    token_id: str = "",
    is_paper: int = 0,
    confidence_score: float = 0.0,
) -> int:
    """Добавляет новую сделку на радарный контроль (реальную или бумажную)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_positions (
                user_id, icao, outcomes, target_date, entry_price, status,
                shares, peak_price, trailing_active, token_id, is_paper, confidence_score
            )
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, 0, ?, ?, ?)
        """, (
            user_id,
            icao.strip().upper(),
            outcomes.strip(),
            target_date.strip(),
            float(entry_price),
            float(shares),
            float(entry_price),
            token_id.strip(),
            int(is_paper),
            float(confidence_score),
        ))
        conn.commit()
        return cursor.lastrowid


def get_user_positions(user_id: int) -> List[Dict[str, Any]]:
    """Возвращает список открытых позиций конкретного пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, icao, outcomes, target_date, entry_price, status,
                   shares, peak_price, trailing_active, token_id, last_alert,
                   COALESCE(is_paper, 0) AS is_paper, COALESCE(confidence_score, 0.0) AS confidence_score,
                   exit_price, exit_reason, pnl_cents, pnl_percent, closed_at, created_at
            FROM user_positions
            WHERE user_id = ? AND (status = 'OPEN' OR status IS NULL)
            ORDER BY id DESC
        """, (user_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_user_positions_split(user_id: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Возвращает кортеж (реальные_сделки, бумажные_сделки) для конкретного пользователя."""
    all_open = get_user_positions(user_id)
    real = [p for p in all_open if not p.get("is_paper")]
    paper = [p for p in all_open if p.get("is_paper")]
    return real, paper


def get_position_by_id(pos_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает сделку по ее идентификатору."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, icao, outcomes, target_date, entry_price, status,
                   shares, peak_price, trailing_active, token_id, last_alert,
                   COALESCE(is_paper, 0) AS is_paper, COALESCE(confidence_score, 0.0) AS confidence_score,
                   exit_price, exit_reason, pnl_cents, pnl_percent, closed_at, created_at
            FROM user_positions WHERE id = ?
        """, (pos_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def is_position_closed_today(user_id: int, icao: str, outcomes: Optional[str] = None, target_date: str = "") -> bool:
    """Проверяет, была ли данная сделка (или любая сделка по городу на эту дату) уже закрыта сегодня."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        if outcomes:
            cursor.execute("""
                SELECT 1 FROM user_positions
                WHERE user_id = ? AND icao = ? AND outcomes = ? AND target_date = ? AND status = 'CLOSED'
                LIMIT 1
            """, (user_id, icao.strip().upper(), outcomes.strip(), target_date.strip()))
        else:
            cursor.execute("""
                SELECT 1 FROM user_positions
                WHERE user_id = ? AND icao = ? AND target_date = ? AND status = 'CLOSED'
                LIMIT 1
            """, (user_id, icao.strip().upper(), target_date.strip()))
        return cursor.fetchone() is not None


def get_all_active_positions() -> List[Dict[str, Any]]:
    """Возвращает все активные позиции (status = 'OPEN') для фонового сканирования радаром."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, icao, outcomes, target_date, entry_price, status,
                   shares, peak_price, trailing_active, token_id, last_alert,
                   COALESCE(is_paper, 0) AS is_paper, COALESCE(confidence_score, 0.0) AS confidence_score,
                   exit_price, exit_reason, pnl_cents, pnl_percent, closed_at, created_at
            FROM user_positions
            WHERE status = 'OPEN' OR status IS NULL
        """)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def _ensure_wallet_columns(cursor: sqlite3.Cursor) -> None:
    """Гарантирует наличие колонок proxy_address и signature_type в user_wallets."""
    try:
        cursor.execute("PRAGMA table_info(user_wallets)")
        cols = {row[1] for row in cursor.fetchall()}
        if cols:
            if "proxy_address" not in cols:
                cursor.execute("ALTER TABLE user_wallets ADD COLUMN proxy_address TEXT DEFAULT ''")
            if "signature_type" not in cols:
                cursor.execute("ALTER TABLE user_wallets ADD COLUMN signature_type INTEGER DEFAULT 1")
    except Exception:
        pass


def save_user_wallet(
    user_id: int,
    private_key: str,
    wallet_address: str,
    proxy_address: str = "",
    signature_type: int = 1,
) -> None:
    """Сохраняет приватный ключ, адрес подписанта и адрес торгового прокси Polymarket."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        _ensure_wallet_columns(cursor)
        cursor.execute("""
            INSERT INTO user_wallets (user_id, private_key, wallet_address, proxy_address, signature_type, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                private_key = excluded.private_key,
                wallet_address = excluded.wallet_address,
                proxy_address = excluded.proxy_address,
                signature_type = excluded.signature_type,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, private_key.strip(), wallet_address.strip(), (proxy_address or "").strip(), int(signature_type)))
        conn.commit()


def get_user_wallet(user_id: int) -> Optional[Dict[str, Any]]:
    """Получает сохраненный кошелек пользователя с автоматическим определением proxyWallet при необходимости."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        _ensure_wallet_columns(cursor)
        cursor.execute("""
            SELECT user_id, private_key, wallet_address,
                   COALESCE(proxy_address, '') AS proxy_address,
                   COALESCE(signature_type, 1) AS signature_type
            FROM user_wallets WHERE user_id = ?
        """, (user_id,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        # Если proxy_address еще не заполнен, динамически разрешаем его через Gamma API
        if not res.get("proxy_address") and res.get("wallet_address"):
            try:
                from clob_trader import resolve_polymarket_proxy
                proxy = resolve_polymarket_proxy(res["wallet_address"])
                if proxy:
                    res["proxy_address"] = proxy
                    res["signature_type"] = 1
                    cursor.execute("""
                        UPDATE user_wallets
                        SET proxy_address = ?, signature_type = 1
                        WHERE user_id = ?
                    """, (proxy, user_id))
                    conn.commit()
            except Exception:
                pass
        return res


def delete_user_wallet(user_id: int) -> bool:
    """Удаляет ключ кошелька пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_wallets WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


def update_position_trailing(position_id: int, peak_price: float, trailing_active: int = 1) -> None:
    """Обновляет зафиксированный пик цены и статус трейлинг-замка."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_positions
            SET peak_price = ?, trailing_active = ?
            WHERE id = ?
        """, (float(peak_price), int(trailing_active), position_id))
        conn.commit()


def close_position_with_exit(position_id: int, exit_price: float = 0.0, exit_reason: str = "") -> None:
    """Закрывает сделку в базе данных при исполнении автопродажи с фиксацией PnL."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT entry_price, shares FROM user_positions WHERE id = ?", (position_id,))
        row = cursor.fetchone()
        entry_price = float(row[0] or 0.0) if row else 0.0
        pnl_cents = float(exit_price) - entry_price if entry_price > 0 else 0.0
        pnl_percent = (pnl_cents / entry_price * 100.0) if entry_price > 0 else 0.0
        cursor.execute("""
            UPDATE user_positions
            SET status = 'CLOSED',
                exit_price = ?,
                exit_reason = ?,
                pnl_cents = ?,
                pnl_percent = ?,
                closed_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (float(exit_price), str(exit_reason or ""), round(pnl_cents, 2), round(pnl_percent, 1), position_id))
        conn.commit()


def get_trading_mode(user_id: int) -> str:
    """Возвращает текущий режим торговли: 'REAL' (боевой) или 'PAPER' (тестовый на бумаге)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT trading_mode FROM user_wallets WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row and row[0]:
                return str(row[0]).strip().upper()
        except Exception:
            pass
    state = get_bot_state(f"trading_mode_{user_id}")
    return state.upper() if state else "PAPER"


def set_trading_mode(user_id: int, mode: str) -> None:
    """Переключает режим торговли пользователя ('REAL' или 'PAPER')."""
    clean_mode = "REAL" if mode.strip().upper() == "REAL" else "PAPER"
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                UPDATE user_wallets
                SET trading_mode = ?
                WHERE user_id = ?
            """, (clean_mode, user_id))
            conn.commit()
        except Exception:
            pass
    set_bot_state(f"trading_mode_{user_id}", clean_mode)


def get_trading_stats(user_id: int) -> Dict[str, Any]:
    """Возвращает подробную статистику сделок (реальных и бумажных)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, icao, outcomes, target_date, entry_price, exit_price, exit_reason,
                   pnl_cents, pnl_percent, shares, COALESCE(is_paper, 0) AS is_paper, status, closed_at
            FROM user_positions
            WHERE user_id = ? AND status = 'CLOSED'
            ORDER BY id DESC
        """, (user_id,))
        rows = [dict(r) for r in cursor.fetchall()]

        real_closed = [r for r in rows if not r.get("is_paper")]
        paper_closed = [r for r in rows if r.get("is_paper")]

        def calc_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
            if not trades:
                return {
                    "total": 0, "wins": 0, "losses": 0, "winrate": 0.0,
                    "total_pnl_usd": 0.0, "avg_win_cents": 0.0, "avg_loss_cents": 0.0
                }
            wins = [t for t in trades if (t.get("pnl_cents") or 0.0) > 0]
            losses = [t for t in trades if (t.get("pnl_cents") or 0.0) <= 0]
            total_pnl_cents = sum((t.get("pnl_cents") or 0.0) * (float(t.get("shares") or 1.0)) for t in trades)
            winrate = round(len(wins) / len(trades) * 100.0, 1)
            avg_win = round(sum(t.get("pnl_cents") or 0.0 for t in wins) / len(wins), 1) if wins else 0.0
            avg_loss = round(sum(t.get("pnl_cents") or 0.0 for t in losses) / len(losses), 1) if losses else 0.0
            return {
                "total": len(trades),
                "wins": len(wins),
                "losses": len(losses),
                "winrate": winrate,
                "total_pnl_usd": round(total_pnl_cents / 100.0, 2),
                "avg_win_cents": avg_win,
                "avg_loss_cents": avg_loss,
            }

        return {
            "real": calc_metrics(real_closed),
            "paper": calc_metrics(paper_closed),
            "recent": rows[:10],
            "recent_trades": rows[:10],
        }


def delete_position(position_id: int, user_id: int) -> bool:
    """Закрывает сделку (переводит в статус CLOSED) в базе данных."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_positions
            SET status = 'CLOSED'
            WHERE id = ? AND user_id = ?
        """, (position_id, user_id))
        conn.commit()
        return cursor.rowcount > 0


def set_bot_state(key: str, value: str) -> None:
    """Сохраняет строковое значение состояния бота в SQLite."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
        """, (key, str(value)))
        conn.commit()


def get_bot_state(key: str) -> Optional[str]:
    """Считывает значение состояния бота из SQLite."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("SELECT value FROM bot_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None


def register_subscriber(user_id: int, username: str = "") -> None:
    """Регистрирует пользователя для получения регулярных 30-минутных дайджестов."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO bot_subscribers (user_id, username, is_active)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET is_active = 1, username = CASE WHEN excluded.username != '' THEN excluded.username ELSE bot_subscribers.username END
        """, (user_id, username or ""))
        conn.commit()


def get_all_subscribers() -> List[int]:
    """Возвращает список user_id всех активных подписчиков на дайджест."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM bot_subscribers WHERE is_active = 1")
        return [row[0] for row in cursor.fetchall()]


def save_user_public_wallet(user_id: int, wallet_address: str, proxy_address: str = "") -> None:
    """Сохраняет публичный адрес кошелька пользователя для мониторинга баланса и сделок."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        _ensure_wallet_columns(cursor)
        cursor.execute("""
            INSERT INTO user_wallets (user_id, private_key, wallet_address, proxy_address, signature_type, updated_at)
            VALUES (?, '', ?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                wallet_address = excluded.wallet_address,
                proxy_address = excluded.proxy_address,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, wallet_address.strip(), (proxy_address or "").strip()))
        conn.commit()


def update_position_alert(pos_id: int, alert_type: str) -> None:
    """Обновляет статус последнего отправленного алерта по позиции."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE user_positions SET last_alert = ? WHERE id = ?", (alert_type, pos_id))
        conn.commit()


def save_daily_snapshot_db(user_id: int, icao: str, target_date: str, snapshot: Dict[str, Any]) -> None:
    """
    Сохраняет дневной срез анализа в базу данных.
    Автоматически фиксирует утренний срез (baseline) и обновляет актуальный.
    """
    import json
    snap_json = json.dumps(snapshot, ensure_ascii=False)
    icao_clean = icao.upper().strip()
    uid = int(user_id or 0)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT baseline_json, latest_json FROM daily_snapshots
            WHERE user_id = ? AND icao = ? AND target_date = ?
        """, (uid, icao_clean, target_date))
        row = cursor.fetchone()

        if not row:
            cursor.execute("""
                INSERT INTO daily_snapshots (user_id, icao, target_date, baseline_json, latest_json, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (uid, icao_clean, target_date, snap_json, snap_json))
        else:
            baseline_json = row[0] or snap_json
            cursor.execute("""
                UPDATE daily_snapshots
                SET baseline_json = ?, latest_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND icao = ? AND target_date = ?
            """, (baseline_json, snap_json, uid, icao_clean, target_date))
        conn.commit()


def get_daily_snapshot_db(user_id: int, icao: str, target_date: str) -> Optional[Dict[str, Any]]:
    """Возвращает последний сохраненный дневной срез анализа из базы данных вместе с утренней базой."""
    import json
    icao_clean = icao.upper().strip()
    uid = int(user_id or 0)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        # 1. Персональный поиск
        cursor.execute("""
            SELECT latest_json, baseline_json FROM daily_snapshots
            WHERE user_id = ? AND icao = ? AND target_date = ?
        """, (uid, icao_clean, target_date))
        row = cursor.fetchone()

        # 2. Поиск общего среза (user_id = 0), если персонального еще нет
        if not row and uid != 0:
            cursor.execute("""
                SELECT latest_json, baseline_json FROM daily_snapshots
                WHERE user_id = 0 AND icao = ? AND target_date = ?
            """, (icao_clean, target_date))
            row = cursor.fetchone()

        if row and row[0]:
            try:
                data = json.loads(row[0])
                if row[1]:
                    try:
                        data["baseline"] = json.loads(row[1])
                    except Exception:
                        pass
                return data
            except Exception:
                return None
        return None


def cleanup_old_daily_snapshots_db(current_date: str) -> None:
    """Сбрасывает историю за предыдущие дни (сброс в конце дня / на новый день)."""
    if not current_date:
        return
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM daily_snapshots WHERE target_date != ?", (current_date,))
        conn.commit()


def clear_all_daily_snapshots_db() -> None:
    """Очищает все дневные срезы (для тестов)."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM daily_snapshots")
        conn.commit()


# -------------------------------------------------------------
# ЖУРНАЛ ДНЕВНЫХ ДЕЙСТВИЙ И АУДИТОРСКИЙ ОТЧЕТ
# -------------------------------------------------------------

def record_daily_action(
    user_id: int,
    action_type: str,
    icao: str = "",
    outcomes: str = "",
    price: float = 0.0,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """Записывает действие или событие робота в журнал дня для последующей выгрузки отчета."""
    import json
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_actions_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action_type TEXT,
                icao TEXT,
                outcomes TEXT,
                price REAL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        det_json = json.dumps(details or {}, ensure_ascii=False)
        cursor.execute("""
            INSERT INTO daily_actions_log (user_id, action_type, icao, outcomes, price, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (int(user_id or 0), str(action_type), str(icao or ""), str(outcomes or ""), float(price or 0.0), det_json))
        conn.commit()


def get_daily_actions(date_str: str = "", user_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """Возвращает все записанные действия за конкретную дату (YYYY-MM-DD по Хабаровску UTC+10)."""
    import json
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_actions_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action_type TEXT,
                icao TEXT,
                outcomes TEXT,
                price REAL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        query = "SELECT id, user_id, action_type, icao, outcomes, price, details, datetime(created_at, '+10 hours') AS local_khv_time FROM daily_actions_log"
        params = []
        conditions = []
        if date_str:
            conditions.append("DATE(created_at, '+10 hours') = ?")
            params.append(date_str)
        if user_id is not None and user_id != 0:
            conditions.append("(user_id = ? OR user_id = 0)")
            params.append(user_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id ASC"
        cursor.execute(query, params)
        rows = [dict(r) for r in cursor.fetchall()]
        for r in rows:
            try:
                r["details"] = json.loads(r["details"]) if r.get("details") else {}
            except Exception:
                r["details"] = {}
        return rows


def generate_daily_audit_report(date_str: str = "", user_id: int = 0) -> Tuple[str, str]:
    """
    Формирует полный структурированный отчет обо всех действиях робота за день (дата по Хабаровску YYYY-MM-DD).
    Возвращает (текст_отчета, имя_файла).
    """
    from datetime import datetime
    import zoneinfo

    khv_tz = zoneinfo.ZoneInfo("Asia/Vladivostok")
    if not date_str:
        date_str = datetime.now(khv_tz).strftime("%Y-%m-%d")

    actions = get_daily_actions(date_str, user_id=user_id if user_id else None)
    stats = get_trading_stats(user_id) if user_id else {"real": {}, "paper": {}, "recent": []}
    real_s = stats.get("real", {})
    paper_s = stats.get("paper", {})
    wallet = get_user_wallet(user_id) if user_id else None
    mode = get_trading_mode(user_id) if user_id else "PAPER"
    mode_label = "БОЕВОЙ (Деньги + бумага)" if mode == "REAL" else "ТЕСТОВЫЙ (Только бумага)"

    wallet_str = wallet.get("proxy_address") or wallet.get("wallet_address", "Не подключен") if wallet else "Не подключен"

    scans_count = sum(1 for a in actions if a["action_type"] == "SCAN")
    buys_real = [a for a in actions if a["action_type"] == "BUY_REAL"]
    buys_paper = [a for a in actions if a["action_type"] == "BUY_PAPER"]
    sells_real = [a for a in actions if a["action_type"] == "SELL_REAL"]
    sells_paper = [a for a in actions if a["action_type"] == "SELL_PAPER"]
    skips_count = sum(1 for a in actions if a["action_type"] == "SKIP")
    errors = [a for a in actions if a["action_type"] == "ERROR"]

    today_trades = [
        r for r in stats.get("recent_trades", [])
        if str(r.get("closed_at") or "").startswith(date_str) or r.get("target_date") == date_str
    ]
    today_pnl_usd = sum((float(t.get("pnl_cents") or 0.0) * float(t.get("shares") or 1.0)) / 100.0 for t in today_trades)
    today_wins = sum(1 for t in today_trades if float(t.get("pnl_cents") or 0.0) > 0)
    today_losses = sum(1 for t in today_trades if float(t.get("pnl_cents") or 0.0) <= 0)
    sign_today = "+" if today_pnl_usd >= 0 else ""

    lines = [
        "=" * 80,
        " ДНЕВНОЙ АУДИТОРСКИЙ ОТЧЕТ ДЕЙСТВИЙ РОБОТА WEATHER ALPHA ENGINE",
        f" Дата (Хабаровск UTC+10): {date_str}",
        f" Торговый режим: {mode_label}",
        f" Торговый сейф/кошелек: {wallet_str}",
        f" Торговое окно: 10:00 - 24:00 ХБР (Ночью бот спит)",
        "=" * 80,
        "",
        "📊 1. СВОДНЫЕ РЕЗУЛЬТАТЫ ДНЯ:",
        f" • Всего циклов сканирования: {scans_count}",
        f" • Куплено реальных контрактов: {len(buys_real)}",
        f" • Оформлено бумажных тестов: {len(buys_paper)}",
        f" • Закрыто реальных сделок: {len(sells_real)}",
        f" • Закрыто бумажных тестов: {len(sells_paper)}",
        f" • Дневной PnL (закрытые сегодня): {sign_today}${today_pnl_usd:.2f} USDC (Побед: {today_wins} | Потерь: {today_losses})",
        f" • Отклонено кандидатов (фильтры/скип): {skips_count}",
        f" • Ошибок биржи/отказов: {len(errors)}",
        "",
        "💰 2. ФИНАНСОВЫЕ ПОКАЗАТЕЛИ (ПО ВСЕЙ ИСТОРИИ):",
        f" • Реальный винрейт: {real_s.get('winrate', 0.0)}% (Побед: {real_s.get('wins', 0)} / Потерь: {real_s.get('losses', 0)})",
        f" • Реальный PnL: ${real_s.get('total_pnl_usd', 0.0):.2f} USDC",
        f" • Бумажный винрейт: {paper_s.get('winrate', 0.0)}% (Побед: {paper_s.get('wins', 0)} / Потерь: {paper_s.get('losses', 0)})",
        f" • Бумажный PnL: ${paper_s.get('total_pnl_usd', 0.0):.2f} USDC",
        "",
        "=" * 80,
        "📋 3. ПОЛНАЯ ХРОНОЛОГИЯ ВСЕХ ДЕЙСТВИЙ ЗА ДЕНЬ (ВРЕМЯ ХАБАРОВСК):",
        "=" * 80,
    ]

    if not actions:
        lines.append(" За указанный день действий в журнале не зафиксировано.")
    else:
        for idx, act in enumerate(actions, 1):
            time_khv = act.get("local_khv_time", "")
            atype = act.get("action_type", "")
            icao = act.get("icao", "")
            outcomes = act.get("outcomes", "")
            price = float(act.get("price") or 0.0)
            det = act.get("details", {})

            time_label = time_khv.split()[1][:5] if " " in time_khv else time_khv

            if atype == "SCAN":
                lines.append(f"\n[{time_label} ХБР] 🔍 СКАНИРОВАНИЕ РЫНКОВ")
                lines.append(f"   Города: {det.get('cities', 'EGLC, LFPB, LIMC, LEMD')}")
                if det.get("note"):
                    lines.append(f"   Инфо: {det.get('note')}")

            elif atype == "BUY_REAL":
                lines.append(f"\n[{time_label} ХБР] ⚡ АВТОПОКУПКА (РЕАЛЬНАЯ СДЕЛКА НА POLYMARKET)")
                lines.append(f"   Локация: {icao} | Исход: {outcomes} | Цена входа: {price:.0f}¢")
                lines.append(f"   Сумма ордера: ${det.get('amount_usd', 1.08):.2f} USDC (контрактов: ~{det.get('shares', 0):.2f})")
                lines.append(f"   Рейтинг надежности: {det.get('conf_score', 0)}/100 ({det.get('conf_label', '')})")
                if det.get("ai_verdict"):
                    lines.append(f"   🧠 Вердикт нейросети (Gemini AI): {det.get('ai_verdict')}")
                reasons = det.get("reasons", [])
                if reasons:
                    lines.append("   Факторы входа:")
                    for r in reasons:
                        lines.append(f"     • {r}")

            elif atype == "BUY_PAPER":
                lines.append(f"\n[{time_label} ХБР] 📝 АВТОПОКУПКА (БУМАЖНЫЙ ТЕСТ)")
                lines.append(f"   Локация: {icao} | Исход: {outcomes} | Цена входа: {price:.0f}¢")
                lines.append(f"   Рейтинг надежности: {det.get('conf_score', 0)}/100 ({det.get('conf_label', '')})")
                lines.append(f"   Причина теста: {det.get('paper_reason', 'Тестовый режим или вторичный кандидат')}")
                if det.get("ai_verdict"):
                    lines.append(f"   🧠 Вердикт нейросети (Gemini AI): {det.get('ai_verdict')}")
                reasons = det.get("reasons", [])
                if reasons:
                    lines.append("   Факторы:")
                    for r in reasons:
                        lines.append(f"     • {r}")

            elif atype == "SELL_REAL":
                lines.append(f"\n[{time_label} ХБР] ⚡ АВТОПРОДАЖА (РЕАЛЬНОЕ ЗАКРЫТИЕ НА БИРЖЕ)")
                lines.append(f"   Локация: {icao} | Исход: {outcomes} | Цена выхода: {price:.0f}¢")
                lines.append(f"   Причина закрытия: {det.get('reason', 'Плановый выход')}")
                lines.append(f"   Результат сделки: PnL {det.get('pnl_str', '0%')} ({det.get('pnl_usd', '$0.00')})")

            elif atype == "SELL_PAPER":
                lines.append(f"\n[{time_label} ХБР] 📝 ЗАКРЫТИЕ БУМАЖНОГО ТЕСТА")
                lines.append(f"   Локация: {icao} | Исход: {outcomes} | Цена выхода: {price:.0f}¢")
                lines.append(f"   Причина закрытия: {det.get('reason', 'Плановый выход')}")
                lines.append(f"   Результат: PnL {det.get('pnl_str', '0%')}")

            elif atype == "SKIP":
                outcomes_str = f" ({outcomes})" if outcomes else ""
                lines.append(f"[{time_label} ХБР] ⛔ ПРОПУСК: {icao}{outcomes_str} — {det.get('reason', 'Низкая уверенность')}")

            elif atype == "ERROR":
                lines.append(f"\n[{time_label} ХБР] ⚠️ ОШИБКА БИРЖИ:")
                lines.append(f"   Локация: {icao} | Исход: {outcomes}")
                lines.append(f"   Описание: {det.get('error', 'Неизвестная ошибка')}")

    lines.append("\n" + "=" * 80)
    lines.append(f" Конец отчета. Сформировано автоматически Weather Alpha Engine в {datetime.now(khv_tz).strftime('%H:%M:%S')} ХБР.")
    lines.append("=" * 80)

    report_text = "\n".join(lines)
    filename = f"weather_bot_report_{date_str}.txt"
    return report_text, filename