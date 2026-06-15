"""Climate-forecast strategy.

For each climate market:
  1. Parse the question to extract (city, threshold °F, target date).
     If we can't parse confidently, skip.
  2. If the target date is more than max_days_out away, skip — NWS isn't
     accurate enough past about a week.
  3. Pull NOAA's daily forecast for the city. If no high temp for that
     date, skip.
  4. Compute model_prob = P(actual high > threshold) using a normal CDF
     with sigma sized to the forecast horizon.
  5. Compare model_prob to the market's YES price. If they disagree by
     more than `min_edge`, place a limit order on the cheaper side at
     a small discount.

Risk controls (read from bot config under `extra`):
  - min_edge:         minimum probability gap to bet (default 0.05)
  - max_days_out:     don't bet on dates >N days away (default 7)
  - bid_discount:     post bid at price * (1 - bid_discount) (default 0.03)
  - cancel_before_refresh, dry_run, max_open_notional, bet_size_usdc come
    from GlobalConfig (shared with the stink-bid strategy).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..client import PolymarketClient
from ..config import GlobalConfig
from ..data import weather
from ..markets import Market
from .stink_bid import Intent, PlanResult  # reuse the result shape

log = logging.getLogger(__name__)


# --- question parser ---------------------------------------------------------

_TEMP_RE = re.compile(r"\b(\d{2,3})\s*(?:°\s*F|degrees|°|F\b)", re.IGNORECASE)
_DATE_MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_MONTH_NAMES = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_DAY_RE = re.compile(
    r"\b(" + "|".join(_MONTH_NAMES.keys()) + r")\s+(\d{1,2})\b",
    re.IGNORECASE,
)
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


@dataclass
class Parsed:
    city: str
    lat: float
    lon: float
    threshold_f: float
    target_date: date
    days_out: int


def _find_city(text: str) -> tuple[str, float, float] | None:
    lower = text.lower()
    # Longest-name-first so "new york" matches before "york".
    for name in sorted(weather.CITIES.keys(), key=len, reverse=True):
        if name in lower:
            lat, lon = weather.CITIES[name]
            return name, lat, lon
    return None


def _find_threshold(text: str) -> float | None:
    m = _TEMP_RE.search(text)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    if 0 <= v <= 130:
        return v
    return None


def _find_date(text: str, now: date) -> date | None:
    lower = text.lower()
    if "tomorrow" in lower:
        return now + timedelta(days=1)
    if "today" in lower or "tonight" in lower:
        return now
    # "this Monday" / "next Friday"
    for word, idx in _WEEKDAYS.items():
        if word in lower:
            ahead = (idx - now.weekday()) % 7
            if "next" in lower and ahead == 0:
                ahead = 7
            elif "this" in lower and ahead == 0:
                pass
            elif ahead == 0:
                ahead = 7
            return now + timedelta(days=ahead)
    # "June 17"
    mmm = _MONTH_DAY_RE.search(lower)
    if mmm:
        month = _MONTH_NAMES[mmm.group(1)]
        day = int(mmm.group(2))
        year = now.year
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d < now - timedelta(days=30):
            d = date(year + 1, month, day)
        return d
    # "6/17" or "6/17/2026"
    mdy = _DATE_MDY_RE.search(text)
    if mdy:
        m_ = int(mdy.group(1))
        d_ = int(mdy.group(2))
        y_raw = mdy.group(3)
        y_ = now.year if not y_raw else (2000 + int(y_raw)) if len(y_raw) == 2 else int(y_raw)
        try:
            return date(y_, m_, d_)
        except ValueError:
            return None
    return None


def parse_climate_question(question: str) -> Parsed | None:
    if not question:
        return None
    city = _find_city(question)
    threshold = _find_threshold(question)
    if not city or threshold is None:
        return None
    today = datetime.now(timezone.utc).date()
    target = _find_date(question, today)
    if target is None:
        return None
    days_out = (target - today).days
    if days_out < 0:
        return None
    name, lat, lon = city
    return Parsed(
        city=name,
        lat=lat,
        lon=lon,
        threshold_f=threshold,
        target_date=target,
        days_out=days_out,
    )


# --- strategy ---------------------------------------------------------------


def _yes_index(market: Market) -> int:
    for i, o in enumerate(market.outcomes):
        if o.strip().lower() in ("yes", "y", "true"):
            return i
    return 0


class ClimateForecastStrategy:
    def __init__(self, client: PolymarketClient, cfg: GlobalConfig,
                 label: str, *, extra: dict[str, Any] | None = None):
        self.client = client
        self.cfg = cfg
        self.label = label
        extra = extra or {}
        self.min_edge = float(extra.get("min_edge", 0.05))
        self.max_days_out = int(extra.get("max_days_out", 7))
        self.bid_discount = float(extra.get("bid_discount", 0.03))
        # Cache one forecast per city per cycle.
        self._forecast_cache: dict[str, list] = {}

    def _get_forecast(self, parsed: Parsed):
        key = parsed.city
        if key not in self._forecast_cache:
            self._forecast_cache[key] = weather.get_forecast(parsed.lat, parsed.lon)
        return self._forecast_cache[key]

    def plan(self, markets: list[Market]) -> PlanResult:
        self._forecast_cache.clear()
        result = PlanResult(inspected=len(markets))
        for m in markets:
            slug = m.slug or m.condition_id[:10]
            parsed = parse_climate_question(m.question)
            if not parsed:
                result.skipped.append({"slug": slug, "reason": "couldn't parse city+temp+date"})
                continue
            if parsed.days_out > self.max_days_out:
                result.skipped.append({
                    "slug": slug,
                    "reason": f"target {parsed.days_out}d out > max {self.max_days_out}",
                })
                continue
            forecast = self._get_forecast(parsed)
            if not forecast:
                result.skipped.append({"slug": slug, "reason": f"no NWS forecast for {parsed.city}"})
                continue
            day_fc = weather.forecast_for_date(forecast, parsed.target_date)
            if not day_fc or day_fc.high_f is None:
                result.skipped.append({
                    "slug": slug,
                    "reason": f"no high temp forecast for {parsed.target_date.isoformat()}",
                })
                continue

            model_prob = weather.prob_above_threshold(
                day_fc.high_f, parsed.threshold_f, parsed.days_out,
            )
            yes_idx = _yes_index(m)
            no_idx = 1 - yes_idx
            yes_price = m.last_prices[yes_idx]
            edge_yes = model_prob - yes_price          # positive => YES is underpriced
            edge_no = (1 - model_prob) - m.last_prices[no_idx]

            # Pick the side with bigger edge above min_edge.
            if edge_yes >= self.min_edge and edge_yes >= edge_no:
                side = "Yes"
                token_id = m.token_ids[yes_idx]
                market_price = yes_price
                edge = edge_yes
            elif edge_no >= self.min_edge:
                side = "No"
                token_id = m.token_ids[no_idx]
                market_price = m.last_prices[no_idx]
                edge = edge_no
            else:
                result.skipped.append({
                    "slug": slug,
                    "reason": (
                        f"edge below threshold: model_yes={model_prob:.2f} "
                        f"market_yes={yes_price:.2f} edge={max(edge_yes, edge_no):.2f}"
                    ),
                })
                continue

            book = self.client.get_book(token_id)
            if book is None:
                result.skipped.append({"slug": slug, "reason": "no order book / no liquidity"})
                continue
            tick = book.tick_size or 0.01
            raw_bid = market_price * (1 - self.bid_discount)
            ticks = max(1, int(raw_bid / tick))
            bid = round(ticks * tick, 6)
            if bid >= market_price or bid <= 0:
                result.skipped.append({"slug": slug, "reason": f"bid {bid} >= price {market_price}"})
                continue

            size_shares = round(self.cfg.bet_size_usdc / bid, 2)
            min_size = book.min_order_size or 5.0
            if size_shares < min_size:
                bumped = round(min_size * bid, 2)
                if bumped > self.cfg.bet_size_usdc * 1.5:
                    result.skipped.append({
                        "slug": slug,
                        "reason": f"min_size {min_size} would cost ${bumped} > 1.5x bet_size",
                    })
                    continue
                size_shares = min_size

            notional = round(bid * size_shares, 2)
            reason = (
                f"forecast high={day_fc.high_f:.1f}°F vs threshold {parsed.threshold_f:.0f}°F "
                f"({parsed.days_out}d out) -> P(yes)={model_prob:.2f} "
                f"market={yes_price:.2f}; betting {side} edge={edge:+.2f}"
            )
            result.intents.append(Intent(
                market_question=m.question,
                market_slug=slug,
                condition_id=m.condition_id,
                end_date=m.end_date.isoformat() if m.end_date else None,
                token_id=token_id,
                outcome=side,
                favorite_price=market_price,
                bid_price=bid,
                size_shares=size_shares,
                notional=notional,
                tick_size=tick,
                min_order_size=min_size,
                reason=reason,
            ))
        return result

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
                posted.append({
                    "dry_run": True,
                    "intent": intent.__dict__,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                budget -= intent.notional
                continue
            resp = self.client.place_limit_buy(
                intent.token_id, intent.bid_price, intent.size_shares, tick_size=intent.tick_size,
            )
            if resp:
                posted.append({
                    "resp": resp,
                    "intent": intent.__dict__,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
                budget -= intent.notional
        return posted
