"""Edge scoring: rank live planning intents by expected value.

Uses a backtest result (run on the same tag) to estimate, per favorite-
price bucket, P(fill) and P(win|fill). Then for each live intent:

    EV_per_share = fill_rate * ( win_rate * (1 - bid) - (1 - win_rate) * bid )
    EV_dollars   = EV_per_share * size_shares

We bucket on the favorite price because hit rates are strongly
price-dependent — a 60¢ favorite has very different volatility than
an 85¢ favorite.

This is descriptive, not predictive. It tells you "historically this
type of bid had X EV"; future markets may diverge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..strategies.stink_bid import Intent
from .backtest import BacktestStats, BacktestTrade

# Favorite-price buckets (inclusive of lower bound).
BUCKETS = [(0.50, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 1.00)]


@dataclass
class BucketStat:
    low: float
    high: float
    attempts: int
    fills: int
    wins: int
    decided: int

    @property
    def fill_rate(self) -> float:
        return (self.fills / self.attempts) if self.attempts else 0.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.decided) if self.decided else 0.0


@dataclass
class EdgeRow:
    market_slug: str
    favorite_outcome: str
    favorite_price: float
    bid: float
    size_shares: float
    bucket: tuple[float, float]
    bucket_attempts: int
    fill_rate: float
    win_rate_given_fill: float
    ev_per_share: float
    ev_dollars: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_slug": self.market_slug,
            "favorite_outcome": self.favorite_outcome,
            "favorite_price": self.favorite_price,
            "bid": self.bid,
            "size_shares": self.size_shares,
            "bucket": f"{self.bucket[0]:.2f}-{self.bucket[1]:.2f}",
            "bucket_attempts": self.bucket_attempts,
            "fill_rate": round(self.fill_rate, 4),
            "win_rate_given_fill": round(self.win_rate_given_fill, 4),
            "ev_per_share": round(self.ev_per_share, 4),
            "ev_dollars": round(self.ev_dollars, 4),
        }


def _bucket_for(px: float) -> tuple[float, float]:
    for low, high in BUCKETS:
        if low <= px < high:
            return (low, high)
    return BUCKETS[-1]


def bucket_stats(stats: BacktestStats) -> dict[tuple[float, float], BucketStat]:
    out: dict[tuple[float, float], BucketStat] = {
        b: BucketStat(low=b[0], high=b[1], attempts=0, fills=0, wins=0, decided=0)
        for b in BUCKETS
    }
    for t in stats.trades:
        b = _bucket_for(t.favorite_price)
        bs = out[b]
        bs.attempts += 1
        if t.filled:
            bs.fills += 1
            if t.won is True:
                bs.wins += 1
                bs.decided += 1
            elif t.won is False:
                bs.decided += 1
    return out


def score_intents(intents: list[Intent], stats: BacktestStats) -> list[EdgeRow]:
    buckets = bucket_stats(stats)
    rows: list[EdgeRow] = []
    for it in intents:
        b = _bucket_for(it.favorite_price)
        bs = buckets[b]
        # If we have no data in this bucket, fall back to overall rates.
        if bs.attempts < 5:
            fill_rate = stats.fill_rate
            win_rate = stats.win_rate_given_fill
            bucket_n = stats.attempts
        else:
            fill_rate = bs.fill_rate
            win_rate = bs.win_rate
            bucket_n = bs.attempts
        ev_per_share = fill_rate * (win_rate * (1 - it.bid_price) - (1 - win_rate) * it.bid_price)
        ev_dollars = ev_per_share * it.size_shares
        rows.append(EdgeRow(
            market_slug=it.market_slug,
            favorite_outcome=it.outcome,
            favorite_price=round(it.favorite_price, 4),
            bid=round(it.bid_price, 4),
            size_shares=it.size_shares,
            bucket=b,
            bucket_attempts=bucket_n,
            fill_rate=fill_rate,
            win_rate_given_fill=win_rate,
            ev_per_share=ev_per_share,
            ev_dollars=ev_dollars,
        ))
    rows.sort(key=lambda r: r.ev_dollars, reverse=True)
    return rows
