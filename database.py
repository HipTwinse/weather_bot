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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                private_key TEXT NOT NULL,
                wallet_address TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


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
                   shares, peak_price, trailing_active, token_id, created_at
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
                   shares, peak_price, trailing_active, token_id, created_at
            FROM user_positions
            WHERE status = 'OPEN' OR status IS NULL
        """)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def save_user_wallet(user_id: int, private_key: str, wallet_address: str) -> None:
    """Сохраняет приватный ключ и адрес кошелька пользователя для автопродажи."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_wallets (user_id, private_key, wallet_address, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                private_key = excluded.private_key,
                wallet_address = excluded.wallet_address,
                updated_at = CURRENT_TIMESTAMP
        """, (user_id, private_key.strip(), wallet_address.strip()))
        conn.commit()


def get_user_wallet(user_id: int) -> Optional[Dict[str, Any]]:
    """Получает сохраненный кошелек пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, private_key, wallet_address FROM user_wallets WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


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