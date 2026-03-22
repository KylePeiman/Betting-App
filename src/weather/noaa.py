"""NOAA/NWS hourly weather forecast fetcher.

Fetches hourly forecasts from the National Weather Service API.
No API key required. Results are cached for 10 minutes.
"""

import re
import time
from datetime import datetime

import httpx

_BASE_URL = "https://api.weather.gov"
_HEADERS = {"User-Agent": "BettingApp/1.0 (weather-strategy)"}
_TIMEOUT = 10
_CACHE_TTL = 600  # 10 minutes

# Cache: (lat_rounded, lon_rounded) -> (timestamp, data)
_cache: dict[tuple, tuple[float, list]] = {}


def _round_coord(lat: float, lon: float) -> tuple[float, float]:
    """Round coordinates to 2 decimal places for cache keying.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        Tuple of (rounded_lat, rounded_lon).
    """
    return round(lat, 2), round(lon, 2)


def _parse_wind_speed(wind_str: str) -> float:
    """Extract numeric wind speed from a string like '10 mph'.

    Args:
        wind_str: Wind speed string from the NWS API.

    Returns:
        Wind speed as a float. Returns 0.0 if parsing fails.
    """
    match = re.search(r"\d+", wind_str)
    if match:
        return float(int(match.group()))
    return 0.0


def _parse_period(period: dict) -> dict:
    """Parse a single NWS forecast period into a standardized dict.

    Args:
        period: Raw period dict from the NWS API response.

    Returns:
        Dict with keys: time, temp_f, precip_pct, wind_mph,
        short_forecast.
    """
    precip_data = period.get("probabilityOfPrecipitation", {})
    precip_value = precip_data.get("value") if precip_data else None

    return {
        "time": datetime.fromisoformat(period["startTime"]),
        "temp_f": float(period["temperature"]),
        "precip_pct": int(precip_value) if precip_value is not None else 0,
        "wind_mph": _parse_wind_speed(period.get("windSpeed", "0 mph")),
        "short_forecast": period.get("shortForecast", ""),
    }


def get_forecast(lat: float, lon: float) -> list[dict]:
    """Fetch hourly weather forecast from NOAA NWS API for a given lat/lon.

    Uses a two-step API flow:
    1. Resolve the lat/lon to a forecast office grid point.
    2. Fetch the hourly forecast for that grid point.

    Results are cached for 10 minutes, keyed by coordinates rounded
    to 2 decimal places.

    Args:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.

    Returns:
        A list of hourly period dicts with keys:
            - time (datetime): Start time of hour (timezone-aware).
            - temp_f (float): Temperature in Fahrenheit.
            - precip_pct (int): Probability of precipitation 0-100.
            - wind_mph (float): Wind speed in mph.
            - short_forecast (str): e.g. "Partly Cloudy".
        Returns [] on any error (network, parse, etc.).
    """
    cache_key = _round_coord(lat, lon)

    # Check cache
    if cache_key in _cache:
        cached_time, cached_data = _cache[cache_key]
        if time.time() - cached_time < _CACHE_TTL:
            return cached_data

    try:
        with httpx.Client(
            headers=_HEADERS, timeout=_TIMEOUT
        ) as client:
            # Step 1: Resolve lat/lon to forecast grid point
            points_url = f"{_BASE_URL}/points/{lat},{lon}"
            points_resp = client.get(points_url)
            points_resp.raise_for_status()
            forecast_url = points_resp.json()["properties"][
                "forecastHourly"
            ]

            # Step 2: Fetch hourly forecast
            forecast_resp = client.get(forecast_url)
            forecast_resp.raise_for_status()
            periods = forecast_resp.json()["properties"]["periods"]

        result = [_parse_period(p) for p in periods]

        # Update cache
        _cache[cache_key] = (time.time(), result)

        return result

    except Exception:
        return []
