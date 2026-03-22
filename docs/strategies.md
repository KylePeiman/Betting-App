# Strategies

This document covers the three active trading strategies in depth: how each works, what data it uses, entry conditions, position sizing, and settlement.

---

## Table of Contents

- [Last-Second Convergence](#last-second-convergence)
- [Arbitrage (Binary + Series)](#arbitrage-binary--series)
- [Weather Market Strategy](#weather-market-strategy)
- [Dropped Strategies](#dropped-strategies)

---

## Last-Second Convergence

### How it works

Kalshi hourly crypto series divide each asset's price into a set of mutually exclusive, collectively exhaustive price-range buckets (e.g. BTC $84,000–$85,000, $85,000–$86,000, etc.). Each bucket is a binary YES/NO market. The winning bucket is determined by the **CF Benchmarks Real-Time Index (RTI)**: a 60-second equally-weighted average of qualifying trades in the final minute before close.

If spot price has been stable and well inside a single bucket for the last 15+ seconds, the 60-second average will almost certainly land in that same bucket. The market may still be pricing YES at 70–92¢ rather than 99¢ because other traders are not yet confident or haven't acted — giving a small but reliable edge.

The edge disappears quickly as the market adjusts. Entry must happen within the first seconds after the window opens.

### Data sources

- **Kraken WebSocket** (`wss://ws.kraken.com/v2`): real-time ask prices for BTC, ETH, SOL, XRP, DOGE. No authentication required. Updates `PriceCache` on every tick.
- **Kalshi WebSocket** (`wss://api.elections.kalshi.com/trade-api/ws/v2`): RSA-PSS authenticated, same key as REST. Subscribes to `orderbook_delta` for near-term market tickers. Provides live YES ask prices that override stale REST values before entry.

Both feeds run on background threads. The main loop blocks on a `threading.Event` that fires whenever either feed writes a new price. Only markets relevant to the changed pair/ticker are checked on each wake.

### Asset-to-pair mapping

| Kalshi series prefix | Kraken pair |
|---|---|
| `KXBTC` | `XBTUSD` |
| `KXETH` | `ETHUSD` |
| `KXSOL` | `SOLUSD` |
| `KXXRP` | `XRPUSD` |
| `KXDOGE` | `DOGEUSD` |

### Entry conditions

All conditions must be satisfied simultaneously on a given price tick:

1. **Entry window**: Market closes within `--ls-entry-window` seconds (default: 120).
2. **Bucket match**: Kraken spot price falls within a specific bucket's `floor_strike`–`cap_strike` range.
3. **Edge buffer**: Spot is at least `--ls-edge-buffer` (default: 15%) of bucket width from both edges. Avoids settlement risk when spot is near a boundary.
4. **Stability**: Spot has moved less than `--ls-stability-threshold` (default: 0.3%) over the last `--ls-stability-window` (default: 15) seconds.
5. **YES trade price**: YES ask is between `--ls-min-yes` (default: 70¢) and `--ls-max-yes` (default: 92¢). The 92¢ cap avoids positions where the edge is too thin after fees.
6. **NO trade price**: For buckets where spot is clearly outside, NO ask is between `--ls-min-no` (default: 3¢) and `--ls-max-no` (default: 40¢).

If a fresh YES ask is available from the Kalshi WebSocket (less than 10 seconds old), it overrides the stale REST value before the price range check.

### Position sizing

Sizing is capped at `--max-position` (default: 10%) of current liquid bankroll. Minimum is 1 contract (1¢). A single position per market ticker is enforced — no doubling up if the window stays open.

### Settlement

After each tick's entry check, the engine polls `GET /markets/{ticker}` for all open positions. A position settles only when:
- Market `status` is `finalized`, `settled`, or `closed`
- `result` is `"yes"` or `"no"` — an empty string is treated as unresolved

On a YES win: bankroll increases by `(100 - entry_price_cents) * contracts / 100`.
On a YES loss: the staked amount is already deducted; no further change.

### Key implementation files

- `src/engine/last_second.py` — `PriceTracker`, `find_matching_bucket()`, `scan_last_second_opportunities()`
- `src/engine/live_sim.py` — main loop calling the scanner and settling positions
- `src/streaming/manager.py` — `StreamManager` wiring both WebSocket feeds to `PriceCache`
- `src/streaming/price_cache.py` — thread-safe `PriceCache` with `update_event`

---

## Arbitrage (Binary + Series)

### How it works

Kalshi prices occasionally allow risk-free (or near risk-free) profit by simultaneously buying contracts across a market or series.

**Binary arb**: A single Kalshi market where `yes_ask + no_ask < 100¢`. Buying 1 YES and 1 NO contract costs less than 100¢ and always pays out exactly 100¢ regardless of outcome.

**Series arb**: A price-range series (e.g. all hourly BTC buckets) where the sum of YES asks across all legs is less than 100¢. Exactly one leg always resolves YES, so buying 1 YES on every leg pays out 100¢. This is risk-free only when the series is **collectively exhaustive** — every possible outcome is covered by a liquid leg.

### Exhaustiveness check (critical)

A series arb is marked `guaranteed=True` only when:

```
len(liquid_legs) == total_markets_in_event
```

Where `total_markets_in_event` is the raw market count from the API **before** any bid/ask filtering. If some buckets have no quotes (illiquid), those buckets are gaps — spot could settle in an uncovered bucket, causing a total loss on all positions. The live engine only auto-enters `guaranteed=True` series arbs.

### Entry and sizing

Arb positions are entered as limit orders on Kalshi. If any leg fails to fill within 2 seconds, all placed legs are cancelled and the position is skipped entirely. This prevents partial fills that would create unhedged directional exposure.

Sizing: each arb is entered with a fixed number of contracts based on available bankroll and the `--max-position` limit, same as last-second trades.

### Settlement

Settlement is polled in the same loop as last-second positions. A series arb position settles when any one leg resolves — the winning leg covers all costs plus profit, losing legs each expire worthless (already paid for at entry).

### CLI commands

```bash
# One-shot scan
python -m src.cli arb scan [--categories TEXT] [--min-profit FLOAT] [--type all|binary|series]

# Record arbs as simulated trades
python -m src.cli arb simulate

# Auto-settle resolved arb simulations
python -m src.cli arb settle

# List arb simulations
python -m src.cli arb list

# Aggregate P&L report
python -m src.cli arb report
```

### Key implementation files

- `src/engine/arbitrage.py` — `scan_binary_arb()`, `scan_series_arb()`, `opportunities_to_sim()`
- `src/engine/live_sim.py` — enforces `guaranteed=True` before entering series arbs

---

## Weather Market Strategy

### How it works

Kalshi lists daily weather markets for major US cities — questions like "Will the high temperature in Chicago exceed 85°F today?" or "Will it rain in Miami today?". These markets imply a probability via their yes_ask price.

The National Weather Service (NWS) publishes free hourly forecasts for any US lat/lon via `api.weather.gov`. The weather strategy:

1. Fetches all open Kalshi weather markets.
2. Parses each market title using regex to extract city, metric (high temp, low temp, precipitation, wind), threshold, and direction (above/below).
3. Resolves the city to coordinates using a hardcoded table of ~27 major US cities.
4. Fetches the NWS hourly forecast for those coordinates (cached 10 minutes).
5. Converts the forecast to a probability for the market condition.
6. Compares the NWS-implied probability to the Kalshi-implied price.
7. Enters a position if the edge exceeds the configured minimum (default: 5%).

### Probability derivation

For **precipitation** markets: the NWS provides hourly precipitation probability (0–100%). The strategy averages all hourly values for the target date and converts to a 0–1 probability.

For **temperature** and **wind** markets: the strategy compares the forecast max (for high temp / wind) or min (for low temp) against the market threshold using a three-tier heuristic:

| Forecast vs threshold | Probability returned |
|---|---|
| More than 3 units in the expected direction | 0.85 |
| Within 3 units of the threshold | 0.50 |
| More than 3 units in the opposite direction | 0.15 |

This is a rough approximation — see [Known Limitations](#known-limitations) below.

### Entry conditions

- Market closes today (markets closing on future dates are skipped)
- Market title parses to a recognized city, metric, and threshold
- NWS forecast data is available for the target date
- `abs(nws_prob - kalshi_prob) >= min_edge`

If `nws_prob > kalshi_prob`, buy YES. If `nws_prob < kalshi_prob`, buy NO.

### CLI commands

```bash
# One-shot scan
python -m src.cli weather scan [--min-edge FLOAT]

# Continuous loop
python -m src.cli weather run [--simulate|--live] [--bankroll FLOAT] [--interval INT] [--min-edge FLOAT] [--resume INT]
```

### Configuration

| Env var | Default | Description |
|---|---|---|
| `WEATHER_MIN_EDGE` | `0.05` | Minimum NWS vs Kalshi probability gap to enter a trade |
| `WEATHER_INTERVAL` | `300` | Seconds between scans in the continuous loop |

### Known limitations

- **Probability heuristic**: the three-tier temperature/wind probability (0.85 / 0.50 / 0.15) is a rough approximation, not a calibrated model. It does not account for forecast uncertainty, historical station bias, or intra-day variance.
- **City coverage**: only ~27 major US cities are supported. Markets for cities not in the table are silently skipped.
- **Title parsing**: Kalshi occasionally changes market title formats. If a title does not match the expected regex patterns, it is skipped with no error.
- **Settlement basis**: NWS forecasts and Kalshi settlement may use different weather stations or measurement methodologies, creating basis risk even when the forecast is accurate.

### Key implementation files

- `src/weather/noaa.py` — `get_forecast()`, NWS two-step API flow, 10-minute cache
- `src/weather/market_parser.py` — `parse_weather_market()`, `CITY_COORDS`, regex patterns
- `src/weather/scanner.py` — `scan_weather_markets()`, `get_nws_probability()`
- `src/weather/strategy.py` — `run_weather_strategy()`, continuous loop, settlement

---

## Dropped Strategies

### 15-minute directional crypto bets (Claude agent)

The legacy agent mode used Claude to evaluate Kalshi crypto markets and place directional YES/NO bets based on macro reasoning and news. This strategy was abandoned after consistently negative results — crypto prediction markets proved too efficient for the agent to find real edge. The code lives in `src/engine/agent_mode.py` and `src/engine/agent_advisor.py` but is not called from the active simulation loop.

### EV recommendation engine

The `run` / `simulate run` / `evaluate` command group was an earlier approach: compute EV across all Kalshi markets using Kelly criterion sizing and store recommendations for later settlement tracking. This was replaced by the arb-only and last-second strategies. The code is preserved for historical tracking and manual review.
