"""Discover Polymarket markets for a given sport via the public Gamma API.

Gamma is Polymarket's catalog API (no auth required). We filter by tag, open
status, and a soon-ending window so the bot only touches markets that resolve
within a tradable timeframe.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass
class Market:
    condition_id: str
    question: str
    end_date: datetime | None
    # Binary markets have two outcomes; each maps to a CLOB token id.
    outcomes: list[str]
    token_ids: list[str]
    # Last-traded probabilities in [0, 1].
    last_prices: list[float]
    slug: str
    raw: dict[str, Any]

    def favorite_index(self) -> int:
        return max(range(len(self.last_prices)), key=lambda i: self.last_prices[i])

    def favorite_price(self) -> float:
        return self.last_prices[self.favorite_index()]

    def favorite_token_id(self) -> str:
        return self.token_ids[self.favorite_index()]

    def favorite_outcome(self) -> str:
        return self.outcomes[self.favorite_index()]


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Gamma sends ISO-8601 like "2026-05-20T18:00:00Z"
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_json_list(field: Any) -> list[Any]:
    """Gamma sometimes returns lists as JSON-encoded strings."""
    if isinstance(field, list):
        return field
    if isinstance(field, str):
        import json
        try:
            return json.loads(field)
        except json.JSONDecodeError:
            return []
    return []


def fetch_markets(
    gamma_host: str,
    tag: str,
    *,
    ending_within_hours: int = 36,
    limit: int = 200,
) -> list[Market]:
    """Return open binary markets for the given tag that end within a window."""
    url = f"{gamma_host.rstrip('/')}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "archived": "false",
        "limit": limit,
        "order": "endDate",
        "ascending": "true",
        "tag_slug": tag,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        rows = r.json() or []
    except Exception as e:  # noqa: BLE001
        log.warning("gamma fetch failed tag=%s err=%s", tag, e)
        return []

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=ending_within_hours)
    out: list[Market] = []
    for row in rows:
        end_dt = _parse_dt(row.get("endDate"))
        # Skip markets that already ended or end past our window.
        if end_dt and (end_dt < now or end_dt > cutoff):
            continue
        token_ids = _parse_json_list(row.get("clobTokenIds"))
        outcomes = _parse_json_list(row.get("outcomes"))
        prices = [float(p) for p in _parse_json_list(row.get("outcomePrices")) or []]
        if len(token_ids) != 2 or len(outcomes) != 2 or len(prices) != 2:
            # Only handle binary markets.
            continue
        out.append(
            Market(
                condition_id=row.get("conditionId", ""),
                question=row.get("question", ""),
                end_date=end_dt,
                outcomes=outcomes,
                token_ids=[str(t) for t in token_ids],
                last_prices=prices,
                slug=row.get("slug", ""),
                raw=row,
            )
        )
    log.info("gamma: %d markets for tag=%s (ending within %dh)", len(out), tag, ending_within_hours)
    return out
