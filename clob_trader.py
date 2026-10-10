"""
Модуль исполнения биржевых сделок на Polymarket CLOB (Central Limit Order Book).
Реализует безопасное подключение, проверку балансов и автоматическую продажу по рынку (Market-Taker).
Поддерживает прокси-кошельки Polymarket (Proxy/Safe), на которых реально хранятся средства пользователей.
"""

import logging
import re
from typing import Any, Dict, Optional, Tuple
import requests
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

# Публичные RPC-узлы Polygon для ончейн-проверок баланса
POLYGON_RPCS = [
    "https://polygon.gateway.tenderly.co",
    "https://gateway.tenderly.co/public/polygon",
    "https://polygon.drpc.org",
]

# Контракты залоговых валют Polymarket на Polygon
COLLATERAL_TOKENS = [
    "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB",  # PUSD (Polymarket USD)
    "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",  # USDC.e (Bridged PoS USDC)
    "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",  # Native USDC
]

# Контракт условных токенов (Conditional Tokens Framework)
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


def clean_private_key(raw_key: str) -> str:
    """Очищает приватный ключ от пробелов, кавычек и префиксов."""
    key = raw_key.strip().strip("'\"")
    if not key.startswith("0x") and len(key) == 64:
        key = "0x" + key
    return key


def resolve_polymarket_proxy(wallet_address: str) -> Optional[str]:
    """
    Запрашивает публичный профиль Polymarket для определения торгового прокси-кошелька (Proxy/Safe).
    На Polymarket средства и контракты хранятся на proxyWallet, 
    в то время как ключ из Predy/Privy является подписантом (EOA Signer).
    """
    if not wallet_address:
        return None
    try:
        url = f"https://gamma-api.polymarket.com/public-profile?address={wallet_address.lower()}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            proxy = data.get("proxyWallet")
            if proxy and proxy.startswith("0x") and len(proxy) == 42:
                return proxy.lower()
    except Exception as e:
        logger.warning(f"Не удалось получить proxyWallet для {wallet_address}: {e}")
    return None


def query_onchain_collateral_balance(wallet_address: str) -> float:
    """Запрашивает ончейн баланс залоговых активов (PUSD / USDC) на Polygon."""
    if not wallet_address:
        return 0.0
    addr_clean = wallet_address[2:].lower().rjust(64, "0")
    data = "0x70a08231" + addr_clean  # balanceOf(address)
    total_bal = 0.0

    for tok in COLLATERAL_TOKENS:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": tok, "data": data}, "latest"],
            "id": 1,
        }
        for rpc in POLYGON_RPCS:
            try:
                r = requests.post(rpc, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=2)
                if r.status_code == 200:
                    res = r.json()
                    if "result" in res and "error" not in res:
                        raw = int(res["result"], 16)
                        total_bal += raw / 1e6
                        break
            except Exception:
                continue

    return round(total_bal, 2)


def query_onchain_token_balance(wallet_address: str, token_id: str) -> float:
    """Запрашивает ончейн количество долей (shares) исхода в контракте CTF на Polygon."""
    if not wallet_address or not token_id:
        return 0.0
    try:
        asset_int = int(token_id)
        addr_padded = wallet_address[2:].lower().rjust(64, "0")
        id_padded = hex(asset_int)[2:].rjust(64, "0")
        # balanceOf(address,uint256) selector: 0x00fdd58e
        data = "0x00fdd58e" + addr_padded + id_padded

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": CTF_CONTRACT, "data": data}, "latest"],
            "id": 1,
        }
        for rpc in POLYGON_RPCS:
            try:
                r = requests.post(rpc, json=payload, headers={"User-Agent": "Mozilla/5.0"}, timeout=2)
                if r.status_code == 200:
                    res = r.json()
                    if "result" in res and "error" not in res:
                        raw = int(res["result"], 16)
                        return round(raw / 1e6, 4)
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Ончейн запрос баланса токена {token_id} завершился с ошибкой: {e}")

    # Fallback на Polymarket Data API
    try:
        url = f"https://data-api.polymarket.com/positions?user={wallet_address.lower()}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
        if r.status_code == 200:
            positions = r.json()
            for p in positions:
                if str(p.get("asset")) == str(token_id):
                    return round(float(p.get("size", 0.0)), 4)
    except Exception:
        pass

    return 0.0


def validate_private_key(raw_key: str) -> Tuple[bool, str, str]:
    """
    Проверяет валидность приватного ключа и способность авторизации на Polymarket CLOB.
    Возвращает (успех, публичный_адрес_подписанта, ошибка).
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


def get_wallet_collateral_balance(
    raw_key: str = "",
    wallet_address: str = "",
    proxy_address: str = "",
) -> float:
    """Возвращает баланс USDC/PUSD на счете Polymarket."""
    # 1. Извлекаем адрес подписанта, если передан ключ
    if raw_key and not wallet_address:
        try:
            key = clean_private_key(raw_key)
            wallet_address = Account.from_key(key).address
        except Exception:
            pass

    # 2. Если proxy_address не указан, разрешаем его через Polymarket Gamma API
    if not proxy_address and wallet_address:
        proxy_address = resolve_polymarket_proxy(wallet_address) or ""

    target_address = proxy_address if proxy_address else wallet_address
    if not target_address:
        return 0.0

    # 3. Ончейн-проверка баланса на целевом адресе (PUSD / USDC)
    bal = query_onchain_collateral_balance(target_address)
    if bal > 0:
        return bal

    # 4. Если на целевом 0 и есть EOA, проверяем также EOA
    if proxy_address and wallet_address and proxy_address.lower() != wallet_address.lower():
        eoa_bal = query_onchain_collateral_balance(wallet_address)
        if eoa_bal > 0:
            return eoa_bal

    # 5. Резервная проверка через CLOB API при наличии ключа
    if raw_key:
        try:
            key = clean_private_key(raw_key)
            sig_type = 1 if (proxy_address and proxy_address.lower() != wallet_address.lower()) else 0
            client = ClobClient(
                host=POLYMARKET_CLOB_HOST,
                key=key,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=sig_type,
                funder=proxy_address if proxy_address else None,
            )
            creds = client.create_or_derive_api_creds()
            if creds:
                client.set_api_creds(creds)
                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
                res = client.get_balance_allowance(params)
                raw_bal = float(res.get("balance", 0))
                return round(raw_bal / 1e6, 2)
        except Exception as e:
            logger.warning(f"Не удалось получить баланс через CLOB: {e}")

    return 0.0


def get_token_balance(
    raw_key: str,
    token_id: str,
    proxy_address: str = "",
) -> float:
    """Возвращает количество контрактов (shares) конкретного токена на балансе."""
    if not token_id:
        return 0.0

    # 1. Определяем адреса
    wallet_address = ""
    try:
        key = clean_private_key(raw_key)
        wallet_address = Account.from_key(key).address
    except Exception:
        pass

    if not proxy_address and wallet_address:
        proxy_address = resolve_polymarket_proxy(wallet_address) or ""

    target_address = proxy_address if proxy_address else wallet_address

    # 2. Ончейн-проверка баланса долей
    if target_address:
        onchain_shares = query_onchain_token_balance(target_address, token_id)
        if onchain_shares > 0:
            return onchain_shares

    # 3. Fallback на CLOB API
    try:
        key = clean_private_key(raw_key)
        sig_type = 1 if (proxy_address and wallet_address and proxy_address.lower() != wallet_address.lower()) else 0
        client = ClobClient(
            host=POLYMARKET_CLOB_HOST,
            key=key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=sig_type,
            funder=proxy_address if proxy_address else None,
        )
        creds = client.create_or_derive_api_creds()
        if creds:
            client.set_api_creds(creds)
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id, signature_type=sig_type)
            res = client.get_balance_allowance(params)
            raw_bal = float(res.get("balance", 0))
            return round(raw_bal / 1e6, 4)
    except Exception as e:
        logger.warning(f"Не удалось получить баланс токена {token_id} через CLOB: {e}")

    return 0.0


def execute_market_sell(
    raw_key: str,
    token_id: str,
    shares: float,
    worst_price: float = 0.01,
    proxy_address: str = "",
    signature_type: int = 1,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Выполняет продажу указанного объема контрактов по рынку (Market-Taker).
    Учитывает архитектуру Polymarket Proxy/Safe:
    - maker: proxy_address (где хранятся токены)
    - signer: EOA address из приватного ключа
    - signatureType: 1 (Polymarket Proxy)
    """
    if not token_id:
        return False, "Не указан token_id для продажи", {}

    if shares <= 0:
        return False, f"Некорректный объем продажи: {shares}", {}

    try:
        key = clean_private_key(raw_key)
        signer_acc = Account.from_key(key)
        signer_addr = signer_acc.address

        if not proxy_address:
            proxy_address = resolve_polymarket_proxy(signer_addr) or ""

        # Если proxyWallet найден и отличается от адреса ключа -> signature_type = 1, funder = proxy
        sig_type = signature_type
        if proxy_address and proxy_address.lower() != signer_addr.lower():
            sig_type = 1
            funder_addr = proxy_address
        else:
            sig_type = 0
            funder_addr = signer_addr

        client = ClobClient(
            host=POLYMARKET_CLOB_HOST,
            key=key,
            chain_id=POLYGON_CHAIN_ID,
            signature_type=sig_type,
            funder=funder_addr,
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        # Получаем реальный минимальный шаг цены (tick_size) рынка (обычно 0.01 на Polymarket)
        try:
            tick_size = float(client.get_tick_size(token_id))
        except Exception:
            tick_size = 0.01

        safe_worst_price = max(float(worst_price), tick_size)
        safe_worst_price = min(safe_worst_price, 1.0 - tick_size)
        safe_worst_price = round(safe_worst_price, 4)

        # Способ 1: Прямой рыночный ордер через SDK
        try:
            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=shares,
                side="SELL",
                price=safe_worst_price,
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
            price=safe_worst_price,
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
