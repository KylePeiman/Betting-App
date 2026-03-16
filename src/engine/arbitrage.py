"""
Micro-arbitrage scanner for Kalshi prediction markets.

Two arb types:
  binary — buy YES + NO on the same market; guaranteed profit when yes_ask + no_ask < 100
  series — buy YES on every leg of a mutually-exclusive price-range series;
            profit when sum(yes_asks) < 100. Only risk-free if the series is
            collectively exhaustive (all possible outcomes covered).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.storage.models import ArbSimulation


@dataclass
class ArbLeg:
    ticker: str
    side: str          # "yes" | "no"
    price_cents: int   # ask price paid


@dataclass
class ArbOpportunity:
    arb_type: str                    # "binary" | "series"
    event_ticker: str
    category: str
    title: str
    legs: list[ArbLeg]
    total_cost_cents: float          # sum of leg prices
    profit_cents: float              # 100 - total_cost_cents
    profit_pct: float                # profit_cents / total_cost_cents
    guaranteed: bool                 # True = provably risk-free
    closes_at: datetime | None = None


def scan_binary_arb(markets: list, min_profit_cents: float = 1.0) -> list[ArbOpportunity]:
    """
    Find markets where yes_ask + no_ask < 100.
    Each such market is a risk-free arb: buy 1 YES contract + 1 NO contract,
    guaranteed payout of 100¢ regardless of outcome.
    """
    opps: list[ArbOpportunity] = []
    for market in markets:
        yes_sel = next((s for s in market.selections if s.name == "Yes"), None)
        no_sel  = next((s for s in market.selections if s.name == "No"),  None)
        if yes_sel is None or no_sel is None:
            continue

        yes_ask = yes_sel.metadata.get("yes_ask", 0)
        no_ask  = no_sel.metadata.get("no_ask", 0)
        if not yes_ask or not no_ask:
            continue

        total_cost = yes_ask + no_ask
        profit = 100.0 - total_cost
        if profit < min_profit_cents:
            continue

        opps.append(ArbOpportunity(
            arb_type="binary",
            event_ticker=market.metadata.get("event_ticker", market.id),
            category=market.category,
            title=market.event_name,
            legs=[
                ArbLeg(ticker=market.id, side="yes", price_cents=yes_ask),
                ArbLeg(ticker=market.id, side="no",  price_cents=no_ask),
            ],
            total_cost_cents=float(total_cost),
            profit_cents=float(profit),
            profit_pct=profit / total_cost,
            guaranteed=True,
            closes_at=market.starts_at,
        ))

    opps.sort(key=lambda o: o.profit_pct, reverse=True)
    return opps


def scan_series_arb(
    markets: list,
    min_profit_cents: float = 1.0,
) -> list[ArbOpportunity]:
    """
    Group markets by event_ticker where mutually_exclusive=True.
    If sum(yes_asks) < 100, buying YES on every leg pays 100¢ if any resolves YES.
    Flagged as guaranteed=True only for price-range series (KXBTC*, KXETH*, KXDOGE*, etc.)
    where the ranges are assumed to be exhaustive.
    """
    from collections import defaultdict

    # Group by (event_ticker, close_time) so markets from the same event but
    # different hours don't get merged into one over-broad exhaustiveness check.
    groups: dict[str, list] = defaultdict(list)
    for market in markets:
        meta = market.metadata or {}
        if not meta.get("mutually_exclusive"):
            continue
        event_ticker = meta.get("event_ticker", "")
        if not event_ticker:
            continue
        close_time = market.starts_at.isoformat() if market.starts_at else ""
        group_key = f"{event_ticker}|{close_time}"
        groups[group_key].append(market)

    opps: list[ArbOpportunity] = []
    for group_key, mkt_list in groups.items():
        event_ticker = group_key.split("|")[0]
        if len(mkt_list) < 2:
            continue

        legs: list[ArbLeg] = []
        total_cost = 0.0
        closes_at = None

        for mkt in mkt_list:
            yes_sel = next((s for s in mkt.selections if s.name == "Yes"), None)
            if yes_sel is None:
                continue
            yes_ask = yes_sel.metadata.get("yes_ask") or 0
            if not yes_ask:
                # Fall back to dollars field if present
                yes_ask_dollars = yes_sel.metadata.get("yes_ask_dollars") or 0
                yes_ask = int(round(yes_ask_dollars * 100)) if yes_ask_dollars else 0
            if not yes_ask:
                continue
            legs.append(ArbLeg(ticker=mkt.id, side="yes", price_cents=yes_ask))
            total_cost += yes_ask
            if mkt.starts_at and (closes_at is None or mkt.starts_at > closes_at):
                closes_at = mkt.starts_at

        if len(legs) < 2:
            continue

        profit = 100.0 - total_cost
        if profit < min_profit_cents:
            continue

        # Only guaranteed if we hold a leg for EVERY market in the event.
        # Use total_markets_in_event (raw count before bid/ask filtering) so that
        # illiquid buckets with no quotes don't get silently excluded — if any
        # bucket is unquoted we can't buy it, meaning that outcome is uncovered.
        first_mkt = mkt_list[0]
        series_ticker = first_mkt.metadata.get("series_ticker", "")
        total_in_event = first_mkt.metadata.get("total_markets_in_event", 0)
        _EXHAUSTIVE_PREFIXES = ("KXBTC", "KXETH", "KXDOGE", "KXXRP", "KXSOL", "KXSPY", "KXNBER")
        # Must cover every raw market in the event (not just those with valid quotes)
        all_markets_covered = (total_in_event > 0 and len(legs) == total_in_event)
        guaranteed = all_markets_covered and any(series_ticker.startswith(p) for p in _EXHAUSTIVE_PREFIXES)

        opps.append(ArbOpportunity(
            arb_type="series",
            event_ticker=event_ticker,
            category=first_mkt.category,
            title=first_mkt.event_name,
            legs=legs,
            total_cost_cents=total_cost,
            profit_cents=profit,
            profit_pct=profit / total_cost if total_cost > 0 else 0.0,
            guaranteed=guaranteed,
            closes_at=closes_at,
        ))

    opps.sort(key=lambda o: o.profit_pct, reverse=True)
    return opps


def opportunities_to_sim(opps: list[ArbOpportunity]) -> list[ArbSimulation]:
    """Convert ArbOpportunity objects to unsaved ArbSimulation ORM rows."""
    rows = []
    for opp in opps:
        row = ArbSimulation(
            arb_type=opp.arb_type,
            event_ticker=opp.event_ticker,
            category=opp.category,
            title=opp.title,
            total_cost_cents=opp.total_cost_cents,
            profit_cents=opp.profit_cents,
            profit_pct=opp.profit_pct,
            guaranteed=int(opp.guaranteed),
            closes_at=opp.closes_at,
        )
        row.legs = [{"ticker": l.ticker, "side": l.side, "price_cents": l.price_cents} for l in opp.legs]
        rows.append(row)
    return rows


def settle_arb_simulations(session, verbose: bool = True) -> dict[str, int]:
    """
    Poll Kalshi for each open ArbSimulation and settle resolved ones.
    Returns tally dict.
    """
    from src.fetchers.kalshi import KalshiFetcher

    try:
        fetcher = KalshiFetcher()
    except Exception as exc:
        print(f"  Cannot init Kalshi fetcher: {exc}")
        return {"settled": 0, "still_open": 0, "errors": 1}

    open_sims: list[ArbSimulation] = (
        session.query(ArbSimulation).filter(ArbSimulation.status == "open").all()
    )

    tally = {"settled": 0, "still_open": 0, "errors": 0}
    now = datetime.now(timezone.utc)

    for sim in open_sims:
        legs = sim.legs
        results: list[str | None] = []
        all_finalized = True

        for leg in legs:
            try:
                mkt = fetcher.get_market_status(leg["ticker"])
            except Exception as exc:
                if verbose:
                    print(f"  Error fetching {leg['ticker']}: {exc}")
                tally["errors"] += 1
                all_finalized = False
                break

            status = mkt.get("status", "")
            result = mkt.get("result")  # "yes" | "no" | None

            if status not in ("finalized", "settled", "closed"):
                all_finalized = False
                break
            results.append(result)

        if not all_finalized:
            tally["still_open"] += 1
            continue

        # Determine outcome
        if sim.arb_type == "binary":
            # One leg yes, one leg no — guaranteed payout unless voided
            if all(r is None for r in results):
                outcome = "voided"
                pnl = 0.0
            else:
                outcome = "won"
                pnl = sim.profit_cents
        else:
            # Series: won if any leg resolved yes (matching our side)
            won_legs = sum(
                1 for leg, result in zip(legs, results)
                if result == leg["side"]
            )
            if won_legs > 0:
                outcome = "won"
                pnl = sim.profit_cents
            elif all(r is None for r in results):
                outcome = "voided"
                pnl = 0.0
            else:
                outcome = "lost"
                pnl = -sim.total_cost_cents

        sim.status = outcome
        sim.result_pnl_cents = pnl
        sim.settled_at = now
        tally["settled"] += 1

        if verbose:
            print(f"  Arb {sim.id} ({sim.arb_type} {sim.event_ticker}) → {outcome}  P&L={pnl:+.0f}¢")

    session.commit()
    return tally


def arb_report(session) -> dict[str, Any]:
    """Aggregate performance stats across all ArbSimulations."""
    all_sims: list[ArbSimulation] = session.query(ArbSimulation).all()

    open_count = sum(1 for s in all_sims if s.status == "open")
    settled = [s for s in all_sims if s.status != "open"]
    won = [s for s in settled if s.status == "won"]
    lost = [s for s in settled if s.status == "lost"]
    voided = [s for s in settled if s.status == "voided"]

    total_invested = sum(s.total_cost_cents for s in settled if s.status != "voided")
    total_pnl = sum(s.result_pnl_cents or 0.0 for s in settled)
    roi = total_pnl / total_invested if total_invested > 0 else 0.0

    by_type: dict[str, dict] = {}
    for s in settled:
        t = s.arb_type
        g = by_type.setdefault(t, {"won": 0, "lost": 0, "voided": 0, "pnl": 0.0, "invested": 0.0})
        g[s.status] = g.get(s.status, 0) + 1
        g["pnl"] += s.result_pnl_cents or 0.0
        if s.status != "voided":
            g["invested"] += s.total_cost_cents

    return {
        "total": len(all_sims),
        "open": open_count,
        "settled": len(settled),
        "won": len(won),
        "lost": len(lost),
        "voided": len(voided),
        "roi": roi,
        "total_pnl_cents": total_pnl,
        "total_invested_cents": total_invested,
        "by_type": by_type,
    }


def print_arb_report(stats: dict[str, Any]) -> None:
    sep = "=" * 62
    print(sep)
    print("  KALSHI ARB SIMULATION REPORT")
    print(sep)
    print(f"  Total opportunities:  {stats['total']}  (open={stats['open']}, settled={stats['settled']})")
    print(f"  Won / Lost / Voided:  {stats['won']} / {stats['lost']} / {stats['voided']}")
    print(f"  Total invested:       {stats['total_invested_cents']:.0f}¢")
    print(f"  Total P&L:            {stats['total_pnl_cents']:+.0f}¢")
    print(f"  ROI:                  {stats['roi']:+.2%}")
    if stats["by_type"]:
        print()
        print("  -- By Type --")
        for t, g in stats["by_type"].items():
            r = g["pnl"] / g["invested"] if g["invested"] > 0 else 0.0
            print(f"    {t:<8}  Won={g['won']} Lost={g['lost']}  ROI={r:+.2%}")
    print(sep)
