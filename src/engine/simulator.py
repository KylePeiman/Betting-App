"""
Simulation engine — paper-trade Kalshi markets, auto-settle, track results.

Usage:
    from src.engine.simulator import run_simulation, settle_open_bets, simulation_report
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from src.fetchers.kalshi import KalshiFetcher
from src.engine.compute_mode import run_compute
from src.storage.models import SimulatedBet


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def run_simulation(
    session: Session,
    min_ev: float = 0.03,
    categories: list[str] | None = None,
    verbose: bool = True,
) -> list[SimulatedBet]:
    """
    Fetch open Kalshi markets, run EV analysis, store positive-EV bets as
    SimulatedBets (paper trades — no real money).

    Returns the list of newly created SimulatedBet rows.
    """
    try:
        fetcher = KalshiFetcher()
    except Exception as exc:
        print(f"  Cannot initialise Kalshi fetcher: {exc}")
        return []

    # Temporary category override for this run
    original_filter = fetcher.category_filter
    if categories is not None:
        fetcher.category_filter = categories

    if verbose:
        print("  Fetching Kalshi markets...")
    markets = fetcher.get_markets()
    fetcher.category_filter = original_filter  # restore

    if verbose:
        print(f"  Running EV analysis on {len(markets)} markets (min_ev={min_ev:.0%})...")
    recommendations = run_compute(markets, min_ev=min_ev)

    new_bets: list[SimulatedBet] = []
    for rec in recommendations:
        ticker = rec.market.id
        side = rec.selection.name.lower()  # "yes" | "no"

        # Derive price in cents from the odds stored on the selection
        # implied_price = 100 / decimal_odds  (inverse of _cents_to_decimal_odds)
        entry_odds = rec.selection.odds
        entry_price_cents = round(100.0 / entry_odds, 2) if entry_odds > 1 else None
        if entry_price_cents is None:
            continue

        bet = SimulatedBet(
            ticker=ticker,
            title=rec.market.event_name,
            category=rec.market.category,
            side=side,
            entry_price_cents=entry_price_cents,
            entry_odds=entry_odds,
            stake_units=round(rec.kelly_fraction * 10, 4),
            kelly_fraction=rec.kelly_fraction,
            ev=rec.ev,
            confidence=rec.confidence,
            rationale=rec.rationale,
            closes_at=rec.market.starts_at,
        )
        session.add(bet)
        new_bets.append(bet)

    session.commit()

    if verbose:
        print(f"  Stored {len(new_bets)} simulated bets.")

    return new_bets


def settle_open_bets(
    session: Session,
    verbose: bool = True,
) -> dict[str, int]:
    """
    Poll Kalshi for each open SimulatedBet and settle any that have resolved.
    Returns a tally: {"settled": N, "still_open": M, "errors": K}.
    """
    try:
        fetcher = KalshiFetcher()
    except Exception as exc:
        print(f"  Cannot initialise Kalshi fetcher: {exc}")
        return {"settled": 0, "still_open": 0, "errors": 1}

    open_bets: list[SimulatedBet] = (
        session.query(SimulatedBet).filter(SimulatedBet.status == "open").all()
    )

    tally = {"settled": 0, "still_open": 0, "errors": 0}
    now = datetime.now(timezone.utc)

    for bet in open_bets:
        try:
            mkt = fetcher.get_market_status(bet.ticker)
        except Exception as exc:
            if verbose:
                print(f"  Error fetching {bet.ticker}: {exc}")
            tally["errors"] += 1
            continue

        mkt_status: str = mkt.get("status", "")
        mkt_result: str | None = mkt.get("result")  # "yes" | "no" | None

        if mkt_status not in ("finalized", "settled", "closed") or mkt_result is None:
            tally["still_open"] += 1
            continue

        # Determine win/loss/void
        if mkt_result == "yes":
            result = "win" if bet.side == "yes" else "loss"
        elif mkt_result == "no":
            result = "win" if bet.side == "no" else "loss"
        else:
            result = "void"

        # P&L in units
        if result == "win":
            pnl = bet.stake_units * (bet.entry_odds - 1.0)
        elif result == "loss":
            pnl = -bet.stake_units
        else:  # void
            pnl = 0.0

        # Exit price from closing market prices (best effort)
        exit_price: float | None = None
        exit_yes_bid = mkt.get("yes_bid")
        exit_yes_ask = mkt.get("yes_ask")
        if exit_yes_bid is not None and exit_yes_ask is not None:
            exit_price = (exit_yes_bid + exit_yes_ask) / 2

        bet.status = "settled"
        bet.result = result
        bet.pnl_units = round(pnl, 4)
        bet.exit_price_cents = exit_price
        bet.settled_at = now

        tally["settled"] += 1
        if verbose:
            print(f"  Settled {bet.ticker} ({bet.side}) → {result}  P&L={pnl:+.4f} units")

    session.commit()
    return tally


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def simulation_report(session: Session) -> dict[str, Any]:
    """
    Aggregate performance stats across all settled SimulatedBets.
    Returns a dict suitable for display or further processing.
    """
    all_bets: list[SimulatedBet] = session.query(SimulatedBet).all()

    open_count = sum(1 for b in all_bets if b.status == "open")
    settled = [b for b in all_bets if b.status == "settled"]

    wins = [b for b in settled if b.result == "win"]
    losses = [b for b in settled if b.result == "loss"]
    voids = [b for b in settled if b.result == "void"]

    total_staked = sum(b.stake_units for b in settled if b.result != "void")
    total_pnl = sum(b.pnl_units for b in settled if b.pnl_units is not None)

    roi = total_pnl / total_staked if total_staked > 0 else 0.0
    hit_rate = len(wins) / (len(wins) + len(losses)) if (wins or losses) else 0.0

    # Breakdown by category
    cat_stats: dict[str, dict] = {}
    for b in settled:
        cat = b.category or "other"
        s = cat_stats.setdefault(cat, {"wins": 0, "losses": 0, "pnl": 0.0, "staked": 0.0})
        if b.result == "win":
            s["wins"] += 1
            s["pnl"] += b.pnl_units or 0.0
            s["staked"] += b.stake_units
        elif b.result == "loss":
            s["losses"] += 1
            s["pnl"] += b.pnl_units or 0.0
            s["staked"] += b.stake_units

    return {
        "total": len(all_bets),
        "open": open_count,
        "settled": len(settled),
        "wins": len(wins),
        "losses": len(losses),
        "voids": len(voids),
        "hit_rate": hit_rate,
        "roi": roi,
        "total_pnl_units": total_pnl,
        "total_staked_units": total_staked,
        "by_category": cat_stats,
    }


def print_simulation_report(stats: dict[str, Any]) -> None:
    sep = "=" * 62
    print(sep)
    print("  KALSHI SIMULATION REPORT")
    print(sep)
    print(f"  Total bets:    {stats['total']}  (open={stats['open']}, settled={stats['settled']})")
    print(f"  W / L / V:     {stats['wins']} / {stats['losses']} / {stats['voids']}")
    print(f"  Hit rate:      {stats['hit_rate']:.1%}")
    print(f"  ROI:           {stats['roi']:+.2%}")
    print(f"  Units staked:  {stats['total_staked_units']:.2f}")
    print(f"  Units P&L:     {stats['total_pnl_units']:+.4f}")
    if stats["by_category"]:
        print()
        print("  -- By Category --")
        for cat, s in sorted(stats["by_category"].items()):
            roi = s["pnl"] / s["staked"] if s["staked"] > 0 else 0.0
            print(f"    {cat:<30}  W={s['wins']} L={s['losses']}  ROI={roi:+.2%}")
    print(sep)
