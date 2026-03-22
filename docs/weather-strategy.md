# Weather Market Strategy

This document describes the weather trading strategy in depth: the NWS API flow, how market titles are parsed, how probabilities are derived from forecasts, city coverage, configuration, and known limitations.

---

## Table of Contents

- [Overview](#overview)
- [NWS API Flow](#nws-api-flow)
- [Market Title Parser](#market-title-parser)
- [Probability Derivation](#probability-derivation)
- [Edge Calculation and Entry](#edge-calculation-and-entry)
- [City Coverage](#city-coverage)
- [Configuration](#configuration)
- [Running the Strategy](#running-the-strategy)
- [Known Limitations](#known-limitations)
- [Key Files](#key-files)

---

## Overview

Kalshi offers daily weather markets for major US cities. Example titles:

- "Will the high temperature in Chicago exceed 85°F today?"
- "Will it rain in Miami today?"
- "Will the low temperature in Seattle fall below 40°F?"

Each market has a YES ask and NO ask in cents (1–99). The YES ask implies a probability: a YES ask of 70¢ means the market is pricing a 70% chance the condition is met.

The National Weather Service publishes free hourly forecasts for any US coordinate via `api.weather.gov`. These forecasts are generally more accurate than a naive market price, especially for markets that closed many hours ago when prices were set.

When the NWS-implied probability diverges from the Kalshi price by more than a configurable edge threshold, the strategy enters a position on the favored side.

---

## NWS API Flow

The NWS hourly forecast requires two HTTP calls (no API key needed):

### Step 1: Resolve coordinates to a grid point

```
GET https://api.weather.gov/points/{lat},{lon}
```

Response includes `properties.forecastHourly` — the URL for the hourly forecast. This grid point mapping is stable for a given coordinate and is implicitly cached by the forecast cache.

### Step 2: Fetch hourly forecast

```
GET {properties.forecastHourly}
```

Response is a list of forecast periods, each containing:

| Field | Description |
|---|---|
| `startTime` | ISO 8601 datetime for the start of the hour |
| `temperature` | Temperature in °F |
| `probabilityOfPrecipitation.value` | Precipitation probability 0–100 (may be null) |
| `windSpeed` | Wind speed string, e.g. `"12 mph"` |
| `shortForecast` | Human-readable summary, e.g. `"Partly Cloudy"` |

The fetcher normalizes each period to:

```python
{
    "time": datetime,
    "temp_f": float,
    "precip_pct": int,       # 0 if null
    "wind_mph": float,
    "short_forecast": str,
}
```

### Caching

Forecasts are cached for **10 minutes**, keyed by coordinates rounded to 2 decimal places. This prevents hammering the NWS API when scanning many markets for the same city. The cache is in-process only — it resets on restart.

```
User-Agent: BettingApp/1.0 (weather-strategy)
```

The NWS API requires a `User-Agent` header. The app sets a descriptive value as recommended by the NWS documentation.

---

## Market Title Parser

`src/weather/market_parser.py` extracts structured fields from Kalshi weather market titles using regular expressions.

### Fields extracted

| Field | Example value | Description |
|---|---|---|
| `city` | `"Chicago"` | Canonical city name |
| `lat`, `lon` | `41.8781`, `-87.6298` | Coordinates for NWS lookup |
| `date` | `datetime.date(2026, 3, 22)` | Target date (defaults to today) |
| `metric` | `"high_temp"` | One of `high_temp`, `low_temp`, `precip`, `wind` |
| `threshold` | `85.0` | Numeric threshold from the title |
| `direction` | `"above"` | `"above"` or `"below"` |

### City matching

City names are matched against a combined list of canonical names and aliases (e.g. `"NYC"` → `"New York"`, `"DC"` → `"Washington DC"`). The list is sorted longest-first so multi-word names match before their substrings.

If the market title contains no recognized city name, `parse_weather_market()` returns `None` and the market is skipped.

### Supported patterns

The parser recognizes titles in these formats (case-insensitive):

- `"Will the high temperature in {City} exceed {N}°F today?"`
- `"Will the low temperature in {City} fall below {N}°F today?"`
- `"Will it rain in {City} today?"` / `"Will there be precipitation in {City} today?"`
- `"Will wind speeds in {City} exceed {N} mph today?"`

Titles that do not match are silently skipped.

---

## Probability Derivation

Once a market is parsed and a forecast is fetched, `get_nws_probability()` converts the forecast to a 0–1 probability.

### Precipitation markets

```
avg_precip = mean(period["precip_pct"] for all hours on target date)
prob = avg_precip / 100.0
```

If direction is `"below"` (e.g. "Will there be no precipitation?"), `prob = 1.0 - avg_precip / 100.0`.

### Temperature markets (high and low)

```
observed = max(temp_f)   # for high_temp
observed = min(temp_f)   # for low_temp
```

Then apply the three-tier heuristic against the market threshold:

| Scenario (direction = "above") | Returned probability |
|---|---|
| `observed - threshold > 3` | 0.85 |
| `abs(observed - threshold) <= 3` | 0.50 |
| `observed - threshold < -3` | 0.15 |

For direction `"below"`, the sign is flipped.

### Wind markets

Same three-tier heuristic as temperature, using `max(wind_mph)` as the observed value.

---

## Edge Calculation and Entry

```
edge = abs(nws_prob - kalshi_prob)
kalshi_prob = yes_ask_cents / 100.0
```

If `nws_prob > kalshi_prob`: buy **YES** (market underpricing the event).
If `nws_prob < kalshi_prob`: buy **NO** (market overpricing the event).

Entry is skipped if the chosen side has a zero ask (`ask_cents == 0`).

The minimum edge threshold is `WEATHER_MIN_EDGE` (default: 5%). This is configurable via env var or `--min-edge` CLI flag.

---

## City Coverage

The following ~27 US cities are supported. Markets for any other city are skipped.

| City | Coordinates |
|---|---|
| New York | 40.7128, -74.0060 |
| Los Angeles | 34.0522, -118.2437 |
| Chicago | 41.8781, -87.6298 |
| Houston | 29.7604, -95.3698 |
| Phoenix | 33.4484, -112.0740 |
| Philadelphia | 39.9526, -75.1652 |
| San Antonio | 29.4241, -98.4936 |
| San Diego | 32.7157, -117.1611 |
| Dallas | 32.7767, -96.7970 |
| San Jose | 37.3382, -121.8863 |
| Austin | 30.2672, -97.7431 |
| Jacksonville | 30.3322, -81.6557 |
| Fort Worth | 32.7555, -97.3308 |
| Columbus | 39.9612, -82.9988 |
| Charlotte | 35.2271, -80.8431 |
| Indianapolis | 39.7684, -86.1581 |
| San Francisco | 37.7749, -122.4194 |
| Seattle | 47.6062, -122.3321 |
| Denver | 39.7392, -104.9903 |
| Nashville | 36.1627, -86.7816 |
| Oklahoma City | 35.4676, -97.5164 |
| El Paso | 31.7619, -106.4850 |
| Washington DC | 38.9072, -77.0369 |
| Boston | 42.3601, -71.0589 |
| Miami | 25.7617, -80.1918 |
| Atlanta | 33.7490, -84.3880 |
| Minneapolis | 44.9778, -93.2650 |
| Portland | 45.5152, -122.6784 |

Aliases supported: `NYC`, `New York City`, `LA`, `DC`, `D.C.`, `Washington D.C.`, `Philly`, `SF`, `MPLS`, `OKC`, `Indy`, `Jax`.

---

## Configuration

| Env var | Default | Description |
|---|---|---|
| `WEATHER_MIN_EDGE` | `0.05` | Minimum probability edge (5%) to enter a trade |
| `WEATHER_INTERVAL` | `300` | Seconds between scans in continuous mode |

---

## Running the Strategy

```bash
# One-shot scan — print opportunities, no trades
python -m src.cli weather scan
python -m src.cli weather scan --min-edge 0.10   # stricter threshold

# Continuous paper trade loop (default)
python -m src.cli weather run --simulate --bankroll 10.00

# Continuous live trading
python -m src.cli weather run --live --bankroll 50.00 --interval 120

# Resume a previous session
python -m src.cli weather run --simulate --resume 3
```

---

## Known Limitations

**Probability heuristic is coarse.** The three-tier (0.85 / 0.50 / 0.15) mapping for temperature and wind is a rough approximation. It does not account for:
- Forecast uncertainty bands
- Historical station bias
- Intra-day temperature variance (max/min can shift by several degrees)
- Microclimate differences between the NWS grid point and the actual measurement station Kalshi uses for settlement

**City coordinates are approximate.** The lat/lon in `CITY_COORDS` points to city centers, not specific airport or ASOS weather stations. The NWS resolves these to the nearest forecast office grid, which may not match Kalshi's settlement station exactly.

**Title parsing is fragile.** Kalshi occasionally changes the phrasing or format of market titles. Any title that does not match the regex patterns is silently skipped. Check the `[weather] skip` log lines to see how many markets are being excluded.

**No forecast uncertainty model.** When the forecast says 84°F and the market threshold is 85°F, the three-tier heuristic returns 0.50 — but the true probability depends on forecast uncertainty, which can be ±3–5°F in typical NWS hourly forecasts.

**Settlement basis risk.** NWS forecasts use grid-point interpolation across many stations. Kalshi may settle temperature markets using a single ASOS station reading. These can differ meaningfully, especially in cities with complex topography or multiple nearby stations.
