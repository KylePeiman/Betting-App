# CLAUDE.md — Betting App

## Project Overview
Python CLI application that fetches betting market data, generates recommendations via AI (Agent mode) or statistics (Compute mode), stores them in SQLite, and evaluates historical performance.

## Architecture
- `src/fetchers/` — one module per API source, all implement `BaseFetcher`
- `src/engine/` — `compute_mode.py` (EV/Kelly/arbitrage), `agent_mode.py` (Claude tools loop), `pipeline.py` (entry point)
- `src/storage/` — SQLAlchemy ORM with SQLite (swappable to Postgres via `DATABASE_URL`)
- `src/evaluator/` — ROI/hit-rate/CLV performance reports
- `src/cli.py` — Click CLI

## Key Commands
```bash
python -m src.cli run --mode compute --period week
python -m src.cli run --mode agent --period week
python -m src.cli evaluate
python -m src.cli recommendations list
python -m src.cli recommendations settle <id> --result win
```

## Adding a New Fetcher
1. Create `src/fetchers/your_source.py`
2. Implement `BaseFetcher` (`.get_markets()` and `.get_odds()`)
3. Register in `FETCHER_MAP` in `src/engine/pipeline.py`
4. Add API key to `config/settings.py` and `.env.example`

## Environment
Copy `.env.example` to `.env` and fill in your API keys. `ODDS_API_KEY` is sufficient to run compute mode.

## Models
- `Recommendation` — every generated bet with odds, confidence, rationale
- `Outcome` — win/loss/void result (added via `settle` CLI command)
- `EvaluationReport` — persisted ROI/hit-rate summaries

## Modes
- **compute**: Pure statistical EV/Kelly analysis, no API calls to Anthropic
- **agent**: Multi-turn Claude loop; uses tools to fetch markets and store recommendations; requires `ANTHROPIC_API_KEY`
