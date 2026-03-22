"""Kalshi weather market title parser.

Extracts structured data (city, date, metric, threshold, direction) from
human-readable Kalshi weather market titles using regex patterns.  No
external dependencies beyond the standard library.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# City coordinates — ~27 major US cities commonly seen on Kalshi
# ---------------------------------------------------------------------------

CITY_COORDS: Dict[str, Tuple[float, float]] = {
    "New York": (40.7128, -74.0060),
    "Los Angeles": (34.0522, -118.2437),
    "Chicago": (41.8781, -87.6298),
    "Houston": (29.7604, -95.3698),
    "Phoenix": (33.4484, -112.0740),
    "Philadelphia": (39.9526, -75.1652),
    "San Antonio": (29.4241, -98.4936),
    "San Diego": (32.7157, -117.1611),
    "Dallas": (32.7767, -96.7970),
    "San Jose": (37.3382, -121.8863),
    "Austin": (30.2672, -97.7431),
    "Jacksonville": (30.3322, -81.6557),
    "Fort Worth": (32.7555, -97.3308),
    "Columbus": (39.9612, -82.9988),
    "Charlotte": (35.2271, -80.8431),
    "Indianapolis": (39.7684, -86.1581),
    "San Francisco": (37.7749, -122.4194),
    "Seattle": (47.6062, -122.3321),
    "Denver": (39.7392, -104.9903),
    "Nashville": (36.1627, -86.7816),
    "Oklahoma City": (35.4676, -97.5164),
    "El Paso": (31.7619, -106.4850),
    "Washington DC": (38.9072, -77.0369),
    "Boston": (42.3601, -71.0589),
    "Miami": (25.7617, -80.1918),
    "Atlanta": (33.7490, -84.3880),
    "Minneapolis": (44.9778, -93.2650),
    "Portland": (45.5152, -122.6784),
}

# ---------------------------------------------------------------------------
# City aliases — map common abbreviations to canonical city names
# ---------------------------------------------------------------------------

_CITY_ALIASES: Dict[str, str] = {
    "NYC": "New York",
    "New York City": "New York",
    "LA": "Los Angeles",
    "DC": "Washington DC",
    "D.C.": "Washington DC",
    "Washington D.C.": "Washington DC",
    "Washington, D.C.": "Washington DC",
    "Philly": "Philadelphia",
    "SF": "San Francisco",
    "MPLS": "Minneapolis",
    "OKC": "Oklahoma City",
    "Indy": "Indianapolis",
    "Jax": "Jacksonville",
}

# Build a combined lookup: aliases + canonical names, sorted longest-first
# so that "New York City" matches before "New York", "San Francisco" before
# "San", etc.
_ALL_CITY_NAMES: list[tuple[str, str]] = []

for alias, canonical in _CITY_ALIASES.items():
    _ALL_CITY_NAMES.append((alias, canonical))
for city in CITY_COORDS:
    _ALL_CITY_NAMES.append((city, city))

_ALL_CITY_NAMES.sort(key=lambda pair: len(pair[0]), reverse=True)

# Pre-compile a single regex that matches any city name / alias.
_city_pattern = re.compile(
    "|".join(re.escape(name) for name, _ in _ALL_CITY_NAMES),
    re.IGNORECASE,
)

# Reverse lookup from lowered surface form to canonical city name.
_CITY_LOOKUP: Dict[str, str] = {
    name.lower(): canonical for name, canonical in _ALL_CITY_NAMES
}

# ---------------------------------------------------------------------------
# Date patterns
# ---------------------------------------------------------------------------

_MONTH_NAMES: Dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_month_group = "|".join(_MONTH_NAMES.keys())

# "March 22, 2026" or "March 22 2026" or "March 22"
_DATE_PATTERN = re.compile(
    rf"\b({_month_group})\s+(\d{{1,2}})(?:\s*,?\s*(\d{{4}}))?\b",
    re.IGNORECASE,
)

_TODAY_PATTERN = re.compile(r"\btoday\b", re.IGNORECASE)
_TOMORROW_PATTERN = re.compile(r"\btomorrow\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Metric patterns
# ---------------------------------------------------------------------------

_METRIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bhigh\s+temp(?:erature)?\b", re.IGNORECASE), "high_temp"),
    (re.compile(r"\blow\s+temp(?:erature)?\b", re.IGNORECASE), "low_temp"),
    (
        re.compile(
            r"\b(?:precipitation|rainfall|rain|snow)\b", re.IGNORECASE
        ),
        "precip",
    ),
    (re.compile(r"\bwinds?\b(?:\s+speed)?", re.IGNORECASE), "wind"),
]

# ---------------------------------------------------------------------------
# Direction patterns
# ---------------------------------------------------------------------------

_ABOVE_PATTERN = re.compile(
    r"\b(?:exceed|exceeds|above|more\s+than|over|at\s+least)\b",
    re.IGNORECASE,
)
_BELOW_PATTERN = re.compile(
    r"\b(?:below|under|less\s+than|at\s+most)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Threshold patterns — e.g. "75\u00b0F", "75 degrees", "0.5 inches", "25 mph"
# ---------------------------------------------------------------------------

_THRESHOLD_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:\u00b0\s*F|degrees|inches|inch|mph|in\.?)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _match_city(text: str) -> Optional[Tuple[str, float, float]]:
    """Find the first (longest) matching city name in *text*.

    Returns:
        A tuple of (canonical_city_name, lat, lon) or ``None``.
    """
    match = _city_pattern.search(text)
    if match is None:
        return None
    canonical = _CITY_LOOKUP[match.group(0).lower()]
    lat, lon = CITY_COORDS[canonical]
    return canonical, lat, lon


def _match_date(text: str) -> Optional[datetime.date]:
    """Extract a date from *text*.

    Handles "March 22", "Mar 22, 2026", "today", and "tomorrow".

    Returns:
        A ``datetime.date`` or ``None``.
    """
    if _TODAY_PATTERN.search(text):
        return datetime.date.today()
    if _TOMORROW_PATTERN.search(text):
        return datetime.date.today() + datetime.timedelta(days=1)

    match = _DATE_PATTERN.search(text)
    if match is None:
        return None

    month_str, day_str, year_str = match.groups()
    month = _MONTH_NAMES[month_str.lower()]
    day = int(day_str)
    year = int(year_str) if year_str else datetime.date.today().year

    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _match_metric(text: str) -> Optional[str]:
    """Identify the weather metric mentioned in *text*.

    Returns:
        One of ``"high_temp"``, ``"low_temp"``, ``"precip"``, ``"wind"``,
        or ``None``.
    """
    for pattern, metric in _METRIC_PATTERNS:
        if pattern.search(text):
            return metric
    return None


def _match_direction(text: str) -> Optional[str]:
    """Determine whether the market asks about above or below a threshold.

    Returns:
        ``"above"``, ``"below"``, or ``None``.
    """
    above = _ABOVE_PATTERN.search(text)
    below = _BELOW_PATTERN.search(text)

    if above and not below:
        return "above"
    if below and not above:
        return "below"
    if above and below:
        # When both appear, use whichever comes last (closer to the
        # threshold value and therefore more likely to be the operative
        # word).
        return "above" if above.start() > below.start() else "below"
    return None


def _match_threshold(text: str) -> Optional[float]:
    """Extract the numeric threshold from *text*.

    Recognises patterns like ``75\u00b0F``, ``75 degrees``, ``0.5 inches``,
    and ``25 mph``.

    Returns:
        The threshold as a float, or ``None``.
    """
    match = _THRESHOLD_PATTERN.search(text)
    if match is None:
        return None
    return float(match.group(1))


def parse_weather_market(market: Any) -> Optional[Dict[str, Any]]:
    """Parse a Kalshi weather market into structured fields.

    Args:
        market: An object with ``.name`` (str) and ``.market_id`` (str)
            attributes.  ``.name`` is the human-readable title, e.g.
            *"Will the high temperature in New York City exceed
            75\u00b0F on March 22?"*.

    Returns:
        A dict with keys ``city``, ``lat``, ``lon``, ``date``, ``metric``,
        ``threshold``, and ``direction``; or ``None`` if any required
        field cannot be determined.
    """
    title: str = getattr(market, "name", "")
    if not title:
        return None

    city_result = _match_city(title)
    if city_result is None:
        return None
    city, lat, lon = city_result

    date = _match_date(title)
    if date is None:
        return None

    metric = _match_metric(title)
    if metric is None:
        return None

    direction = _match_direction(title)
    if direction is None:
        return None

    threshold = _match_threshold(title)
    if threshold is None:
        return None

    return {
        "city": city,
        "lat": lat,
        "lon": lon,
        "date": date,
        "metric": metric,
        "threshold": threshold,
        "direction": direction,
    }
