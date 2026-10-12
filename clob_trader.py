"""
Модуль исполнения биржевых сделок на Polymarket CLOB (Central Limit Order Book).
Реализует безопасное подключение, проверку балансов и автоматическую продажу по рынку (Market-Taker).
Поддерживает прокси-кошельки Polymarket (Proxy/Safe), на которых реально хранятся средства пользователей.
"""

import logging
import re
import time
from typing import Any, Dict, Optional, Tuple
import requests
from eth_account import Account
from py_clob_client_v2 import (
    ClobClient,
    AssetType,
    BalanceAllowanceParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)

logger = logging.getLogger("ClobTrader")

POLYMARKET_CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137


def _derive_and_set_api_creds(client: ClobClient):
    """Генерирует или восстанавливает API-ключи CLOB V2 и устанавливает их в клиенте."""
    derive_fn = getattr(client, "create_or_derive_api_key", None) or getattr(
        client, "create_or_derive_api_creds", None
    )
    creds = derive_fn() if derive_fn else None
    if creds:
        client.set_api_creds(creds)
    return creds

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
    for attempt in range(2):
        try:
            url = f"https://gamma-api.polymarket.com/public-profile?address={wallet_address.lower()}"
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                proxy = data.get("proxyWallet")
                if proxy and proxy.startswith("0x") and len(proxy) == 42:
                    return proxy.lower()
        except Exception as e:
            logger.warning(f"Попытка {attempt+1}: Не удалось получить proxyWallet для {wallet_address}: {e}")
            if attempt == 0:
                time.sleep(0.5)
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
        try:
            client = ClobClient(
                host=POLYMARKET_CLOB_HOST,
                key=key,
                chain_id=POLYGON_CHAIN_ID,
                signature_type=0,
            )
            creds = _derive_and_set_api_creds(client)
            if not creds or not creds.api_key:
                return False, address, "Не удалось сгенерировать торговые ключи Polymarket CLOB."
        except Exception as e_clob:
            logger.warning(f"Предупреждение при запросе к CLOB API (сетевая задержка): {e_clob}")

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
            creds = _derive_and_set_api_creds(client)
            if creds:
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
        creds = _derive_and_set_api_creds(client)
        if creds:
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
    Выполняет продажу указанного объема контрактов по рынку (Market-Taker) по протоколу CLOB V2.
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
        _derive_and_set_api_creds(client)

        try:
            tick_size = float(client.get_tick_size(token_id))
        except Exception:
            tick_size = 0.01

        safe_worst_price = max(float(worst_price), tick_size)
        safe_worst_price = min(safe_worst_price, 1.0 - tick_size)
        safe_worst_price = round(safe_worst_price, 4)

        # Способ 1: Прямой рыночный ордер V2 (create_and_post_market_order)
        try:
            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(shares, 2),
                side="SELL",
                price=safe_worst_price,
                order_type=OrderType.FOK,
            )
            if hasattr(client, "create_and_post_market_order"):
                resp = client.create_and_post_market_order(market_args, order_type=OrderType.FOK)
            else:
                signed_order = client.create_market_order(market_args)
                resp = client.post_order(signed_order, OrderType.FOK)
            if isinstance(resp, dict) and (resp.get("success") or (resp.get("orderID") and not resp.get("errorMsg"))):
                order_id = str(resp.get("orderID", "OK"))
                return True, order_id, resp
        except Exception as e1:
            logger.info(f"MarketOrderArgs V2 не сработал ({e1}), переключаемся на агрессивный OrderArgs V2...")

        # Способ 2: Агрессивный ордер OrderArgs V2 (сначала FAK, затем GTC)
        order_args = OrderArgs(
            token_id=token_id,
            price=safe_worst_price,
            size=round(shares, 2),
            side="SELL",
        )
        for o_type in (OrderType.FAK, OrderType.GTC):
            try:
                resp2 = client.create_and_post_order(order_args, order_type=o_type)
                if isinstance(resp2, dict) and (resp2.get("success") or (resp2.get("orderID") and not resp2.get("errorMsg"))):
                    order_id = str(resp2.get("orderID", "OK"))
                    return True, order_id, resp2
            except Exception as e_sub:
                logger.info(f"Попытка продажи ({o_type}) завершилась: {e_sub}")
                last_sub_err = e_sub

        return False, f"Биржа отклонила сделку ({locals().get('last_sub_err', 'нет ликвидности')})", {}

    except Exception as e:
        logger.error(f"Критическая ошибка исполнения автопродажи: {e}", exc_info=True)
        return False, f"Исключение при продаже: {str(e)}", {}


def execute_market_buy(
    raw_key: str,
    token_id: str,
    amount_usd: float = 1.08,
    max_price: float = 0.50,
    proxy_address: str = "",
    signature_type: int = 1,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Выполняет покупку исхода на Polymarket CLOB V2.
    Учитывает:
    - Минимальный размер ордера на Polymarket ($1.00 - $1.05), по умолчанию $1.08.
    - Ограничение максимальной цены входа (max_price), чтобы не покупать перегретые токены.
    - Возвращает (успех, order_id_или_ошибка, детали).
    """
    if not token_id:
        return False, "Не указан token_id для покупки", {}

    if amount_usd < 1.00:
        return False, f"Сумма покупки (${amount_usd:.2f}) ниже минимального порога Polymarket ($1.00)", {}

    try:
        key = clean_private_key(raw_key)
        signer_acc = Account.from_key(key)
        signer_addr = signer_acc.address

        if not proxy_address:
            proxy_address = resolve_polymarket_proxy(signer_addr) or ""

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
        _derive_and_set_api_creds(client)

        try:
            tick_size = float(client.get_tick_size(token_id))
        except Exception:
            tick_size = 0.01

        safe_max_price = max(float(max_price), tick_size)
        safe_max_price = min(safe_max_price, 0.99)
        safe_max_price = round(safe_max_price, 4)

        shares = round(amount_usd / safe_max_price, 2)
        if shares <= 0:
            return False, f"Некорректный расчет объема: {shares}", {}

        # 1. Попытка рыночного ордера V2 на покупку (Market FOK)
        try:
            market_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(amount_usd, 2),
                side="BUY",
                price=safe_max_price,
                order_type=OrderType.FOK,
            )
            if hasattr(client, "create_and_post_market_order"):
                resp = client.create_and_post_market_order(market_args, order_type=OrderType.FOK)
            else:
                signed_order = client.create_market_order(market_args)
                resp = client.post_order(signed_order, OrderType.FOK)
            if isinstance(resp, dict) and (resp.get("success") or (resp.get("orderID") and not resp.get("errorMsg"))):
                order_id = str(resp.get("orderID", "OK"))
                return True, order_id, resp
        except Exception as e1:
            logger.info(f"MarketOrderArgs V2 на покупку не сработал ({e1}), переключаемся на OrderArgs V2...")

        # 2. Агрессивный ордер OrderArgs V2 на покупку (FAK / GTC)
        order_args = OrderArgs(
            token_id=token_id,
            price=safe_max_price,
            size=shares,
            side="BUY",
        )
        for o_type in (OrderType.FAK, OrderType.GTC):
            try:
                resp2 = client.create_and_post_order(order_args, order_type=o_type)
                if isinstance(resp2, dict) and (resp2.get("success") or (resp2.get("orderID") and not resp2.get("errorMsg"))):
                    order_id = str(resp2.get("orderID", "OK"))
                    return True, order_id, resp2
            except Exception as e_sub:
                logger.info(f"Попытка покупки ({o_type}) завершилась: {e_sub}")
                last_sub_err = e_sub

        return False, f"Биржа отклонила покупку ({locals().get('last_sub_err', 'нет ликвидности')})", {}

    except Exception as e:
        logger.error(f"Критическая ошибка исполнения автопокупки: {e}", exc_info=True)
        return False, f"Исключение при покупке: {str(e)}", {}

