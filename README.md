# Kalshi Last-Second Sniper

A Python CLI application that trades Kalshi crypto bucket markets using a last-second price convergence strategy. Real-time spot prices and orderbook data are streamed via WebSocket from Kraken and Kalshi, and the scanner fires instantly on every price update.

## Strategy

Kalshi hourly crypto series divide each asset's price into mutually exclusive buckets (e.g. BTC $84,000–$85,000). Each bucket resolves based on the CF Benchmarks 60-second average in the final minute before close.

The sniper enters positions in the final entry window (default 2 minutes) when:
- The Kraken spot price has been **stable** (< 0.3% movement over 15 seconds)
- The spot price is **well inside** a bucket (≥ 15% of bucket width from each edge)
- **YES trade**: spot is inside the bucket and YES ask is in the tradeable range
- **NO trade**: spot is clearly outside a bucket and NO ask is in the tradeable range

Real-time WebSocket feeds from both Kraken (spot prices) and Kalshi (orderbook) mean the scanner reacts to price changes within milliseconds rather than polling on a fixed interval.

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — see API Keys section below

# 3. Run
python -m src.cli --simulate        # paper trade
python -m src.cli --live            # real orders
```

---

## API Keys

### Required

```env
KALSHI_API_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi_private_key.pem
```

Get these from [kalshi.com/profile/api-keys](https://kalshi.com/profile/api-keys). Download the PEM private key file and set the path accordingly.

### Optional

```env
# Headline prediction trades (Claude + NewsAPI)
ANTHROPIC_API_KEY=your-anthropic-key
NEWS_API_KEY=your-newsapi-key

# Override defaults
DATABASE_URL=sqlite:///betting_app.db
CLAUDE_MODEL=claude-sonnet-4-6
KALSHI_CATEGORIES=Crypto,Economics,Financials
```

---

## Quick Start

```bash
# Paper trade with all defaults (streaming on, last-second on)
python -m src.cli --simulate

# Paper trade without WebSocket streaming (REST polling fallback)
python -m src.cli --simulate --no-streaming

# Real orders — auto-detects Kalshi account balance
python -m src.cli --live

# Full options via the live subcommand
python -m src.cli live --simulate --bankroll 10.00
```

---

## CLI Reference

### Root command — `python -m src.cli`

The simplest way to run a simulation. Starts the sniper with sensible defaults.

```bash
python -m src.cli [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--simulate` | — | Paper trade — no real orders placed |
| `--live` | — | Place real orders on Kalshi |
| `--bankroll FLOAT` | `5.00` (sim) / account balance (live) | Starting bankroll in USD |
| `--last-second` / `--no-last-second` | on | Enable/disable last-second convergence strategy |
| `--streaming` / `--no-streaming` | on | Use WebSocket streaming for real-time prices |
| `--prediction` | off | Enable Claude + NewsAPI headline prediction trades |

---

### `live` subcommand — full options

```bash
python -m src.cli live [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `--simulate` / `--live` | — | **Required.** Paper trade or real orders |
| `--bankroll FLOAT` | `5.00` / account balance | Starting bankroll in USD |
| `--last-second` / `--no-last-second` | on | Enable/disable last-second strategy |
| `--streaming` / `--no-streaming` | on | Enable/disable WebSocket streaming |
| `--prediction` | off | Enable headline prediction trades |
| `--interval INT` | `15` | Seconds between full Kalshi REST market scans |
| `--settle-interval INT` | `5` | Max seconds between settlement checks (event-driven when streaming) |
| `--categories TEXT` | `Crypto,Economics,Financials` | Comma-separated Kalshi categories to scan |
| `--near-term INT` | `60` | Only consider markets closing within this many minutes |
| `--max-position FLOAT` | `0.10` | Max fraction of bankroll per single position |
| `--logs-dir TEXT` | `logs` | Directory for log files |
| `--resume INT` | — | Resume an existing session by ID |
| `--ls-entry-window INT` | `120` | Seconds before close to start monitoring for entries |
| `--ls-min-yes INT` | `70` | Minimum YES ask in cents to enter a YES trade |
| `--ls-max-yes INT` | `99` | Maximum YES ask in cents to enter a YES trade |
| `--ls-min-no INT` | `3` | Minimum NO ask in cents to enter a NO trade |
| `--ls-max-no INT` | `40` | Maximum NO ask in cents to enter a NO trade |
| `--ls-edge-buffer FLOAT` | `0.15` | Fraction of bucket width spot must be from edges |

**Examples:**

```bash
# Narrow the entry window to the final 60 seconds
python -m src.cli live --simulate --ls-entry-window 60

# Widen the near-term scan to 2 hours
python -m src.cli live --simulate --near-term 120

# Run without streaming to compare against a streaming session
python -m src.cli live --simulate --no-streaming

# Resume a previous session
python -m src.cli live --live --resume 4

# Real orders with manual bankroll
python -m src.cli live --live --bankroll 25.00
```

---

### `simulate sessions` — list all sessions

```bash
python -m src.cli simulate sessions [--limit 10]
```

Prints a table of all sessions with P&L, win/loss record, and status.

---

### `arb scan` — one-shot market scan

```bash
python -m src.cli arb scan [--categories "Crypto"] [--type series]
```

Scans Kalshi once and prints any opportunities found. No trades are placed.

---

## Dashboard

A Streamlit dashboard is included for monitoring sessions, open positions, and trade history.

```bash
# Local only
streamlit run dashboard.py

# Accessible from any device on the same network
streamlit run dashboard.py --server.address 0.0.0.0 --server.port 8501
```

Then open `http://<your-machine-ip>:8501` on any device on the network.

To make network access permanent, create `~/.streamlit/config.toml`:

```toml
[server]
address = "0.0.0.0"
port = 8501
```

---

## Utility Scripts

### Clear the database

```bash
python scripts/clear_db.py
```

Wipes all sessions, positions, and historical records. Prompts for confirmation before proceeding. The database schema is preserved — the app starts fresh on next run.

---

## How It Works

### WebSocket streaming

Two persistent WebSocket connections run on background threads:

- **Kraken** (`wss://ws.kraken.com/v2`) — public feed, no auth. Streams real-time ask prices for BTC, ETH, SOL, XRP, DOGE. Updates `PriceCache` on every tick.
- **Kalshi** (`wss://api.elections.kalshi.com/trade-api/ws/v2`) — RSA-PSS authenticated (same key as REST). Subscribes to `orderbook_delta` for near-term market tickers. Updates `PriceCache` with the best YES ask on every orderbook change.

The main loop blocks on a `threading.Event` that fires whenever either feed writes a new price. The scanner wakes up immediately and checks only the markets relevant to what just changed (filtered by triggered pair/ticker), then goes back to waiting.

### Entry decision flow

On each price update:
1. Identify which Kraken pairs or Kalshi tickers just changed
2. Filter the near-term market cache to relevant buckets only
3. Check stability: spot price must have moved < 0.3% over the last 15 seconds
4. Check edge buffer: spot must be ≥ 15% of bucket width from both edges
5. **YES entry**: spot is inside the bucket, YES ask in `[ls-min-yes, ls-max-yes]`
6. **NO entry**: spot is clearly outside the bucket, NO ask in `[ls-min-no, ls-max-no]`
7. If the WS cache has a fresh yes_ask (< 10s old), that overrides the stale REST value before entry

### Settlement

After the entry check each tick, resolved positions are settled by polling `GET /markets/{ticker}`. A position settles only when `status` is `finalized`/`settled`/`closed` **and** `result` is `"yes"` or `"no"` — empty string is treated as unresolved.

### Authentication

Kalshi uses RSA-PSS SHA-256. Sign string: `{timestamp_ms}GET/trade-api/v2{path}`. Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`. The WebSocket connection uses the same signing scheme passed as HTTP headers during the handshake.

---

## Project Structure

```
src/
├── fetchers/
│   ├── base.py              # BaseFetcher ABC, Market/Selection dataclasses
│   ├── kalshi.py            # Kalshi REST API — auth, market fetch, order placement
│   ├── crypto_prices.py     # Kraken REST price fetch (fallback when streaming off)
│   └── news.py              # NewsAPI client for headline prediction trades
├── engine/
│   ├── live_sim.py          # Main simulation loop — entry, settlement, bankroll tracking
│   ├── last_second.py       # Last-second strategy — PriceTracker, bucket matching, scanner
│   ├── prediction.py        # Headline signal detection + Claude review
│   ├── arbitrage.py         # Legacy arb scanner
│   └── agent_advisor.py     # Legacy Claude agent (unused)
├── streaming/
│   ├── price_cache.py       # Thread-safe cache — spot prices + yes_ask, update_event
│   ├── kraken_ws.py         # Kraken WebSocket client (public)
│   ├── kalshi_ws.py         # Kalshi WebSocket client (RSA-PSS auth)
│   └── manager.py           # StreamManager — starts/stops both feeds, manages subscriptions
├── storage/
│   ├── models.py            # ORM: SimSession, SimPosition, ArbSimulation, Recommendation
│   └── db.py                # SQLAlchemy session factory + auto-migration
└── cli.py                   # Click CLI entry point
config/
└── settings.py              # All env vars via python-dotenv
scripts/
└── clear_db.py              # Wipe all DB records (with confirmation prompt)
dashboard.py                 # Streamlit dashboard
```

---

## Kalshi Categories

Pass any of these to `--categories` (comma-separated):

| Category | Notes |
|---|---|
| `Crypto` | Hourly BTC/ETH/SOL/XRP/DOGE price-range buckets — primary target |
| `Economics` | Macro indicator series |
| `Financials` | Index and rate series |
| `Companies` | Earnings and stock price series |
| `Politics`, `Sports`, `Entertainment` | Rarely have bucket-style markets |

Default: `Crypto,Economics,Financials`
