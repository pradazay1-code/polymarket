"""Stink-bid strategy.

For each open market in scope:
  1. Identify the favorite (highest YES price).
  2. Skip if favorite is outside the [min, max] window — no edge bidding on
     near-certain or coin-flip markets.
  3. Compute bid = current_favorite_price * (1 - discount), snapped to the
     market's tick size.
  4. Buy `bet_size_usdc / bid` shares as a GTC limit order, respecting the
     market's min_order_size.
  5. On the next refresh cycle, cancel everything and re-post (so bids track
     the market price).

Risk controls:
  - Refuse to post if total open notional exceeds `max_open_notional_usdc`.
  - Skip markets where the order book is missing or has no liquidity.
  - Honor `dry_run` — only logs, never sends.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..client import BookSnapshot, PolymarketClient
from ..config import GlobalConfig
from ..markets import Market

log = logging.getLogger(__name__)


@dataclass
class Intent:
    market_question: str
    market_slug: str
    condition_id: str
    end_date: str | None
    token_id: str
    outcome: str
    favorite_price: float
    bid_price: float
    size_shares: float
    notional: float
    tick_size: float
    min_order_size: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanResult:
    intents: list[Intent] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    inspected: int = 0


class StinkBidStrategy:
    def __init__(self, client: PolymarketClient, cfg: GlobalConfig, label: str):
        self.client = client
        self.cfg = cfg
        self.label = label

    # --- planning -----------------------------------------------------------

    def plan(self, markets: list[Market]) -> PlanResult:
        result = PlanResult(inspected=len(markets))
        for m in markets:
            fav_px = m.favorite_price()
            slug = m.slug or m.condition_id[:10]
            if fav_px < self.cfg.min_favorite_price:
                result.skipped.append({"slug": slug, "reason": f"fav {fav_px:.2f} < min {self.cfg.min_favorite_price}"})
                continue
            if fav_px > self.cfg.max_favorite_price:
                result.skipped.append({"slug": slug, "reason": f"fav {fav_px:.2f} > max {self.cfg.max_favorite_price}"})
                continue

            token_id = m.favorite_token_id()
            book: BookSnapshot | None = self.client.get_book(token_id)
            if book is None:
                result.skipped.append({"slug": slug, "reason": "no order book / no liquidity"})
                continue

            # Snap bid to the market's actual tick size.
            tick = book.tick_size or 0.01
            raw_bid = fav_px * (1 - self.cfg.discount)
            ticks = max(1, int(raw_bid / tick))
            bid = round(ticks * tick, 6)
            if bid >= fav_px:
                result.skipped.append({"slug": slug, "reason": f"bid {bid} >= fav {fav_px}"})
                continue

            size_shares = round(self.cfg.bet_size_usdc / bid, 2)
            min_size = book.min_order_size or 5.0
            if size_shares < min_size:
                # Bump up to the market minimum if the user's risk budget allows it.
                bumped_notional = round(min_size * bid, 2)
                if bumped_notional > self.cfg.bet_size_usdc * 1.5:
                    result.skipped.append({
                        "slug": slug,
                        "reason": f"min_size {min_size} would cost ${bumped_notional} > 1.5x bet_size",
                    })
                    continue
                size_shares = min_size

            notional = round(bid * size_shares, 2)
            result.intents.append(
                Intent(
                    market_question=m.question,
                    market_slug=slug,
                    condition_id=m.condition_id,
                    end_date=m.end_date.isoformat() if m.end_date else None,
                    token_id=token_id,
                    outcome=m.favorite_outcome(),
                    favorite_price=fav_px,
                    bid_price=bid,
                    size_shares=size_shares,
                    notional=notional,
                    tick_size=tick,
                    min_order_size=min_size,
                    reason=f"favorite={m.favorite_outcome()} px={fav_px:.2f} -> bid {bid}",
                )
            )
        return result

    # --- execution ----------------------------------------------------------

    def execute(self, intents: list[Intent]) -> list[dict]:
        if self.cfg.cancel_before_refresh and not self.cfg.dry_run:
            self.client.cancel_all()

        budget = self.cfg.max_open_notional_usdc
        posted: list[dict] = []
        for intent in intents:
            if intent.notional > budget:
                log.info("[%s] budget exhausted, stopping after %d posts", self.label, len(posted))
                break
            if self.cfg.dry_run:
                log.info(
                    "[DRY %s] would buy %.2f sh @ $%s on %s (%s)",
                    self.label, intent.size_shares, intent.bid_price, intent.market_slug, intent.reason,
                )
                posted.append({"dry_run": True, "intent": intent.to_dict(), "ts": datetime.now(timezone.utc).isoformat()})
                budget -= intent.notional
                continue
            resp = self.client.place_limit_buy(
                intent.token_id, intent.bid_price, intent.size_shares,
                tick_size=intent.tick_size,
            )
            if resp:
                posted.append({"resp": resp, "intent": intent.to_dict(), "ts": datetime.now(timezone.utc).isoformat()})
                budget -= intent.notional
        return posted
