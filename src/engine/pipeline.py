"""Unified pipeline entry point for both agent and computation modes."""
from __future__ import annotations
from typing import Literal

from src.fetchers.base import Market
from src.fetchers.odds_api import OddsAPIFetcher
from src.fetchers.betfair import BetfairFetcher
from src.fetchers.sportsdata import SportsDataFetcher
from src.fetchers.mock import MockFetcher
from src.fetchers.kalshi import KalshiFetcher
from src.fetchers.polymarket import PolymarketFetcher
from src.storage.db import get_session
from src.storage.models import Recommendation
from config.settings import settings


FETCHER_MAP = {
    "odds_api": OddsAPIFetcher,
    "betfair": BetfairFetcher,
    "sportsdata": SportsDataFetcher,
    "mock": MockFetcher,
    "kalshi": KalshiFetcher,
    "polymarket": PolymarketFetcher,
}


def _build_fetchers(sources: list[str]) -> dict:
    fetchers = {}
    for name in sources:
        cls = FETCHER_MAP.get(name)
        if cls is None:
            print(f"Warning: Unknown source '{name}', skipping.")
            continue
        try:
            fetchers[name] = cls()
        except Exception as exc:
            print(f"Warning: Could not initialise fetcher '{name}': {exc}")
    return fetchers


def run(
    mode: Literal["agent", "compute"] = "compute",
    period: Literal["week", "month"] = "week",
    sources: list[str] | None = None,
    verbose: bool = True,
    min_ev: float | None = None,
) -> list[Recommendation]:
    """
    Main pipeline entry point.
    Fetches data from configured sources -> runs selected engine -> stores recommendations.
    """
    if sources is None:
        sources = settings.DEFAULT_SOURCES

    if verbose:
        print(f"Running pipeline: mode={mode}, period={period}, sources={sources}")

    fetchers = _build_fetchers(sources)
    if not fetchers:
        print("No fetchers available. Check your API keys and source configuration.")
        return []

    session = get_session()
    stored: list[Recommendation] = []

    if mode == "compute":
        from src.engine.compute_mode import run_compute

        # Fetch markets from all sources
        all_markets: list[Market] = []
        for name, fetcher in fetchers.items():
            if verbose:
                print(f"  Fetching markets from {name}...")
            try:
                markets = fetcher.get_markets()
                all_markets.extend(markets)
                if verbose:
                    print(f"  -> Got {len(markets)} markets from {name}")
            except Exception as exc:
                print(f"  Error fetching from {name}: {exc}")

        if verbose:
            print(f"  Analysing {len(all_markets)} markets...")

        effective_min_ev = min_ev if min_ev is not None else settings.MIN_EV_THRESHOLD
        recommendations = run_compute(all_markets, min_ev=effective_min_ev)

        if verbose:
            print(f"  Found {len(recommendations)} positive-EV recommendations")

        for rec in recommendations:
            db_rec = Recommendation(
                period=period,
                mode=mode,
                source=rec.market.source,
                category=rec.market.category,
                event_name=rec.market.event_name,
                selection=rec.selection.name,
                odds=rec.selection.odds,
                stake_units=round(rec.kelly_fraction * 10, 4),  # in units (10 unit bankroll)
                confidence=round(rec.confidence, 4),
                rationale=rec.rationale,
            )
            session.add(db_rec)
            stored.append(db_rec)

    elif mode == "agent":
        from src.engine.agent_mode import run_agent

        if verbose:
            print("  Starting agent mode...")

        raw_recs = run_agent(fetchers=fetchers, period=period, db_session=session, verbose=verbose)

        for raw in raw_recs:
            db_rec = Recommendation(
                period=period,
                mode=mode,
                source=raw.get("source", "agent"),
                category=raw.get("category", "unknown"),
                event_name=raw.get("event_name", ""),
                selection=raw.get("selection", ""),
                odds=float(raw.get("odds", 0.0)),
                stake_units=float(raw.get("stake_units", 1.0)),
                confidence=float(raw.get("confidence", 0.5)),
                rationale=raw.get("rationale", ""),
            )
            session.add(db_rec)
            stored.append(db_rec)

    session.commit()

    if verbose:
        print(f"  Stored {len(stored)} recommendations to database.")

    return stored
