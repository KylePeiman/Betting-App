# Betting App

A Python CLI application for generating data-driven betting recommendations across sports, politics, esports, entertainment, and financial markets.

## Features

- **Multi-source data**: TheOddsAPI, Betfair Exchange, SportsDataIO
- **Two analysis modes**:
  - **Compute**: Pure statistical analysis — Expected Value, Kelly Criterion, arbitrage detection
  - **Agent**: Claude AI orchestrates market analysis and produces narrative recommendations
- **Persistent storage**: SQLite (via SQLAlchemy), upgradeable to Postgres
- **Performance tracking**: ROI, hit rate, units P&L by mode and category
- **CLI**: Full management of recommendations lifecycle

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env with your API keys

# 3. Run (DB is auto-created on first run)
python -m src.cli run --mode compute --period week
```

## CLI Reference

```bash
# Generate recommendations
python -m src.cli run --mode compute --period week
python -m src.cli run --mode agent --period week
python -m src.cli run --mode compute --period month --sources odds_api betfair

# Evaluate performance
python -m src.cli evaluate
python -m src.cli evaluate --from 2026-01-01 --to 2026-03-08

# Manage recommendations
python -m src.cli recommendations list
python -m src.cli recommendations list --status pending --mode compute
python -m src.cli recommendations show <id>
python -m src.cli recommendations settle <id> --result win
python -m src.cli recommendations settle <id> --result loss
```

## API Keys

| Service | Required For | Free Tier |
|---------|-------------|-----------|
| [TheOddsAPI](https://the-odds-api.com) | Compute + Agent mode | Yes (500 req/mo) |
| [Anthropic](https://console.anthropic.com) | Agent mode only | No |
| [Betfair](https://developer.betfair.com) | Optional extra source | No |
| [SportsDataIO](https://sportsdata.io) | Optional stats enrichment | Limited |

## Adding a New Data Source

1. Create `src/fetchers/your_source.py` implementing `BaseFetcher`
2. Register it in `FETCHER_MAP` in `src/engine/pipeline.py`
3. Add config to `config/settings.py` and `.env.example`

## Project Structure

```
src/
├── fetchers/       # Data source adapters (BaseFetcher interface)
├── engine/         # compute_mode.py, agent_mode.py, pipeline.py
├── storage/        # SQLAlchemy models + db session
├── evaluator/      # Performance reports
└── cli.py          # Click CLI
config/settings.py  # Env-based configuration
```
