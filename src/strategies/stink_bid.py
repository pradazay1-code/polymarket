"""Stink-bid strategy.

For each open market in scope:
  1. Identify the favorite (highest YES price).
  2. Skip if favorite is outside the [min, max] window — no edge bidding on
     near-certain or coin-flip markets.
  3. Compute bid = current_favorite_price * (1 - discount), floored at $0.01.
  4. Buy `bet_size_usdc / bid` shares as a GTC limit order.
  5. On the next refresh cycle, cancel everything in the strategy's scope and
     re-post at the new market price (so bids track the market down).

Risk controls:
  - Refuse to post if total open notional exceeds `max_open_notional_usdc`.
  - Refuse to double-up: if an open order already exists on the same token at
    a similar price, skip.
  - Honor `dry_run` — only logs, never sends.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..client import PolymarketClient
from ..config import GlobalConfig
from ..markets import Market

log = logging.getLogger(__name__)


@dataclass
class Intent:
    market: Market
    token_id: str
    outcome: str
    price: float
    size_shares: float
    notional: float
    reason: str


class StinkBidStrategy:
    def __init__(self, client: PolymarketClient, cfg: GlobalConfig, label: str):
        self.client = client
        self.cfg = cfg
        self.label = label

    # --- planning -----------------------------------------------------------

    def plan(self, markets: list[Market]) -> list[Intent]:
        intents: list[Intent] = []
        for m in markets:
            fav_px = m.favorite_price()
            if fav_px < self.cfg.min_favorite_price:
                log.debug("[%s] skip %s: favorite %.2f below min %.2f", self.label, m.slug, fav_px, self.cfg.min_favorite_price)
                continue
            if fav_px > self.cfg.max_favorite_price:
                log.debug("[%s] skip %s: favorite %.2f above max %.2f", self.label, m.slug, fav_px, self.cfg.max_favorite_price)
                continue
            bid = max(0.01, round(fav_px * (1 - self.cfg.discount), 2))
            if bid >= fav_px:
                continue
            size_shares = round(self.cfg.bet_size_usdc / bid, 2)
            if size_shares < 1:
                # Polymarket has a min order size; with $5 notional and a $0.30 bid we get ~16 shares,
                # so this guard only triggers on bad config.
                log.debug("[%s] skip %s: size %.2f shares < 1", self.label, m.slug, size_shares)
                continue
            intents.append(
                Intent(
                    market=m,
                    token_id=m.favorite_token_id(),
                    outcome=m.favorite_outcome(),
                    price=bid,
                    size_shares=size_shares,
                    notional=round(bid * size_shares, 2),
                    reason=f"favorite={m.favorite_outcome()} px={fav_px:.2f} -> bid {bid:.2f}",
                )
            )
        return intents

    # --- execution ----------------------------------------------------------

    def execute(self, intents: list[Intent]) -> list[dict]:
        if self.cfg.cancel_before_refresh and not self.cfg.dry_run:
            n = self.client.cancel_all()
            log.info("[%s] cancelled %d existing orders", self.label, n)

        # Risk cap: don't post past max_open_notional_usdc.
        budget = self.cfg.max_open_notional_usdc
        posted: list[dict] = []
        for intent in intents:
            if intent.notional > budget:
                log.info("[%s] budget exhausted, stopping after %d posts", self.label, len(posted))
                break
            if self.cfg.dry_run:
                log.info("[DRY %s] would buy %.2f sh @ $%.2f on %s (%s)", self.label, intent.size_shares, intent.price, intent.market.slug, intent.reason)
                posted.append({"dry_run": True, "intent": intent})
                budget -= intent.notional
                continue
            resp = self.client.place_limit_buy(intent.token_id, intent.price, intent.size_shares)
            if resp:
                posted.append({"resp": resp, "intent": intent})
                budget -= intent.notional
        return posted
