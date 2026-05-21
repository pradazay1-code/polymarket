"""Thin wrapper around py-clob-client that supports multiple accounts.

Exposes the subset of operations the bots actually need:
    - list open orders
    - cancel orders
    - place a GTC limit order (the "stink bid")
    - read the order book (best bid/ask, tick size, min order size)
    - read user positions (via data-api)
    - read USDC balance + allowance
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    AssetType,
    BalanceAllowanceParams,
    OpenOrderParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.order_builder.constants import BUY

from .config import Account

log = logging.getLogger(__name__)


@dataclass
class BookSnapshot:
    token_id: str
    best_bid: float
    best_ask: float
    midpoint: float
    tick_size: float
    min_order_size: float


class PolymarketClient:
    """Per-account Polymarket CLOB client with L2 API creds auto-derived."""

    def __init__(self, account: Account, host: str, chain_id: int = 137):
        self.account = account
        self.host = host
        self.chain_id = chain_id
        self._client = ClobClient(
            host=host,
            key=account.private_key,
            chain_id=chain_id,
            signature_type=account.signature_type,
            funder=account.funder_address,
        )
        creds = self._client.create_or_derive_api_creds()
        self._client.set_api_creds(creds)
        # Refresh server-side allowance snapshot once at startup (required for
        # Magic/email wallets, harmless for EOAs).
        try:
            self._client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
        except Exception as e:  # noqa: BLE001
            log.debug("update_balance_allowance skipped: %s", e)
        log.info(
            "Polymarket client ready account=%s funder=%s sig_type=%d",
            account.name, account.funder_address, account.signature_type,
        )

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
        best_bid = max(float(b.price) for b in bids)
        best_ask = min(float(a.price) for a in asks)
        return BookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=(best_bid + best_ask) / 2,
            tick_size=float(getattr(book, "tick_size", 0.01) or 0.01),
            min_order_size=float(getattr(book, "min_order_size", 5) or 5),
        )

    def open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        params = OpenOrderParams(market=market) if market else OpenOrderParams()
        try:
            return self._client.get_orders(params) or []
        except Exception as e:  # noqa: BLE001
            log.warning("get_orders failed: %s", e)
            return []

    def positions(self) -> list[dict[str, Any]]:
        url = "https://data-api.polymarket.com/positions"
        try:
            r = requests.get(
                url,
                params={"user": self.account.funder_address, "limit": 500},
                timeout=15,
            )
            r.raise_for_status()
            return r.json() or []
        except Exception as e:  # noqa: BLE001
            log.warning("positions fetch failed: %s", e)
            return []

    # --- write --------------------------------------------------------------

    def _log_cancel_response(self, res: Any, label: str) -> int:
        if not isinstance(res, dict):
            return 0
        canceled = res.get("canceled", []) or []
        not_canceled = res.get("not_canceled", {}) or {}
        if not_canceled:
            log.warning("%s: %d orders could not be cancelled: %s", label, len(not_canceled), not_canceled)
        log.info("%s: cancelled %d orders", label, len(canceled))
        return len(canceled)

    def cancel_all(self) -> int:
        try:
            return self._log_cancel_response(self._client.cancel_all(), "cancel_all")
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_all failed: %s", e)
            return 0

    def cancel_market(self, market_condition_id: str) -> int:
        try:
            res = self._client.cancel_market_orders(market=market_condition_id)
            return self._log_cancel_response(res, f"cancel_market[{market_condition_id[:10]}]")
        except Exception as e:  # noqa: BLE001
            log.warning("cancel_market failed market=%s err=%s", market_condition_id, e)
            return 0

    def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size_shares: float,
        *,
        tick_size: float = 0.01,
    ) -> dict[str, Any] | None:
        """GTC limit buy. Rounds `price` to the market's tick size."""
        # Snap price to tick.
        ticks = round(price / tick_size)
        price = round(ticks * tick_size, 6)
        size = round(size_shares, 2)
        args = OrderArgs(token_id=token_id, price=price, size=size, side=BUY)
        try:
            signed = self._client.create_order(args)
            resp = self._client.post_order(signed, OrderType.GTC)
            log.info("posted order token=%s px=%s size=%s resp=%s", token_id[:12], price, size, resp)
            return resp
        except Exception as e:  # noqa: BLE001
            log.warning("post_order failed token=%s px=%s size=%s err=%s", token_id[:12], price, size, e)
            return None

    def balance_allowance(self) -> dict[str, float]:
        """Return {'balance': usdc, 'allowance': usdc} for the collateral asset."""
        try:
            res = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return {
                "balance": float(res.get("balance", 0)) / 1e6,
                "allowance": float(res.get("allowance", 0)) / 1e6,
            }
        except Exception as e:  # noqa: BLE001
            log.warning("balance fetch failed: %s", e)
            return {"balance": 0.0, "allowance": 0.0}

    def usdc_balance(self) -> float:
        return self.balance_allowance()["balance"]
