# Betting App

> A Python CLI application that trades Kalshi prediction markets using real-time price data, arbitrage detection, and weather forecast edge. Supports paper trading and live order placement.

## Table of Contents

- [Overview](#overview)
- [Strategies](#strategies)
- [Getting Started](#getting-started)
- [Configuration](#configuration)
- [CLI Reference](#cli-reference)
- [Architecture](#architecture)
- [Development](#development)

---

## Overview

Betting App scans Kalshi prediction markets continuously and executes trades when a quantifiable edge is found. Three active strategies are supported: last-second price convergence on hourly crypto bucket markets, micro-arbitrage within Kalshi (binary and series), and weather forecast edge using NOAA/NWS data versus Kalshi-implied probabilities. All strategies work in paper-trade (simulation) mode or with real Kalshi orders. Sessions, positions, and P&L are persisted in a local SQLite database and can be inspected via a Streamlit dashboard.

---

## Strategies

### Last-Second Convergence

The primary strategy. Kalshi hourly crypto series divide each asset's price into mutually exclusive buckets (e.g. BTC $84,000–$85,000). Each bucket resolves based on the CF Benchmarks 60-second equally-weighted average in the final minute before close. If the Kraken spot price has been stable and well inside a bucket for the preceding 15+ seconds, the 60-second settlement average will almost certainly land in the same bucket — yet the YES ask may still be 70–92¢, providing a measurable edge.

Entry conditions checked on every WebSocket tick:
- Spot price has moved less than 0.3% in the last 15 seconds (stability check)
- Spot is at least 15% of bucket width from both bucket edges (edge buffer)
- YES ask is between 70¢ and 92¢ for YES trades, or NO ask is between 3¢ and 40¢ for NO trades
- Market closes within the configured entry window (default: 120 seconds)

Real-time data comes from two WebSocket feeds: Kraken (public, no auth) for spot prices and Kalshi (RSA-PSS authenticated) for live orderbook. The scanner wakes on every price tick, checking only markets relevant to what changed, then goes back to waiting.

See [docs/strategies.md](docs/strategies.md) for full entry logic, sizing, and settlement details.

### Arbitrage (Binary + Series)

Scans for riskless arbitrage across Kalshi markets:

- **Binary arb**: A single market where `yes_ask + no_ask < 100`. Buying both YES and NO guarantees a payout of 100¢ regardless of outcome.
- **Series arb**: A price-range series (e.g. all BTC hourly buckets) where `sum(yes_asks) < 100`. Risk-free only when the series is collectively exhaustive — meaning all possible outcomes are covered by liquid legs.

The exhaustiveness check is critical: `guaranteed=True` only when the number of liquid legs equals the total market count in the event before any bid/ask filtering. Partial-coverage series are flagged as unguaranteed and are not entered automatically.

See [docs/strategies.md](docs/strategies.md) for the exhaustiveness logic and sizing rules.

### Weather Market Strategy

Compares NOAA/NWS hourly forecast probabilities against Kalshi-implied prices for weather markets (temperature highs/lows, precipitation, wind). When the NWS-derived probability diverges from the Kalshi price by more than a configurable minimum edge (default: 5%), a position is entered on the favored side.

The NWS API requires no authentication. Forecasts are cached for 10 minutes per coordinate. Cities are matched from Kalshi market titles via regex; ~27 major US cities are supported.

See [docs/weather-strategy.md](docs/weather-strategy.md) for the full NWS flow, probability derivation, and known limitations.

---

## Getting Started

```bash
# 1. Clone the repository
git clone <repo-url>
cd Betting-App

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env — at minimum set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH

# 4. Run a paper-trade session (last-second strategy, streaming on)
python -m src.cli --simulate
```

Get Kalshi API credentials from [kalshi.com/profile/api-keys](https://kalshi.com/profile/api-keys). Download the PEM private key file and set `KALSHI_PRIVATE_KEY_PATH` to its absolute path.

---

## Configuration

All settings are loaded from `.env` via `python-dotenv`. Copy `.env.example` to `.env` and fill in the values below.

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | *(required)* | API key ID from your Kalshi profile |
| `KALSHI_PRIVATE_KEY_PATH` | *(required)* | Absolute path to the downloaded PEM private key |
| `KALSHI_CATEGORIES` | *(all)* | Comma-separated category filter, e.g. `Crypto,Economics`. Leave blank to fetch all. |
| `DATABASE_URL` | `sqlite:///betting_app.db` | SQLAlchemy database URL. Swap to Postgres via `postgresql://...` |
| `ANTHROPIC_API_KEY` | *(optional)* | Required only for `--prediction` mode (Claude headline trades) |
| `NEWS_API_KEY` | *(optional)* | Required only for `--prediction` mode (NewsAPI headlines) |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model used by agent and prediction modes |
| `MIN_EV_THRESHOLD` | `0.005` | Minimum expected value threshold for the legacy recommendation engine |
| `DEFAULT_SOURCES` | `kalshi` | Default data sources for the legacy `run` command |
| `WEATHER_MIN_EDGE` | `0.05` | Minimum NWS-vs-Kalshi probability gap to enter a weather trade (5%) |
| `WEATHER_INTERVAL` | `300` | Seconds between weather market scans (5 minutes) |
| `GH_GIST_TOKEN` | *(optional)* | GitHub token for writing live session data to a Gist (dashboard) |
| `GH_GIST_ID` | *(preset)* | GitHub Gist ID for the live dashboard data feed |
| `POLYMARKET_API_KEY` | *(optional)* | Polymarket API key (read-only market data works without it) |
| `POLYMARKET_PRIVATE_KEY` | *(optional)* | Ethereum private key for live Polymarket order placement |
| `POLYMARKET_CHAIN_ID` | `137` | `137` = Polygon mainnet, `80002` = Amoy testnet |

---

## CLI Reference

### Root command — `python -m src.cli`

Quickest way to start a session. Uses sensible defaults for all strategy parameters.

```bash
python -m src.cli --simulate            # paper trade, last-second on, streaming on
python -m src.cli --live                # real orders, auto-detects Kalshi balance
python -m src.cli --simulate --prediction  # paper trade + Claude headline trades
```

| Flag | Default | Description |
|---|---|---|
| `--simulate` | — | Paper trade — no real orders placed |
| `--live` | — | Place real orders on Kalshi |
| `--bankroll FLOAT` | `5.00` (sim) / account balance (live) | Starting bankroll in USD |
| `--last-second` / `--no-last-second` | on | Enable/disable last-second convergence strategy |
| `--streaming` / `--no-streaming` | on | Use WebSocket feeds; `--no-streaming` falls back to REST polling |
| `--prediction` | off | Enable Claude + NewsAPI headline prediction trades |

---

### `live` — full options

```bash
python -m src.cli live --simulate [OPTIONS]
python -m src.cli live --live [OPTIONS]
```

Identical to the root command but exposes every tunable parameter.

| Flag | Default | Description |
|---|---|---|
| `--simulate` / `--live` | *(required)* | Paper trade or real orders |
| `--bankroll FLOAT` | `5.00` / account balance | Starting bankroll in USD |
| `--interval INT` | `15` | Seconds between full REST market list scans |
| `--settle-interval INT` | `5` | Seconds between settlement polls while idle |
| `--categories TEXT` | `Crypto,Economics,Financials` | Comma-separated Kalshi categories to scan |
| `--near-term INT` | `60` | Only consider markets closing within this many minutes |
| `--max-position FLOAT` | `0.10` | Max fraction of bankroll per single position |
| `--logs-dir TEXT` | `logs` | Directory for session log files |
| `--resume INT` | — | Resume an existing session by ID |
| `--last-second` / `--no-last-second` | on | Enable/disable last-second strategy |
| `--streaming` / `--no-streaming` | on | Enable/disable WebSocket streaming |
| `--prediction` | off | Enable headline prediction trades |
| `--ls-entry-window INT` | `120` | Seconds before close to begin monitoring for entries |
| `--ls-min-yes INT` | `70` | Minimum YES ask in cents to enter a YES trade |
| `--ls-max-yes INT` | `92` | Maximum YES ask in cents to enter a YES trade |
| `--ls-min-no INT` | `3` | Minimum NO ask in cents to enter a NO trade |
| `--ls-max-no INT` | `40` | Maximum NO ask in cents to enter a NO trade |
| `--ls-edge-buffer FLOAT` | `0.15` | Fraction of bucket width spot must be from both edges |
| `--ls-stability-window INT` | `15` | Seconds of price history used for stability check |
| `--ls-stability-threshold FLOAT` | `0.003` | Max allowed price movement in stability window (0.3%) |
| `--ls-directional-margin FLOAT` | `0.003` | Min pct spot must be above/below floor_strike for directional entries |

**Examples:**

```bash
# Narrow the entry window to the final 60 seconds
python -m src.cli live --simulate --ls-entry-window 60

# Widen near-term scan to 2 hours, larger bankroll
python -m src.cli live --simulate --near-term 120 --bankroll 20.00

# Disable WebSocket streaming (REST polling fallback)
python -m src.cli live --simulate --no-streaming

# Resume a previous session by ID
python -m src.cli live --live --resume 4

# Real orders with explicit bankroll
python -m src.cli live --live --bankroll 25.00
```

---

### `simulate` — paper-trade management

```bash
# List all live simulation sessions with P&L
python -m src.cli simulate sessions [--limit 10]

# List legacy simulated bets (EV paper trades)
python -m src.cli simulate list [--status open|settled|expired] [--limit 30]

# Create legacy EV paper-trade bets
python -m src.cli simulate run [--min-ev FLOAT] [--categories TEXT] [--quiet]

# Auto-settle resolved legacy paper-trade bets
python -m src.cli simulate settle [--quiet]

# Show aggregate performance report for legacy paper trades
python -m src.cli simulate report
```

---

### `arb` — arbitrage scanning

```bash
# One-shot scan, print results — no trades placed
python -m src.cli arb scan [--categories TEXT] [--min-profit FLOAT] [--type all|binary|series]

# Record current arb opportunities as simulated trades
python -m src.cli arb simulate [--categories TEXT] [--min-profit FLOAT] [--type all|binary|series]

# Auto-settle resolved arb simulations
python -m src.cli arb settle [--quiet]

# List recorded arb simulations
python -m src.cli arb list [--status open|won|lost|voided] [--limit 30]

# Aggregate P&L report for all arb simulations
python -m src.cli arb report
```

---

### `weather` — NWS vs Kalshi weather strategy

```bash
# One-shot scan: print weather edge opportunities
python -m src.cli weather scan [--min-edge FLOAT]

# Continuous trading loop (paper trade by default)
python -m src.cli weather run [--simulate|--live] [--bankroll FLOAT] [--interval INT] [--min-edge FLOAT] [--resume INT]
```

---

### `cross-arb` — Kalshi vs Polymarket cross-platform arbitrage

```bash
# Scan both platforms and print cross-arb opportunities
python -m src.cli cross-arb scan [--categories TEXT] [--min-profit FLOAT] [--min-match FLOAT] [--show-unmatched]
```

---

### `recommendations` — legacy recommendation engine

```bash
# Generate recommendations (compute or agent mode)
python -m src.cli run [--mode compute|agent] [--period week|month] [--categories TEXT] [--min-ev FLOAT] [--quiet]

# List stored recommendations
python -m src.cli recommendations list [--limit 20] [--status pending|settled] [--mode agent|compute]

# Show full details of a recommendation
python -m src.cli recommendations show <ID>

# Manually settle a recommendation
python -m src.cli recommendations settle <ID> --result win|loss|void [--actual-odds FLOAT]

# Evaluate historical performance
python -m src.cli evaluate [--from YYYY-MM-DD] [--to YYYY-MM-DD]
```

---

### Dashboard

```bash
# Start the Streamlit dashboard locally
streamlit run dashboard.py

# Make accessible on the local network
streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
```

Then open `http://<machine-ip>:8501` on any device on the same network.

---

### Utility scripts

```bash
# Wipe all sessions, positions, and records (prompts for confirmation)
python scripts/clear_db.py
```

---

## Architecture

```
src/
├── cli.py                       # Click CLI entry point — all command groups
├── fetchers/
│   ├── base.py                  # BaseFetcher ABC, Market/Selection dataclasses
│   ├── kalshi.py                # Kalshi REST API — RSA-PSS auth, market fetch, order placement
│   ├── polymarket.py            # Polymarket read + order placement (Polygon)
│   ├── crypto_prices.py         # Kraken REST price fetch (streaming fallback)
│   └── news.py                  # NewsAPI client for headline prediction trades
├── engine/
│   ├── live_sim.py              # Main simulation loop — entry, settlement, bankroll tracking
│   ├── last_second.py           # Last-second strategy: PriceTracker, bucket matching, scanner
│   ├── arbitrage.py             # Binary + series arb detection, exhaustiveness check
│   ├── cross_arb.py             # Cross-platform arb: Kalshi vs Polymarket market matching
│   ├── prediction.py            # Headline signal detection + Claude review
│   ├── pipeline.py              # Legacy recommendation engine entry point
│   ├── compute_mode.py          # Legacy EV + Kelly sizing
│   └── agent_mode.py            # Legacy Claude multi-turn tool loop (unused)
├── streaming/
│   ├── price_cache.py           # Thread-safe cache: spot prices + yes_ask, update_event
│   ├── kraken_ws.py             # Kraken WebSocket client (public, no auth)
│   ├── kalshi_ws.py             # Kalshi WebSocket client (RSA-PSS authenticated)
│   └── manager.py               # StreamManager — starts/stops both feeds, manages subscriptions
├── weather/
│   ├── noaa.py                  # NOAA/NWS hourly forecast fetcher (no API key required)
│   ├── market_parser.py         # Regex parser: Kalshi weather title → city, metric, threshold
│   ├── scanner.py               # Edge scanner: NWS prob vs Kalshi price
│   └── strategy.py              # Continuous weather strategy loop — enter, settle, bankroll
├── storage/
│   ├── models.py                # ORM: SimSession, SimPosition, ArbSimulation, Recommendation, etc.
│   └── db.py                    # SQLAlchemy session factory + auto-migration for new columns
└── evaluator/
    └── performance.py           # Historical recommendation performance evaluation
config/
└── settings.py                  # All env vars loaded via python-dotenv
scripts/
└── clear_db.py                  # Wipe all DB records (with confirmation prompt)
dashboard.py                     # Streamlit dashboard — sessions, positions, trade history
```

### Key files

| File | Purpose |
|---|---|
| `src/cli.py` | All CLI commands. Entry point for every user-facing action. |
| `src/engine/live_sim.py` | The main loop. Drives fetching, last-second scanning, settlement, and bankroll accounting. |
| `src/engine/last_second.py` | `PriceTracker`, `find_matching_bucket()`, and the per-tick entry decision logic. |
| `src/engine/arbitrage.py` | `scan_binary_arb()`, `scan_series_arb()`, exhaustiveness check, sim recording. |
| `src/fetchers/kalshi.py` | Every Kalshi API call: market list, event fetch, order placement, balance, market status. |
| `src/streaming/manager.py` | Manages the Kraken + Kalshi WebSocket threads and the shared `PriceCache`. |
| `src/weather/scanner.py` | `scan_weather_markets()` — end-to-end NWS edge detection pipeline. |
| `src/storage/models.py` | All ORM models: `SimSession`, `SimPosition`, `ArbSimulation`, `Recommendation`, etc. |
| `config/settings.py` | Single source of truth for all env vars and their defaults. |

### Sub-documents

- [docs/strategies.md](docs/strategies.md) — detailed write-up of each strategy: entry conditions, sizing, settlement
- [docs/kalshi-auth.md](docs/kalshi-auth.md) — RSA-PSS auth, sign string format, header names, common errors
- [docs/weather-strategy.md](docs/weather-strategy.md) — NWS API flow, probability derivation, city coverage, limitations

---

## Development

### Running tests

```bash
python -m pytest tests/
```

Specific test files:
```bash
python -m pytest tests/test_last_second.py    # last-second strategy unit tests
python -m pytest tests/test_weather.py        # weather parser and scanner tests
```

### Adding a new strategy

1. Create `src/<strategy_name>/` directory with its own module files.
2. Do not modify `live_sim.py` or other files that running strategies depend on — add a new loop function instead.
3. Add a CLI command group in `src/cli.py`.
4. Add any new env vars to `config/settings.py` and `.env.example`.

### Adding a new fetcher

1. Create `src/fetchers/your_source.py`.
2. Implement `BaseFetcher` — `.get_markets()` and `.get_odds()`.
3. Register the fetcher in `FETCHER_MAP` in `src/engine/pipeline.py`.
4. Add any API key settings to `config/settings.py` and `.env.example`.

### Kalshi categories

Pass any of these to `--categories` (comma-separated, title-case):

| Category | Notes |
|---|---|
| `Crypto` | Hourly BTC/ETH/SOL/XRP/DOGE price-range buckets — primary target |
| `Economics` | Macro indicator series |
| `Financials` | Index and rate series |
| `Companies` | Earnings and stock price series |
| `Climate and Weather` | Daily temperature, precipitation, and wind markets |
| `Politics` | Election and policy markets |
| `Sports`, `Entertainment` | Event-driven markets |

Default: `Crypto,Economics,Financials`
