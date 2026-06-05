"""Historical data access for backtesting and PnL.

Two sources:
    - Gamma `markets?closed=true` for resolved markets with their final outcome
    - CLOB `prices-history` for per-token price series
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from ..markets import _parse_dt, _parse_json_list  # reuse private helpers

log = logging.getLogger(__name__)


@dataclass
class ResolvedMarket:
    condition_id: str
    question: str
    slug: str
    end_date: datetime | None
    outcomes: list[str]
    token_ids: list[str]
    # Final outcome prices: [1.0, 0.0] means outcome[0] won, [0.0, 1.0] means outcome[1] won.
    final_prices: list[float]
    raw: dict[str, Any]

    def winning_index(self) -> int | None:
        if len(self.final_prices) != 2:
            return None
        if self.final_prices[0] >= 0.99 and self.final_prices[1] <= 0.01:
            return 0
        if self.final_prices[1] >= 0.99 and self.final_prices[0] <= 0.01:
            return 1
        return None  # not cleanly resolved (cancelled, refunded, etc.)


@dataclass
class PricePoint:
    ts: int   # unix seconds
    price: float


def fetch_resolved_markets(
    gamma_host: str,
    tag: str,
    *,
    days_back: int = 30,
    limit: int = 500,
) -> list[ResolvedMarket]:
    """Resolved binary markets for the given tag within the lookback window."""
    url = f"{gamma_host.rstrip('/')}/markets"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    out: list[ResolvedMarket] = []
    offset = 0
    page_size = 100
    while len(out) < limit:
        params = {
            "closed": "true",
            "archived": "false",
            "limit": page_size,
            "offset": offset,
            "order": "endDate",
            "ascending": "false",
            "tag_slug": tag,
        }
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            rows = r.json() or []
        except Exception as e:  # noqa: BLE001
            log.warning("gamma resolved fetch failed tag=%s err=%s", tag, e)
            break
        if not rows:
            break
        stop = False
        for row in rows:
            end_dt = _parse_dt(row.get("endDate"))
            if end_dt and end_dt < cutoff:
                stop = True
                break
            token_ids = _parse_json_list(row.get("clobTokenIds"))
            outcomes = _parse_json_list(row.get("outcomes"))
            try:
                final = [float(p) for p in _parse_json_list(row.get("outcomePrices")) or []]
            except (TypeError, ValueError):
                continue
            if len(token_ids) != 2 or len(outcomes) != 2 or len(final) != 2:
                continue
            out.append(ResolvedMarket(
                condition_id=row.get("conditionId", ""),
                question=row.get("question", ""),
                slug=row.get("slug", ""),
                end_date=end_dt,
                outcomes=outcomes,
                token_ids=[str(t) for t in token_ids],
                final_prices=final,
                raw=row,
            ))
            if len(out) >= limit:
                break
        if stop or len(rows) < page_size:
            break
        offset += page_size
    log.info("gamma resolved: %d markets tag=%s within %dd", len(out), tag, days_back)
    return out


def fetch_price_history(
    clob_host: str,
    token_id: str,
    *,
    interval: str = "1h",
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> list[PricePoint]:
    """Time series of token price (0..1). Returns [] on error."""
    url = f"{clob_host.rstrip('/')}/prices-history"
    params: dict[str, Any] = {"market": token_id, "interval": interval}
    if start_ts:
        params["startTs"] = start_ts
    if end_ts:
        params["endTs"] = end_ts
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:  # noqa: BLE001
        log.debug("prices-history failed token=%s err=%s", token_id[:12], e)
        return []
    rows = data.get("history") or []
    out: list[PricePoint] = []
    for row in rows:
        try:
            out.append(PricePoint(ts=int(row["t"]), price=float(row["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out
