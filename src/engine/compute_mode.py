"""Computation mode — pure statistical analysis for betting recommendations."""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import Any

from src.fetchers.base import Market, Selection


@dataclass
class BetRecommendation:
    market: Market
    selection: Selection
    ev: float               # Expected Value
    kelly_fraction: float   # Kelly Criterion fraction of bankroll
    confidence: float       # 0–1 normalised confidence
    rationale: str
    metadata: dict[str, Any]


def implied_probability(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability."""
    if decimal_odds <= 1.0:
        return 1.0
    return 1.0 / decimal_odds


def expected_value(prob_estimate: float, decimal_odds: float) -> float:
    """EV = (p * odds) - 1"""
    return (prob_estimate * decimal_odds) - 1.0


def kelly_fraction(prob_estimate: float, decimal_odds: float) -> float:
    """Full Kelly: f = (p * odds - 1) / (odds - 1)"""
    if decimal_odds <= 1.0:
        return 0.0
    numerator = prob_estimate * decimal_odds - 1.0
    denominator = decimal_odds - 1.0
    return max(0.0, numerator / denominator)


def _estimate_prob(selection: Selection, all_selections: list[Selection]) -> float:
    """
    Estimate 'true' probability by removing vig from the book's implied probabilities.
    Uses basic margin removal (proportional).
    """
    raw_probs = [implied_probability(s.odds) for s in all_selections if s.odds > 1.0]
    if not raw_probs:
        return 0.0
    total_overround = sum(raw_probs)
    if total_overround <= 0:
        return 0.0
    sel_prob = implied_probability(selection.odds)
    return sel_prob / total_overround


def analyse_market(market: Market, min_ev: float = 0.03) -> list[BetRecommendation]:
    """
    Analyse a market and return positive EV recommendations.
    min_ev: minimum EV threshold (default 3%).
    """
    recommendations = []
    for sel in market.selections:
        if sel.odds <= 1.0:
            continue
        prob = _estimate_prob(sel, market.selections)
        ev = expected_value(prob, sel.odds)
        kf = kelly_fraction(prob, sel.odds)

        if ev >= min_ev:
            recommendations.append(BetRecommendation(
                market=market,
                selection=sel,
                ev=ev,
                kelly_fraction=kf,
                confidence=min(ev * 10, 1.0),  # rough confidence scaling
                rationale=(
                    f"EV={ev:.2%}, Kelly={kf:.2%}. "
                    f"Implied prob (vig-adjusted)={prob:.2%} vs raw odds prob={implied_probability(sel.odds):.2%}."
                ),
                metadata={"vig_removed_prob": prob},
            ))
    return recommendations


def detect_arbitrage(markets_by_event: dict[str, list[Market]]) -> list[dict]:
    """
    Detect arbitrage opportunities across bookmakers for the same event.
    Returns list of arb opportunities with details.
    """
    arbs = []
    for event_id, market_list in markets_by_event.items():
        if len(market_list) < 2:
            continue
        # Flatten selections grouped by outcome name
        outcome_best: dict[str, tuple[float, str]] = {}
        for market in market_list:
            for sel in market.selections:
                if sel.name not in outcome_best or sel.odds > outcome_best[sel.name][0]:
                    outcome_best[sel.name] = (sel.odds, market.source)

        if len(outcome_best) < 2:
            continue

        arb_sum = sum(1.0 / odds for odds, _ in outcome_best.values())
        if arb_sum < 1.0:
            profit_pct = (1.0 / arb_sum - 1.0) * 100
            arbs.append({
                "event_id": event_id,
                "arb_sum": arb_sum,
                "profit_pct": profit_pct,
                "legs": {name: {"odds": odds, "source": src} for name, (odds, src) in outcome_best.items()},
            })
    return arbs


def run_compute(markets: list[Market], min_ev: float = 0.03) -> list[BetRecommendation]:
    """Run computation analysis on a list of markets and return positive-EV recommendations."""
    results = []
    for market in markets:
        results.extend(analyse_market(market, min_ev=min_ev))
    results.sort(key=lambda r: r.ev, reverse=True)
    return results
