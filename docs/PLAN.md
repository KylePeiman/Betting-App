# Plan: Betting App — Architecture & Implementation

## Overview

A Python CLI application that fetches betting market data from multiple sources, generates recommendations using either AI-driven (Agent mode) or statistical (Compute mode) analysis, stores them in a database, and evaluates historical performance.

## Architecture

### Data Flow

```
Data Sources → Fetchers → Engine (Agent/Compute) → Storage → Evaluator
                                                        ↓
                                                   CLI Output
```

### Components

#### 1. Fetchers (`src/fetchers/`)
Each fetcher implements the `BaseFetcher` abstract interface:
- `get_markets(**kwargs) -> list[Market]` — retrieve available markets
- `get_odds(market_id, **kwargs) -> Market` — retrieve current odds for a specific market

**Implemented fetchers:**
- `OddsAPIFetcher` — TheOddsAPI (sports, politics, entertainment on higher tiers)
- `BetfairFetcher` — Betfair Exchange (broadest market variety)
- `SportsDataFetcher` — SportsDataIO (stats enrichment, no live odds)

**Data models:**
- `Selection` — a single outcome with name, decimal odds, and metadata
- `Market` — an event with ID, category, event name, start time, selections, and source

#### 2. Engine (`src/engine/`)

**Compute Mode (`compute_mode.py`):**
- Pure statistical analysis with no external AI calls
- Implied probability calculation (vig removal via proportional method)
- Expected Value (EV) computation: `EV = (p * odds) - 1`
- Kelly Criterion stake sizing: `f = (p * odds - 1) / (odds - 1)`
- Arbitrage detection across multiple bookmakers
- Returns `BetRecommendation` objects sorted by EV

**Agent Mode (`agent_mode.py`):**
- Multi-turn Claude AI loop using tool use
- Tools: `fetch_markets`, `fetch_odds`, `get_historical_performance`, `store_recommendation`
- Claude analyses markets, identifies value, and stores recommendations with narrative rationale
- Requires `ANTHROPIC_API_KEY`

**Pipeline (`pipeline.py`):**
- Unified entry point that orchestrates fetchers and engine modes
- Builds fetcher instances from configured sources
- Routes to compute or agent mode
- Persists recommendations to database

#### 3. Storage (`src/storage/`)

**Database (`db.py`):**
- SQLAlchemy with SQLite (default), swappable to Postgres via `DATABASE_URL`
- Auto-creates tables on first run

**Models (`models.py`):**
- `Recommendation` — every generated bet (event, selection, odds, confidence, rationale, status)
- `Outcome` — win/loss/void result linked to a recommendation
- `EvaluationReport` — persisted performance summaries with mode breakdown

#### 4. Evaluator (`src/evaluator/`)

**Performance (`performance.py`):**
- Calculates ROI, hit rate, units P&L for settled recommendations
- Breakdowns by mode (agent vs compute) and category (sport type)
- Persists evaluation reports to database
- Pretty-print report output

#### 5. CLI (`src/cli.py`)

**Commands:**
- `run` — fetch markets and generate recommendations (--mode, --period, --sources)
- `evaluate` — evaluate historical performance (--from, --to)
- `recommendations list` — list stored recommendations (--limit, --status, --mode)
- `recommendations show <id>` — show full recommendation details
- `recommendations settle <id>` — settle with win/loss/void result

### Configuration (`config/settings.py`)
- Environment variables loaded via python-dotenv
- API keys for all data sources and Anthropic
- Database URL, Claude model selection
- Engine defaults (min EV threshold, default sources)

## Implementation Phases

### Phase 1: Core Infrastructure
- [x] Project structure and configuration
- [x] Base fetcher interface and data models
- [x] Database setup and ORM models
- [x] CLI skeleton

### Phase 2: Data Sources
- [x] TheOddsAPI fetcher
- [x] Betfair Exchange fetcher
- [x] SportsDataIO fetcher

### Phase 3: Analysis Engine
- [x] Compute mode (EV, Kelly, arbitrage)
- [x] Agent mode (Claude tool loop)
- [x] Pipeline orchestration

### Phase 4: Evaluation & Reporting
- [x] Performance evaluator
- [x] Report generation and persistence
- [x] CLI evaluate command

### Phase 5: CLI Completeness
- [x] Recommendations management (list, show, settle)
- [x] Full CLI with all commands

## Verification

To verify the implementation:

1. **Structure**: All files exist in the correct locations per the project structure
2. **Imports**: Each module's imports resolve correctly
3. **CLI**: `python -m src.cli --help` shows all commands
4. **Compute**: `python -m src.cli run --mode compute` fetches and analyses markets
5. **Agent**: `python -m src.cli run --mode agent` runs the Claude analysis loop
6. **Evaluate**: `python -m src.cli evaluate` produces a performance report
7. **Settle**: `python -m src.cli recommendations settle <id> --result win` records outcomes
