"""Replay historical Polymarket prices through the stink-bid strategy.

For every resolved market in the lookback window:
  1. Walk its price history at the bot's refresh cadence.
  2. At each step where the favorite price is in the bid window, compute
     the bid (favorite * (1 - discount)) and project a GTC limit order
     that stays open until the next refresh step.
  3. If the favorite-side price prints at-or-below the bid in that window,
     count it as filled. (OPTIMISTIC — see __init__ caveat.)
  4. For filled bids, check the market's final outcome to compute PnL:
     payoff = (1.0 - bid) if favorite won else -bid (per share).

Output: per-market trades + overall stats (count, fill rate, win rate,
mean PnL per attempt, mean PnL per fill, total notional, total PnL).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import GlobalConfig
from .history import (
    PricePoint,
    ResolvedMarket,
    fetch_price_history,
    fetch_resolved_markets,
)

log = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    market_slug: str
    question: str
    favorite_outcome: str
    favorite_price: float
    bid: float
    filled: bool
    won: bool | None  # None = filled but market didn't cleanly resolve
    pnl_per_share: float
    notional: float
    pnl_usdc: float
    fill_ts: int | None
    resolve_ts: int | None


@dataclass
class BacktestStats:
    tag: str
    days: int
    markets_considered: int
    markets_with_history: int
    attempts: int
    fills: int
    wins: int
    losses: int
    unresolved_fills: int
    total_pnl_usdc: float
    total_notional_usdc: float
    fill_rate: float
    win_rate_given_fill: float
    mean_pnl_per_attempt: float
    mean_pnl_per_fill: float
    trades: list[BacktestTrade] = field(default_factory=list)

    def summary_lines(self) -> list[str]:
        roi = (self.total_pnl_usdc / self.total_notional_usdc * 100) if self.total_notional_usdc else 0.0
        return [
            f"Backtest — tag={self.tag}  lookback={self.days}d",
            f"  Markets considered:   {self.markets_considered}",
            f"  Markets with prices:  {self.markets_with_history}",
            f"  Stink-bid attempts:   {self.attempts}",
            f"  Simulated fills:      {self.fills}  (fill rate {self.fill_rate*100:.1f}%)",
            f"  Wins / losses:        {self.wins} / {self.losses}"
            + (f"  ({self.unresolved_fills} unresolved)" if self.unresolved_fills else ""),
            f"  Win rate | fill:      {self.win_rate_given_fill*100:.1f}%",
            f"  Mean PnL / attempt:   ${self.mean_pnl_per_attempt:.3f}",
            f"  Mean PnL / fill:      ${self.mean_pnl_per_fill:.3f}",
            f"  Total PnL:            ${self.total_pnl_usdc:.2f}  on  ${self.total_notional_usdc:.2f} notional  ({roi:+.1f}% ROI)",
            "",
            "  CAVEATS:",
            "    - Fills assume our bid is at the front of the queue (optimistic).",
            "    - Survivorship: we only see markets that resolved cleanly.",
            "    - No slippage, no fees, no partial fills modelled.",
            "    - Real-world results will be materially worse. Don't size from these numbers alone.",
        ]


def _walk_bids(
    history: list[PricePoint],
    cfg: GlobalConfig,
) -> list[tuple[int, float, float]]:
    """Yield (ts, favorite_price_at_post, bid) triples at refresh cadence.

    Only emits when favorite_price is inside the strategy's window.
    """
    if not history:
        return []
    step = max(60, cfg.refresh_interval)  # seconds
    out: list[tuple[int, float, float]] = []
    last_ts = history[0].ts
    for pt in history:
        if pt.ts - last_ts < step and out:
            continue
        last_ts = pt.ts
        # Treat price as the favorite-side price if it's > 0.5; otherwise the
        # favorite is the other side and the favorite-side price is 1 - p.
        fav_px = pt.price if pt.price >= 0.5 else 1.0 - pt.price
        if fav_px < cfg.min_favorite_price or fav_px > cfg.max_favorite_price:
            continue
        bid = round(fav_px * (1 - cfg.discount), 4)
        if bid <= 0 or bid >= fav_px:
            continue
        out.append((pt.ts, fav_px, bid))
    return out


def _check_fill(
    history: list[PricePoint],
    post_ts: int,
    bid: float,
    cfg: GlobalConfig,
    favorite_side: int,  # 0 if our side is the higher-price side at post time
) -> int | None:
    """If a later print within one refresh cycle reaches the bid, return the fill ts."""
    window_end = post_ts + cfg.refresh_interval
    for pt in history:
        if pt.ts <= post_ts:
            continue
        if pt.ts > window_end:
            return None
        # Our side's price at this moment:
        side_px = pt.price if favorite_side == 0 else 1.0 - pt.price
        if side_px <= bid:
            return pt.ts
    return None


def _simulate_market(
    market: ResolvedMarket,
    history: list[PricePoint],
    cfg: GlobalConfig,
) -> list[BacktestTrade]:
    trades: list[BacktestTrade] = []
    if not history:
        return trades
    winner_idx = market.winning_index()
    # The CLOB prices-history is per-token. For binary markets we fetch the
    # token that was the favorite *most often* and use it. Caller passes the
    # series for the higher-volume token (we fetch outcomes[0]'s token below).
    bids = _walk_bids(history, cfg)
    # Sort history by ts for fill-window scan.
    history = sorted(history, key=lambda p: p.ts)
    for ts, fav_px, bid in bids:
        # Did the favorite side belong to token 0 or token 1 at this moment?
        fav_token_idx = 0 if history and _price_at(history, ts) >= 0.5 else 1
        fill_ts = _check_fill(history, ts, bid, cfg, favorite_side=fav_token_idx)
        notional = round(cfg.bet_size_usdc, 2)
        size = notional / bid if bid > 0 else 0
        if fill_ts is None:
            trades.append(BacktestTrade(
                market_slug=market.slug,
                question=market.question,
                favorite_outcome=market.outcomes[fav_token_idx],
                favorite_price=round(fav_px, 4),
                bid=bid,
                filled=False,
                won=None,
                pnl_per_share=0.0,
                notional=0.0,
                pnl_usdc=0.0,
                fill_ts=None,
                resolve_ts=int(market.end_date.timestamp()) if market.end_date else None,
            ))
            continue
        if winner_idx is None:
            won = None
            pnl_per_share = 0.0
        else:
            won = (winner_idx == fav_token_idx)
            pnl_per_share = (1.0 - bid) if won else -bid
        pnl_usdc = round(pnl_per_share * size, 4)
        trades.append(BacktestTrade(
            market_slug=market.slug,
            question=market.question,
            favorite_outcome=market.outcomes[fav_token_idx],
            favorite_price=round(fav_px, 4),
            bid=bid,
            filled=True,
            won=won,
            pnl_per_share=round(pnl_per_share, 4),
            notional=notional,
            pnl_usdc=pnl_usdc,
            fill_ts=fill_ts,
            resolve_ts=int(market.end_date.timestamp()) if market.end_date else None,
        ))
    return trades


def _price_at(history: list[PricePoint], ts: int) -> float:
    """Nearest-prior price; assumes history is sorted ascending."""
    last = history[0].price
    for pt in history:
        if pt.ts > ts:
            break
        last = pt.price
    return last


def run_backtest(
    cfg: GlobalConfig,
    *,
    gamma_host: str,
    clob_host: str,
    tag: str,
    days: int = 30,
    market_limit: int = 200,
) -> BacktestStats:
    markets = fetch_resolved_markets(gamma_host, tag, days_back=days, limit=market_limit)
    all_trades: list[BacktestTrade] = []
    markets_with_history = 0
    for m in markets:
        # Fetch history for outcome-0 token; price = P(outcomes[0])
        history = fetch_price_history(clob_host, m.token_ids[0], interval="1h")
        if not history:
            continue
        markets_with_history += 1
        all_trades.extend(_simulate_market(m, history, cfg))

    attempts = len(all_trades)
    fills_list = [t for t in all_trades if t.filled]
    wins = sum(1 for t in fills_list if t.won is True)
    losses = sum(1 for t in fills_list if t.won is False)
    unresolved = sum(1 for t in fills_list if t.won is None)
    total_pnl = sum(t.pnl_usdc for t in fills_list)
    total_notional = sum(t.notional for t in fills_list)
    fill_rate = (len(fills_list) / attempts) if attempts else 0.0
    decided = wins + losses
    win_rate = (wins / decided) if decided else 0.0
    mean_per_attempt = (total_pnl / attempts) if attempts else 0.0
    mean_per_fill = (total_pnl / len(fills_list)) if fills_list else 0.0

    return BacktestStats(
        tag=tag,
        days=days,
        markets_considered=len(markets),
        markets_with_history=markets_with_history,
        attempts=attempts,
        fills=len(fills_list),
        wins=wins,
        losses=losses,
        unresolved_fills=unresolved,
        total_pnl_usdc=round(total_pnl, 2),
        total_notional_usdc=round(total_notional, 2),
        fill_rate=round(fill_rate, 4),
        win_rate_given_fill=round(win_rate, 4),
        mean_pnl_per_attempt=round(mean_per_attempt, 4),
        mean_pnl_per_fill=round(mean_per_fill, 4),
        trades=all_trades,
    )


def stats_to_dict(s: BacktestStats) -> dict[str, Any]:
    return {
        "tag": s.tag,
        "days": s.days,
        "markets_considered": s.markets_considered,
        "markets_with_history": s.markets_with_history,
        "attempts": s.attempts,
        "fills": s.fills,
        "wins": s.wins,
        "losses": s.losses,
        "unresolved_fills": s.unresolved_fills,
        "total_pnl_usdc": s.total_pnl_usdc,
        "total_notional_usdc": s.total_notional_usdc,
        "fill_rate": s.fill_rate,
        "win_rate_given_fill": s.win_rate_given_fill,
        "mean_pnl_per_attempt": s.mean_pnl_per_attempt,
        "mean_pnl_per_fill": s.mean_pnl_per_fill,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
