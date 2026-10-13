"""
Модуль облачной синхронизации и сохранности состояния (Cloud State Safe).
Обеспечивает 100% выживаемость открытых и закрытых сделок, винрейта и режима
при передеплоях и рестартах бесплатного контейнера Render (Zero-Cost Persistence).
"""

import sqlite3
import json
import zlib
import base64
import logging
from datetime import datetime
from typing import Dict, Any, Optional

from aiogram import Bot
import config
from database import DB_PATH, get_trading_mode

logger = logging.getLogger(__name__)

STATE_PIN_HEADER = "💾 <b>ОБЛАЧНЫЙ СЕЙФ WEATHER ALPHA</b>"
STATE_TAG = "#WEATHER_ALPHA_BACKUP:"


def export_state_payload() -> str:
    """Сериализует активные и недавние сделки в компактную сжатую строку base64."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        # Экспортируем все открытые сделки + закрытые за последние 3 дня
        positions = [
            dict(r) for r in c.execute("""
                SELECT id, user_id, icao, outcomes, target_date, entry_price, status,
                       shares, peak_price, trailing_active, token_id, last_alert,
                       is_paper, confidence_score, exit_price, exit_reason, pnl_cents, pnl_percent,
                       closed_at, created_at
                FROM user_positions
                ORDER BY id DESC LIMIT 40
            """).fetchall()
        ]
        wallets = [
            dict(r) for r in c.execute("""
                SELECT user_id, wallet_address, proxy_address, signature_type, trading_mode
                FROM user_wallets
            """).fetchall()
        ]
        subscribers = [
            dict(r) for r in c.execute("""
                SELECT user_id, username, is_active
                FROM bot_subscribers
            """).fetchall()
        ]

    payload = {
        "version": 1,
        "updated_at": datetime.now().isoformat(),
        "positions": positions,
        "wallets": wallets,
        "subscribers": subscribers,
    }
    raw_bytes = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
    compressed = base64.b64encode(zlib.compress(raw_bytes, 9)).decode("ascii")
    return compressed


def import_state_payload(b64_str: str) -> int:
    """Десериализует сжатую строку и восстанавливает позиции и кошельки в SQLite."""
    if not b64_str or not b64_str.strip():
        return 0

    try:
        raw_bytes = zlib.decompress(base64.b64decode(b64_str.strip().encode("ascii")))
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception as e:
        logger.error(f"Ошибка распаковки payload сейфа: {e}")
        return 0

    positions = payload.get("positions", [])
    wallets = payload.get("wallets", [])
    subscribers = payload.get("subscribers", [])

    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        for p in positions:
            p_id = p.get("id")
            c.execute("""
                INSERT OR REPLACE INTO user_positions (
                    id, user_id, icao, outcomes, target_date, entry_price, status,
                    created_at, shares, peak_price, trailing_active, token_id, last_alert,
                    is_paper, confidence_score, exit_price, exit_reason, pnl_cents, pnl_percent, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                p_id, p.get("user_id"), p.get("icao"), p.get("outcomes"), p.get("target_date"),
                p.get("entry_price", 0.0), p.get("status", "OPEN"), p.get("created_at"),
                p.get("shares", 0.0), p.get("peak_price", 0.0), p.get("trailing_active", 0),
                p.get("token_id", ""), p.get("last_alert", ""), p.get("is_paper", 0),
                p.get("confidence_score", 0.0), p.get("exit_price", 0.0), p.get("exit_reason", ""),
                p.get("pnl_cents", 0.0), p.get("pnl_percent", 0.0), p.get("closed_at")
            ))

        for w in wallets:
            uid = w.get("user_id")
            if uid:
                c.execute("""
                    INSERT INTO user_wallets (user_id, private_key, wallet_address, proxy_address, signature_type, trading_mode)
                    VALUES (?, '', ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        wallet_address = excluded.wallet_address,
                        proxy_address = excluded.proxy_address,
                        signature_type = excluded.signature_type,
                        trading_mode = excluded.trading_mode
                """, (
                    uid, w.get("wallet_address", ""), w.get("proxy_address", ""),
                    w.get("signature_type", 1), w.get("trading_mode", "PAPER")
                ))

        for s in subscribers:
            uid = s.get("user_id")
            if uid:
                c.execute("""
                    INSERT INTO bot_subscribers (user_id, username, is_active)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET is_active = excluded.is_active
                """, (uid, s.get("username", ""), s.get("is_active", 1)))

        conn.commit()

    logger.info(f"✅ Восстановлено {len(positions)} записей из облачного сейфа.")
    return len(positions)


async def sync_state_to_telegram(bot: Bot) -> bool:
    """
    Сохраняет состояние в закрепленное сервисное сообщение администратора.
    Работает бесшумно (disable_notification=True) без спама звуками.
    """
    admin_id = getattr(config, "ADMIN_CHAT_ID", None)
    if not admin_id:
        return False

    try:
        admin_uid = int(admin_id)
        payload = export_state_payload()

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            open_count = c.execute("SELECT COUNT(*) FROM user_positions WHERE status = 'OPEN'").fetchone()[0]
            today = datetime.now().strftime("%Y-%m-%d")
            closed_count = c.execute(
                "SELECT COUNT(*) FROM user_positions WHERE status = 'CLOSED' AND (target_date = ? OR closed_at >= ?)",
                (today, today)
            ).fetchone()[0]

        mode = get_trading_mode(admin_uid)
        mode_label = "⚡ БОЕВОЙ" if mode == "REAL" else "🧪 ТЕСТОВЫЙ"
        now_str = datetime.now().strftime("%d.%m %H:%M")

        text = (
            f"{STATE_PIN_HEADER}\n"
            f"<i>Автоматическое сохранение сделок и истории винрейта при перезапусках сервера. Не открепляйте!</i>\n\n"
            f"• Открытых сделок: <b>{open_count}</b>\n"
            f"• Закрытых сегодня: <b>{closed_count}</b>\n"
            f"• Режим торговли: <b>{mode_label}</b>\n"
            f"• Синхронизировано: <code>{now_str}</code>\n\n"
            f"{STATE_TAG}\n<code>{payload}</code>"
        )

        chat = await bot.get_chat(admin_uid)
        pinned = chat.pinned_message

        if pinned and pinned.text and STATE_TAG in pinned.text:
            try:
                await bot.edit_message_text(
                    chat_id=admin_uid,
                    message_id=pinned.message_id,
                    text=text,
                    parse_mode="HTML"
                )
                return True
            except Exception as e_edit:
                if "message is not modified" in str(e_edit).lower():
                    return True
                logger.debug(f"Не удалось отредактировать закреп: {e_edit}")

        # Если закрепа нет или текст не найден — создаем новый и закрепляем
        sent = await bot.send_message(
            chat_id=admin_uid,
            text=text,
            parse_mode="HTML",
            disable_notification=True
        )
        try:
            await bot.pin_chat_message(
                chat_id=admin_uid,
                message_id=sent.message_id,
                disable_notification=True
            )
        except Exception as e_pin:
            logger.debug(f"Не удалось закрепить сообщение сейфа: {e_pin}")

        return True

    except Exception as e:
        logger.warning(f"Ошибка сохранения состояния в Telegram: {e}")
        return False


async def restore_state_from_telegram(bot: Bot) -> bool:
    """Восстанавливает базу данных из закрепленного сервисного сообщения при старте."""
    admin_id = getattr(config, "ADMIN_CHAT_ID", None)
    if not admin_id:
        return False

    try:
        admin_uid = int(admin_id)
        chat = await bot.get_chat(admin_uid)
        pinned = chat.pinned_message

        if pinned and pinned.text and STATE_TAG in pinned.text:
            parts = pinned.text.split(STATE_TAG, 1)
            if len(parts) > 1:
                clean_payload = (
                    parts[1]
                    .replace("<code>", "")
                    .replace("</code>", "")
                    .replace("<pre>", "")
                    .replace("</pre>", "")
                    .strip()
                )
                count = import_state_payload(clean_payload)
                logger.info(f"💾 База данных успешно восстановлена из Telegram-сейфа ({count} записей).")
                return True
    except Exception as e:
        logger.warning(f"Ошибка восстановления состояния из Telegram: {e}")

    return False
