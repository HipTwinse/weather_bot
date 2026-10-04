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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                private_key TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                proxy_address TEXT DEFAULT '',
                signature_type INTEGER DEFAULT 1,
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
) -> int:
    """Добавляет новую сделку на радарный контроль со статусом OPEN."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_positions (user_id, icao, outcomes, target_date, entry_price, status, shares, peak_price, trailing_active, token_id)
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, 0, ?)
        """, (
            user_id,
            icao.strip().upper(),
            outcomes.strip(),
            target_date.strip(),
            float(entry_price),
            float(shares),
            float(entry_price),
            token_id.strip(),
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
                   shares, peak_price, trailing_active, token_id, last_alert, created_at
            FROM user_positions
            WHERE user_id = ? AND (status = 'OPEN' OR status IS NULL)
            ORDER BY id DESC
        """, (user_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_all_active_positions() -> List[Dict[str, Any]]:
    """Возвращает все активные позиции (status = 'OPEN') для фонового сканирования радаром."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, icao, outcomes, target_date, entry_price, status,
                   shares, peak_price, trailing_active, token_id, last_alert, created_at
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


def close_position_with_exit(position_id: int, exit_price: float = 0.0) -> None:
    """Закрывает сделку в базе данных при исполнении автопродажи."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_positions
            SET status = 'CLOSED'
            WHERE id = ?
        """, (position_id,))
        conn.commit()


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