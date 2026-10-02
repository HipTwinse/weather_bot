"""
Модуль исполнения биржевых сделок на Polymarket CLOB (Central Limit Order Book).
Реализует безопасное подключение, проверку балансов и автоматическую продажу по рынку (Market-Taker).
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)

logger = logging.getLogger("ClobTrader")

POLYMARKET_CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def clean_private_key(raw_key: str) -> str:
    """Очищает приватный ключ от пробелов, кавычек и префиксов."""
    key = raw_key.strip().strip("'\"")
    if not key.startswith("0x") and len(key) == 64:
        key = "0x" + key
    return key


def validate_private_key(raw_key: str) -> Tuple[bool, str, str]:
    """
    Проверяет валидность приватного ключа и способность авторизации на Polymarket CLOB.
    Возвращает (успех, публичный_адрес, ошибка).
    """
    try:
        key = clean_private_key(raw_key)
        if not re.match(r"^0x[a-fA-F0-9]{64}$", key):
            return False, "", "Неверный формат ключа (ожидается 64 hex-символа)."

        acc = Account.from_key(key)
        address = acc.address

        # Тестовая проверка генерации API-ключей CLOB
        client = ClobClient(
            host=POLYMARKET_CLOB_HOST,
            key=key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=0,
        )
        creds = client.create_or_derive_api_creds()
        if not creds or not creds.api_key:
            return False, address, "Не удалось сгенерировать торговые ключи Polymarket CLOB."

        return True, address, ""
    except Exception as e:
        logger.error(f"Ошибка валидации ключа: {e}")
        return False, "", f"Сбой проверки ключа: {str(e)}"


def get_wallet_collateral_balance(raw_key: str) -> float:
    """Возвращает баланс USDC на счете Polymarket."""
    try:
        key = clean_private_key(raw_key)
        client = ClobClient(
            host=POLYMARKET_CLOB_HOST,
            key=key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=0,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        res = client.get_balance_allowance(params)
        raw_bal = float(res.get("balance", 0))
        return round(raw_bal / 1e6, 2)
    except Exception as e:
        logger.warning(f"Не удалось получить баланс USDC: {e}")
        return 0.0


def get_token_balance(raw_key: str, token_id: str) -> float:
    """Возвращает количество контрактов (shares) конкретного токена на балансе."""
    if not token_id:
        return 0.0
    try:
        key = clean_private_key(raw_key)
        client = ClobClient(
            host=POLYMARKET_CLOB_HOST,
            key=key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=0,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        res = client.get_balance_allowance(params)
        raw_bal = float(res.get("balance", 0))
        return round(raw_bal / 1e6, 4)
    except Exception as e:
        logger.warning(f"Не удалось получить баланс токена {token_id}: {e}")
        return 0.0


def execute_market_sell(
    raw_key: str,
    token_id: str,
    shares: float,
    worst_price: float = 0.01,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Выполняет продажу указанного объема контрактов по рынку (Market-Taker).
    Возвращает (успех, order_id_или_ошибка, детали).
    """
    if not token_id:
        return False, "Не указан token_id для продажи", {}

    if shares <= 0:
        return False, f"Некорректный объем продажи: {shares}", {}

    try:
        key = clean_private_key(raw_key)
        client = ClobClient(
            host=POLYMARKET_CLOB_HOST,
            key=key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=0,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        # Способ 1: Прямой рыночный ордер через SDK
        try:
            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=shares,
                side="SELL",
                price=worst_price,
                order_type=OrderType.FOK,
            )
            signed_order = client.create_market_order(market_args)
            resp = client.post_order(signed_order, OrderType.FOK)
            if resp and resp.get("success"):
                order_id = resp.get("orderID", "OK")
                return True, order_id, resp
            elif resp and not resp.get("errorMsg"):
                return True, str(resp.get("orderID", "FILLED")), resp
        except Exception as e1:
            logger.info(f"MarketOrderArgs не сработал ({e1}), переключаемся на агрессивный OrderArgs...")

        # Способ 2: Агрессивный FAK-ордер на продажу в существующий бид
        order_args = OrderArgs(
            price=max(0.001, worst_price),
            size=shares,
            side="SELL",
            token_id=token_id,
        )
        resp2 = client.create_and_post_order(order_args)
        if resp2 and (resp2.get("success") or resp2.get("orderID")):
            order_id = resp2.get("orderID", "OK")
            return True, order_id, resp2
        else:
            err = resp2.get("errorMsg") if isinstance(resp2, dict) else str(resp2)
            return False, f"Биржа отклонила сделку: {err}", resp2 if isinstance(resp2, dict) else {}

    except Exception as e:
        logger.error(f"Критическая ошибка исполнения автопродажи: {e}", exc_info=True)
        return False, f"Исключение при продаже: {str(e)}", {}
