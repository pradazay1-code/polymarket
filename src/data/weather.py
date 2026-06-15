"""NOAA National Weather Service forecast client.

api.weather.gov is free, no API key required, no rate limits documented
(they request a User-Agent header that identifies you). The flow:

  1. GET /points/{lat},{lon}     -> returns the grid office + grid (X, Y)
  2. GET /gridpoints/{office}/{X},{Y}/forecast  -> daily forecast periods

Periods alternate day / night. We collapse them into per-date highs
(day periods, isDaytime=true) and lows (night periods).

Forecast accuracy degrades with horizon. Empirical RMSE on the daytime
high (NWS verification reports):
    1d out: ~2°F
    2d:     ~2.5°F
    3d:     ~3.5°F
    5d:     ~5°F
    7d:     ~6°F

We use this as the sigma of a normal-CDF-style probability model for
threshold markets. Past 7 days out, sigma blows up and the strategy
shouldn't bet — the runner enforces that via max_days_out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import erf, sqrt
from typing import Any

import requests

log = logging.getLogger(__name__)

NWS_HOST = "https://api.weather.gov"
USER_AGENT = "polymarket-climate-bot (contact: github.com/pradazay1-code/polymarket)"

# Major US cities — lat/lon used for NWS lookup. Add more as Polymarket adds markets.
CITIES: dict[str, tuple[float, float]] = {
    "nyc":          (40.7128, -74.0060),
    "new york":     (40.7128, -74.0060),
    "manhattan":    (40.7831, -73.9712),
    "boston":       (42.3601, -71.0589),
    "philadelphia": (39.9526, -75.1652),
    "philly":       (39.9526, -75.1652),
    "washington":   (38.9072, -77.0369),
    "dc":           (38.9072, -77.0369),
    "miami":        (25.7617, -80.1918),
    "atlanta":      (33.7490, -84.3880),
    "orlando":      (28.5383, -81.3792),
    "chicago":      (41.8781, -87.6298),
    "minneapolis":  (44.9778, -93.2650),
    "detroit":      (42.3314, -83.0458),
    "dallas":       (32.7767, -96.7970),
    "houston":      (29.7604, -95.3698),
    "austin":       (30.2672, -97.7431),
    "denver":       (39.7392, -104.9903),
    "phoenix":      (33.4484, -112.0740),
    "las vegas":    (36.1699, -115.1398),
    "vegas":        (36.1699, -115.1398),
    "los angeles":  (34.0522, -118.2437),
    "la":           (34.0522, -118.2437),
    "san diego":    (32.7157, -117.1611),
    "san francisco":(37.7749, -122.4194),
    "sf":           (37.7749, -122.4194),
    "seattle":      (47.6062, -122.3321),
    "portland":     (45.5152, -122.6784),
    "salt lake city":(40.7608, -111.8910),
}

# Forecast-error sigma (°F) as a function of days from today.
SIGMA_BY_DAYS_OUT = {0: 1.5, 1: 2.0, 2: 2.5, 3: 3.5, 4: 4.0, 5: 5.0, 6: 5.5, 7: 6.0}


@dataclass
class DayForecast:
    date: date
    high_f: float | None
    low_f: float | None
    days_out: int


def city_coords(name: str) -> tuple[float, float] | None:
    return CITIES.get(name.lower().strip())


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x < mu else 0.0
    return 0.5 * (1 + erf((x - mu) / (sigma * sqrt(2))))


def prob_above_threshold(forecast_f: float, threshold_f: float, days_out: int) -> float:
    """Model probability that the day's actual high exceeds threshold."""
    sigma = SIGMA_BY_DAYS_OUT.get(days_out)
    if sigma is None:
        # Beyond 7 days, blow it up so the strategy refuses to bet.
        sigma = 8.0 + (days_out - 7) * 2.0
    return 1.0 - normal_cdf(threshold_f, forecast_f, sigma)


_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/geo+json"})


def _get_json(url: str) -> dict[str, Any] | None:
    try:
        r = _session.get(url, timeout=15)
        if r.status_code >= 400:
            log.warning("nws %s -> %d %s", url, r.status_code, r.text[:200])
            return None
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("nws %s failed: %s", url, e)
        return None


def _gridpoint(lat: float, lon: float) -> tuple[str, int, int] | None:
    data = _get_json(f"{NWS_HOST}/points/{lat:.4f},{lon:.4f}")
    if not data:
        return None
    p = data.get("properties") or {}
    office = p.get("gridId")
    gx = p.get("gridX")
    gy = p.get("gridY")
    if not (office and gx is not None and gy is not None):
        return None
    return office, int(gx), int(gy)


def get_forecast(lat: float, lon: float) -> list[DayForecast]:
    """Daily forecast (today + ~6 future days) collapsed to per-date highs/lows."""
    grid = _gridpoint(lat, lon)
    if not grid:
        return []
    office, gx, gy = grid
    data = _get_json(f"{NWS_HOST}/gridpoints/{office}/{gx},{gy}/forecast")
    if not data:
        return []
    periods = (data.get("properties") or {}).get("periods") or []
    today = datetime.now(timezone.utc).date()
    by_date: dict[date, DayForecast] = {}
    for p in periods:
        start = p.get("startTime")
        if not start:
            continue
        try:
            d = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
        except ValueError:
            continue
        is_day = bool(p.get("isDaytime"))
        temp = p.get("temperature")
        if temp is None:
            continue
        f = float(temp) if p.get("temperatureUnit", "F").upper() == "F" else float(temp) * 9 / 5 + 32
        days_out = (d - today).days
        if d not in by_date:
            by_date[d] = DayForecast(date=d, high_f=None, low_f=None, days_out=days_out)
        if is_day:
            by_date[d].high_f = f
        else:
            by_date[d].low_f = f
    return sorted(by_date.values(), key=lambda x: x.date)


def forecast_for_date(forecast: list[DayForecast], target: date) -> DayForecast | None:
    for fc in forecast:
        if fc.date == target:
            return fc
    return None
