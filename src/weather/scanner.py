"""Weather edge scanner — compares Kalshi weather market prices to NWS forecasts.

Fetches Kalshi weather markets closing today, parses each title into
structured fields (city, metric, threshold, direction), pulls the
corresponding NOAA/NWS forecast, and identifies markets where the
NWS-implied probability diverges from the Kalshi price by at least
``min_edge``.
"""

from __future__ import annotations

import datetime
from typing import Any

from src.fetchers.kalshi import KalshiFetcher
from src.weather.market_parser import parse_weather_market
from src.weather.noaa import get_forecast


def get_nws_probability(
    parsed: dict[str, Any],
    forecast: list[dict[str, Any]],
) -> float | None:
    """Convert an NWS forecast to a 0-1 probability for a parsed market.

    Args:
        parsed: Output of ``parse_weather_market`` with keys ``date``,
            ``metric``, ``threshold``, and ``direction``.
        forecast: List of hourly forecast period dicts as returned by
            ``get_forecast``, each containing ``time``, ``temp_f``,
            ``precip_pct``, and ``wind_mph``.

    Returns:
        A float in [0, 1] representing the estimated probability that
        the market condition is met, or ``None`` if no forecast data
        matches the target date.
    """
    if not forecast:
        return None

    target_date: datetime.date = parsed["date"]
    metric: str = parsed["metric"]
    threshold: float = parsed["threshold"]
    direction: str = parsed["direction"]

    # Filter forecast periods to the target date.
    day_periods = [
        p for p in forecast if p["time"].date() == target_date
    ]
    if not day_periods:
        return None

    if metric == "precip":
        avg_precip = sum(p["precip_pct"] for p in day_periods) / len(
            day_periods
        )
        prob = avg_precip / 100.0
        return prob if direction == "above" else 1.0 - prob

    if metric == "high_temp":
        observed = max(p["temp_f"] for p in day_periods)
    elif metric == "low_temp":
        observed = min(p["temp_f"] for p in day_periods)
    elif metric == "wind":
        observed = max(p["wind_mph"] for p in day_periods)
    else:
        return None

    return _threshold_probability(observed, threshold, direction)


def _threshold_probability(
    observed: float,
    threshold: float,
    direction: str,
) -> float:
    """Map the difference between an observed value and a threshold to a
    rough probability using a three-tier heuristic.

    Args:
        observed: The forecast value (e.g. max temp, min temp, max wind).
        threshold: The market's threshold value.
        direction: ``"above"`` or ``"below"``.

    Returns:
        A probability float: 0.85, 0.50, or 0.15.
    """
    diff = observed - threshold

    if direction == "above":
        if diff > 3:
            return 0.85
        if abs(diff) <= 3:
            return 0.50
        return 0.15
    else:
        # direction == "below"
        if diff < -3:
            return 0.85
        if abs(diff) <= 3:
            return 0.50
        return 0.15


def scan_weather_markets(
    fetcher: KalshiFetcher,
    min_edge: float = 0.05,
) -> list[dict[str, Any]]:
    """Fetch Kalshi weather markets and find NWS edge opportunities.

    Retrieves all open Kalshi markets, filters to weather markets
    closing today, and compares the Kalshi-implied probability to the
    NWS forecast probability. Returns only markets where the absolute
    edge exceeds ``min_edge``.

    Args:
        fetcher: An authenticated ``KalshiFetcher`` instance.
        min_edge: Minimum absolute probability edge to include in
            results. Defaults to 0.05 (5 percentage points).

    Returns:
        A list of dicts, each containing:
            - ``market``: The original ``Market`` object.
            - ``side``: ``"yes"`` or ``"no"``.
            - ``ask_cents``: The ask price in cents for the chosen side.
            - ``kalshi_prob``: The Kalshi-implied probability
              (``ask_cents / 100``).
            - ``nws_prob``: The NWS-derived probability.
            - ``edge``: ``abs(nws_prob - kalshi_prob)``.
    """
    today = datetime.date.today()
    opportunities: list[dict[str, Any]] = []

    _WEATHER_CATEGORIES = ["Climate and Weather"]

    try:
        markets = fetcher.get_markets(categories=_WEATHER_CATEGORIES)
    except Exception as exc:
        print(f"[weather] failed to fetch markets: {exc}")
        return opportunities

    for market in markets:
        try:
            # Skip markets with no selections or zero yes_ask.
            if not market.selections:
                continue

            yes_ask: int = market.selections[0].metadata.get(
                "yes_ask", 0
            )
            no_ask: int = market.selections[0].metadata.get("no_ask", 0)

            if yes_ask == 0:
                continue

            # Only include markets closing today.
            close_time = market.starts_at
            if close_time is None:
                continue

            if close_time.date() != today:
                continue

            # Parse market title for weather fields.
            parsed = parse_weather_market(market)
            if parsed is None:
                continue

            # Fetch NWS forecast for the market's city.
            forecast = get_forecast(parsed["lat"], parsed["lon"])
            nws_prob = get_nws_probability(parsed, forecast)
            if nws_prob is None:
                continue

            kalshi_prob = yes_ask / 100.0

            if nws_prob == kalshi_prob:
                continue

            edge = abs(nws_prob - kalshi_prob)
            if edge < min_edge:
                continue

            if nws_prob > kalshi_prob:
                side = "yes"
                ask_cents = yes_ask
            else:
                side = "no"
                ask_cents = no_ask

            if ask_cents == 0:
                continue

            opportunities.append(
                {
                    "market": market,
                    "side": side,
                    "ask_cents": ask_cents,
                    "kalshi_prob": kalshi_prob,
                    "nws_prob": nws_prob,
                    "edge": edge,
                }
            )

        except Exception as exc:
            print(f"[weather] error processing {market.id}: {exc}")
            continue
    return opportunities
