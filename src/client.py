"""Thin wrapper around py-clob-client that supports multiple accounts.

Exposes the subset of operations the bots actually need:
    - list open orders
    - cancel orders
    - place a GTC limit order (the "stink bid")
    - read the order book midpoint / best ask
    - read user positions (via data-api)

Polymarket uses a proxy wallet (the "funder") that signs L2 actions with a
separate EOA private key. Both are passed in per account.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY

from .config import Account

log = logging.getLogger(__name__)


@dataclass
class BookSnapshot:
    token_id: str
    best_bid: float
    best_ask: float
    midpoint: float


class PolymarketClient:
    """Per-account Polymarket CLOB client with L2 API creds auto-derived."""

    def __init__(self, account: Account, host: str, chain_id: int = POLYGON):
        self.account = account
        self.host = host
        self.chain_id = chain_id
        # Signature type 2 = proxy wallet (the standard Polymarket setup).
        self._client = ClobClient(
            host=host,
            key=account.private_key,
            chain_id=chain_id,
            signature_type=2,
            funder=account.funder_address,
        )
        # Derive or create L2 API credentials (idempotent).
        creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)
        log.info("Polymarket client ready for account=%s funder=%s", account.name, account.funder_address)

    # --- read ---------------------------------------------------------------

    def get_book(self, token_id: str) -> BookSnapshot | None:
        try:
            book = self._client.get_order_book(token_id)
        except Exception as e:  # noqa: BLE001
            log.warning("get_order_book failed token=%s err=%s", token_id, e)
            return None
        bids = getattr(book, "bids", []) or []
        asks = getattr(book, "asks", []) or []
        if not bids or not asks:
            return None
        # py-clob-client returns bids ascending and asks ascending — best bid is the highest,
        # best ask is the lowest.
        best_bid = max(float(b.price) for b in bids)
        best_ask = min(float(a.price) for a in asks)
        return BookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=(best_bid + best_ask) / 2,
        )

    def open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        params = OpenOrderParams(market=market) if market else OpenOrderParams()
        try:
            return self._client.get_orders(params) or []
        except Exception as e:  # noqa: BLE001
            log.warning("get_orders failed: %s", e)
            return []

    def positions(self) -> list[dict[str, Any]]:
        """Polymarket positions live on the data-api, not the CLOB."""
        url = "https://data-api.polymarket.com/positions"
        try:
            r = requests.get(url, params={"user": self.account.funder_address, "limit": 500}, timeout=15)
            r.raise_for_status()
            return r.json() or []
        except Exception as e:  # noqa: BLE001
            log.warning("positions fetch failed: %s", e)
            return []

    # --- write --------------------------------------------------------------

    def cancel_all(self) -> int:
        try:
            res = self._client.cancel_all()
            n = len(res.get("canceled", [])) if isinstance(res, dict) else 0
            log.info("cancelled %d orders", n)
            return n
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_all failed: %s", e)
            return 0

    def cancel_market(self, market_condition_id: str) -> int:
        try:
            res = self._client.cancel_market_orders(market=market_condition_id)
            n = len(res.get("canceled", [])) if isinstance(res, dict) else 0
            log.info("cancelled %d orders on market %s", n, market_condition_id)
            return n
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_market failed market=%s err=%s", market_condition_id, e)
            return 0

    def place_limit_buy(self, token_id: str, price: float, size_shares: float) -> dict[str, Any] | None:
        """GTC limit buy. `size_shares` is the number of YES/NO shares (1 share pays $1 on win)."""
        # Polymarket prices are in $0.01 increments; sizes in integer shares for most markets.
        price = round(price, 2)
        size = round(size_shares, 2)
        args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        try:
            signed = self._client.create_order(args)
            resp = self._client.post_order(signed, OrderType.GTC)
            log.info("posted order token=%s px=%.2f size=%.2f resp=%s", token_id, price, size, resp)
            return resp
        except Exception as e:  # noqa: BLE001
            log.warning("post_order failed token=%s px=%.2f size=%.2f err=%s", token_id, price, size, e)
            return None

    def usdc_balance(self) -> float:
        try:
            res = self._client.get_balance_allowance(BalanceAllowanceParams())
            return float(res.get("balance", 0)) / 1e6
        except Exception as e:  # noqa: BLE001
            log.warning("balance fetch failed: %s", e)
            return 0.0
