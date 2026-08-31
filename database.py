"""
Модуль локальной базы данных SQLite для хранения открытых торговых позиций.
Работает полностью автономно, безопасно и бесплатно (Zero-Cost).
"""

import sqlite3
from pathlib import Path
from typing import List, Dict, Any

DB_PATH = Path(__file__).resolve().parent / "positions.db"


def init_db() -> None:
    """Инициализирует таблицу открытых позиций при первом запуске."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                icao TEXT NOT NULL,
                outcomes TEXT NOT NULL,
                target_date TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def add_position(user_id: int, icao: str, outcomes: str, target_date: str) -> int:
    """Добавляет новую сделку на радарный контроль."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_positions (user_id, icao, outcomes, target_date)
            VALUES (?, ?, ?, ?)
        """, (user_id, icao.strip().upper(), outcomes.strip(), target_date.strip()))
        conn.commit()
        return cursor.lastrowid


def get_user_positions(user_id: int) -> List[Dict[str, Any]]:
    """Возвращает список открытых позиций конкретного пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, icao, outcomes, target_date, created_at
            FROM user_positions
            WHERE user_id = ?
            ORDER BY id DESC
        """, (user_id,))
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def get_all_active_positions() -> List[Dict[str, Any]]:
    """Возвращает все активные позиции для фонового сканирования радаром."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_id, icao, outcomes, target_date FROM user_positions")
        rows = cursor.fetchall()
        return [dict(r) for r in rows]


def delete_position(position_id: int, user_id: int) -> bool:
    """Удаляет сделку из базы данных при закрытии/кэшауте."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM user_positions
            WHERE id = ? AND user_id = ?
        """, (position_id, user_id))
        conn.commit()
        return cursor.rowcount > 0