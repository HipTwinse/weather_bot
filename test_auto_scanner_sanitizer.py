import pytest
from unittest.mock import AsyncMock, MagicMock
from auto_scanner import sanitize_telegram_html, _safe_send_digest


def test_sanitize_telegram_html_basic():
    # Valid tags must remain untouched
    text = "<b>Жирный текст</b> и <i>курсив</i> и <code>код</code>"
    assert sanitize_telegram_html(text) == text


def test_sanitize_telegram_html_comparison_operators():
    # Raw < must be converted to &lt;
    text = "Темп прогрева (<+0.4°C/ч) и (Штиль <=4 kt) и x < y"
    sanitized = sanitize_telegram_html(text)
    assert "<+" not in sanitized
    assert "<=" not in sanitized
    assert "< y" not in sanitized
    assert "&lt;+0.4°C/ч" in sanitized
    assert "&lt;=4 kt" in sanitized
    assert "&lt; y" in sanitized


def test_sanitize_telegram_html_ampersands():
    # Unescaped & must be converted to &amp;, while valid entities stay intact
    text = "P&L report & &lt;already_escaped&gt; & &amp;"
    sanitized = sanitize_telegram_html(text)
    assert sanitized == "P&amp;L report &amp; &lt;already_escaped&gt; &amp; &amp;"


def test_sanitize_telegram_html_combined_with_tags():
    # Mixed tags and illegal angle brackets
    text = "🛑 <b>ЭКСТРЕННЫЙ ВЫХОД</b> — Темп (<+0.4°C/ч)! <code><15°C</code>"
    sanitized = sanitize_telegram_html(text)
    assert sanitized == "🛑 <b>ЭКСТРЕННЫЙ ВЫХОД</b> — Темп (&lt;+0.4°C/ч)! <code>&lt;15°C</code>"


import asyncio


def test_safe_send_digest_html_success():
    bot = MagicMock()
    bot.send_message = AsyncMock()

    res = asyncio.run(_safe_send_digest(bot, 12345, "<b>Hello</b> world < 5"))
    assert res is True
    bot.send_message.assert_called_once()
    call_args = bot.send_message.call_args
    assert call_args.kwargs["chat_id"] == 12345
    assert call_args.kwargs["parse_mode"] == "HTML"
    assert "&lt; 5" in call_args.kwargs["text"]


def test_safe_send_digest_fallback_on_html_failure():
    bot = MagicMock()
    # First call (HTML) raises TelegramBadRequest or similar exception
    # Second call (plain text fallback) succeeds
    bot.send_message = AsyncMock(side_effect=[Exception("Telegram parse error"), None])

    res = asyncio.run(_safe_send_digest(bot, 12345, "<b>Important</b>: alert <+0.4°C"))
    assert res is True
    assert bot.send_message.call_count == 2
    # Verify fallback call arguments
    fallback_call = bot.send_message.call_args_list[1]
    assert fallback_call.kwargs["chat_id"] == 12345
    assert fallback_call.kwargs["parse_mode"] is None
    assert "Important: alert <+0.4°C" in fallback_call.kwargs["text"]
