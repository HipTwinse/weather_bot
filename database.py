"""
Модуль локальной базы данных SQLite для хранения открытых торговых позиций.
Работает полностью автономно, безопасно и бесплатно (Zero-Cost).
"""

import sqlite3
from pathlib import Path
from typing import List, Dict, Any

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
        conn.commit()


def add_position(user_id: int, icao: str, outcomes: str, target_date: str, entry_price: float = 0.0) -> int:
    """Добавляет новую сделку на радарный контроль со статусом OPEN."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_positions (user_id, icao, outcomes, target_date, entry_price, status)
            VALUES (?, ?, ?, ?, ?, 'OPEN')
        """, (user_id, icao.strip().upper(), outcomes.strip(), target_date.strip(), float(entry_price)))
        conn.commit()
        return cursor.lastrowid


def get_user_positions(user_id: int) -> List[Dict[str, Any]]:
    """Возвращает список открытых позиций конкретного пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, icao, outcomes, target_date, entry_price, status, created_at
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
            SELECT id, user_id, icao, outcomes, target_date, entry_price, status 
            FROM user_positions
            WHERE status = 'OPEN' OR status IS NULL
        """)
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


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