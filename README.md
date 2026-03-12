# Kalshi Betting App

A Python CLI application that scans [Kalshi](https://kalshi.com) prediction markets for arbitrage opportunities, executes trades (paper or live), and tracks performance over time.

## Strategy

The app runs a continuous **arbitrage scanner** targeting Kalshi price-range series (BTC, ETH, SOL, XRP, DOGE, etc.). These hourly series are mutually exclusive and collectively exhaustive — buying YES on every price bucket guarantees a payout of 100¢ regardless of outcome. When the sum of all ask prices falls below 100¢, a risk-free profit exists.

The scanner only enters a position when it has verified full coverage of every bucket in the series. Partial coverage (where some buckets are illiquid or unquoted) is skipped to avoid directional exposure.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Fill in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH

# 3. Run
python -m src.cli live --simulate   # paper trade
python -m src.cli live --live       # real orders (prompts for confirmation)
```

## API Keys

Go to [kalshi.com/profile/api-keys](https://kalshi.com/profile/api-keys), create an API key, and download the private key PEM file.

```env
KALSHI_API_KEY_ID=your-key-id-here
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi_private_key.pem
```

Anthropic API key is only required for the (deprecated) `--mode agent` recommendation engine:

```env
ANTHROPIC_API_KEY=your-anthropic-key-here
```

## CLI Reference

### Live Arb Scanner

```bash
# Paper trade — virtual bankroll, no real orders
python -m src.cli live --simulate
python -m src.cli live --simulate --bankroll 100.00

# Live trading — auto-detects Kalshi account balance, prompts for confirmation
python -m src.cli live --live
python -m src.cli live --live --bankroll 10.00   # override balance

# Resume a previous session
python -m src.cli live --live --resume <session-id>

# Options
python -m src.cli live --simulate \
  --categories "Crypto,Economics,Financials" \  # which Kalshi categories to scan
  --near-term 60 \                              # only events closing within N minutes
  --min-arb-profit 1.0 \                        # minimum profit in cents to enter
  --max-position 0.10 \                         # max fraction of bankroll per position
  --max-deploy 0.80 \                           # max fraction to deploy per scan cycle
  --interval 60 \                               # seconds between full market scans
  --settle-interval 5                           # seconds between settlement polls
```

### Session Management

```bash
# List all live sessions with P&L
python -m src.cli simulate sessions

# One-shot arb scan (inspect without trading)
python -m src.cli arb scan
python -m src.cli arb scan --categories "Crypto,Economics,Financials" --type series

# Record and settle arb simulations manually
python -m src.cli arb simulate
python -m src.cli arb settle
python -m src.cli arb list
python -m src.cli arb report
```

### Recommendations (EV/Agent mode)

```bash
python -m src.cli run --mode compute
python -m src.cli run --mode agent
python -m src.cli recommendations list
python -m src.cli recommendations settle <id> --result win
python -m src.cli evaluate
```

## How It Works

**Authentication**: Kalshi uses RSA-PSS (SHA-256). The app signs every request with `{timestamp_ms}{METHOD}/trade-api/v2{path}`.

**Pricing**: Kalshi quotes yes/no prices in cents (1–99). The app reads both the legacy `yes_ask` (int cents) and the newer `yes_ask_dollars` (float) field formats.

**Arb detection**:
1. Fetch all open events with nested markets (`GET /events?with_nested_markets=true`)
2. Group mutually-exclusive markets by `event_ticker`
3. Sum all YES ask prices — if total < 100¢, profit = `100 - total`
4. Only enter if `len(liquid_legs) == total_markets_in_event` (full coverage verified using raw event market count before bid/ask filtering)

**Position sizing**:
- Series arbs: 15% of liquid bankroll per set, minimum 1 set
- Binary arbs: 20% of liquid bankroll per set, minimum 1 set
- Per-cycle deploy cap: 80% of liquid bankroll (reserves kept)

**Live orders**: `POST /portfolio/orders` with a limit buy at the ask price. If any leg fails to fill within 2 seconds, all placed legs are cancelled and the position is skipped.

**Settlement**: Polls `GET /markets/{ticker}` each tick. Settles only when `status` is `finalized`/`settled`/`closed` **and** `result` is `"yes"` or `"no"` (empty string treated as unresolved).

## Kalshi Categories

Pass any of these to `--categories` (comma-separated, title-case):

| Category | Arb frequency |
|---|---|
| `Crypto` | High — hourly BTC/ETH/SOL/XRP/DOGE price-range series |
| `Economics` | Medium — macro indicator series |
| `Financials` | Medium — index/rate series |
| `Companies` | Low |
| `Politics`, `Sports`, `Entertainment`, etc. | Rarely have exhaustive series |

Default: `Crypto,Economics,Financials`

## Configuration

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | — | **Required.** Your Kalshi API key ID |
| `KALSHI_PRIVATE_KEY_PATH` | — | **Required.** Path to RSA private key PEM |
| `KALSHI_CATEGORIES` | *(all)* | Optional category filter |
| `MIN_EV_THRESHOLD` | `0.005` | Minimum EV for recommendation engine |
| `DATABASE_URL` | `sqlite:///betting_app.db` | SQLAlchemy connection string |
| `ANTHROPIC_API_KEY` | — | Required for `--mode agent` only |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Claude model for agent mode |

## Project Structure

```
src/
├── fetchers/
│   ├── base.py          # BaseFetcher ABC, Market/Selection dataclasses
│   └── kalshi.py        # Kalshi REST API — auth, market fetch, order placement
├── engine/
│   ├── arbitrage.py     # Arb scanner — binary + series detection, exhaustiveness check
│   ├── live_sim.py      # Continuous arb loop — scan, enter, settle, bankroll tracking
│   ├── simulator.py     # One-shot EV paper-trade simulator
│   ├── compute_mode.py  # EV, Kelly Criterion, vig removal
│   ├── agent_mode.py    # Claude multi-turn tool loop (deprecated)
│   └── pipeline.py      # Unified run() entry point for recommendation engine
├── storage/
│   ├── models.py        # ORM: Recommendation, SimulatedBet, SimSession, SimPosition, ArbSimulation
│   └── db.py            # SQLAlchemy session factory + auto-migration
├── evaluator/
│   └── performance.py   # ROI / hit-rate / CLV reports
└── cli.py               # Click CLI entry point
config/settings.py       # Environment-based configuration
```

## Adding a New Fetcher

1. Create `src/fetchers/your_source.py`
2. Implement `BaseFetcher` (`.get_markets()` and `.get_odds()`)
3. Register in `FETCHER_MAP` in `src/engine/pipeline.py`
4. Add API key to `config/settings.py` and `.env.example`
