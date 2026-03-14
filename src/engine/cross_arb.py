"""Cross-platform arbitrage scanner — matches Kalshi and Polymarket markets,
then identifies price discrepancies that can be locked in risk-free.

WARNING — settlement risk:
  Cross-platform arb is only safe when BOTH platforms resolve mechanically
  to the same underlying fact (e.g. "Did X happen? Yes/No").  Kalshi and
  Polymarket use different resolution oracles and CAN settle differently on
  contested or ambiguous events.  Always verify settlement language matches
  before entering a live cross-arb position.  Simulated entry is always safe;
  live Polymarket order placement is out of scope (requires USDC on Polygon).
"""
from __future__ import annotations

import difflib
import re
import string
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.fetchers.base import Market


# ---------------------------------------------------------------------------
# Filler words stripped before text comparison
# ---------------------------------------------------------------------------
_FILLER = frozenset(
    ["will", "the", "a", "an", "by", "on", "in", "of", "to", "be",
     "is", "are", "was", "were", "has", "have", "had", "do", "does",
     "did", "at", "for", "or", "and", "its"]
)
_PUNCT_RE = re.compile(r"[" + re.escape(string.punctuation) + r"]")


def _normalise(text: str) -> str:
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    tokens = [t for t in text.split() if t not in _FILLER]
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MatchedPair:
    kalshi_market: "Market"
    poly_market: "Market"
    match_score: float   # 0.0–1.0
    match_reason: str


@dataclass
class CrossArbOpportunity:
    """
    WARNING: Only enter this position when settlement_risk is 'low' or 'medium'.
    High-risk matches should never be entered, even in simulation.

    direction: 'kalshi_yes' means buy YES on Kalshi + NO on Polymarket.
               'poly_yes'   means buy YES on Polymarket + NO on Kalshi.
    """
    direction: str                # "kalshi_yes" | "poly_yes"
    kalshi_market: "Market"
    poly_market: "Market"
    kalshi_leg: dict              # {source, side, price_cents}
    poly_leg: dict                # {source, side, price_cents}
    total_cost_cents: float
    profit_cents: float
    profit_pct: float
    closes_at: datetime | None
    match_score: float
    settlement_risk: str          # "low" | "medium" | "high"


# ---------------------------------------------------------------------------
# Market matching
# ---------------------------------------------------------------------------

def _text_score(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalise(a), _normalise(b)).ratio()


def _expiry_score(a: "Market", b: "Market") -> float:
    if a.starts_at is None or b.starts_at is None:
        return 0.5  # unknown → neutral
    diff_hours = abs((a.starts_at - b.starts_at).total_seconds()) / 3600
    return max(0.0, 1.0 - diff_hours / 48)


def _category_score(a: "Market", b: "Market") -> float:
    ca, cb = (a.category or "").lower(), (b.category or "").lower()
    if ca == cb:
        return 1.0
    # Related pairs
    related = [
        {"crypto"},
        {"economics", "financials"},
        {"politics", "world"},
    ]
    for group in related:
        if ca in group and cb in group:
            return 0.5
    return 0.0


def match_markets(
    kalshi_markets: list["Market"],
    poly_markets: list["Market"],
    min_score: float = 0.85,
) -> list[MatchedPair]:
    """
    One-to-one match each Kalshi market to the highest-scoring Polymarket market.

    Composite score (weighted):
      - Question text:      0.60  (difflib ratio on normalised strings)
      - Expiry proximity:   0.30  (1.0 = same hour, 0.0 = 48h apart)
      - Category match:     0.10  (1.0 exact, 0.5 related, 0.0 different)
    """
    pairs: list[MatchedPair] = []
    used_poly: set[str] = set()

    for km in kalshi_markets:
        best_score = -1.0
        best_pm = None

        for pm in poly_markets:
            if pm.id in used_poly:
                continue
            text_s = _text_score(km.event_name, pm.event_name)
            expiry_s = _expiry_score(km, pm)
            cat_s = _category_score(km, pm)
            score = 0.60 * text_s + 0.30 * expiry_s + 0.10 * cat_s
            if score > best_score:
                best_score = score
                best_pm = pm

        if best_pm is not None and best_score >= min_score:
            if best_score >= 0.90:
                risk = "low"
            elif best_score >= 0.80:
                risk = "medium"
            else:
                risk = "high"

            pairs.append(MatchedPair(
                kalshi_market=km,
                poly_market=best_pm,
                match_score=round(best_score, 4),
                match_reason=(
                    f"text={_text_score(km.event_name, best_pm.event_name):.2f} "
                    f"expiry={_expiry_score(km, best_pm):.2f} "
                    f"cat={_category_score(km, best_pm):.1f}"
                ),
            ))
            used_poly.add(best_pm.id)

    return pairs


# ---------------------------------------------------------------------------
# Cross-arb scanning
# ---------------------------------------------------------------------------

def _get_price(market: "Market", side: str) -> int | None:
    """
    Return best ask for YES or NO side from a market's metadata (integer cents).
    Falls back to computing from selection odds if metadata not available.
    """
    meta = market.metadata or {}
    if side == "yes":
        price = meta.get("yes_ask")
        if price is not None:
            return int(price)
        sel = next((s for s in market.selections if s.name.lower() == "yes"), None)
        if sel and sel.odds > 0:
            return round(100.0 / sel.odds)
    else:
        price = meta.get("no_ask")
        if price is not None:
            return int(price)
        sel = next((s for s in market.selections if s.name.lower() == "no"), None)
        if sel and sel.odds > 0:
            return round(100.0 / sel.odds)
    return None


def scan_cross_arb(
    pairs: list[MatchedPair],
    min_profit_cents: float = 2.0,
) -> list[CrossArbOpportunity]:
    """
    For each matched pair check both arb directions:
      1. kalshi_yes:  buy YES on Kalshi + NO on Polymarket (cost < 100¢)
      2. poly_yes:    buy YES on Polymarket + NO on Kalshi (cost < 100¢)

    WARNING — settlement risk: see module docstring. Entries with
    settlement_risk == 'high' are always skipped regardless of profit.
    """
    opps: list[CrossArbOpportunity] = []

    for pair in pairs:
        if pair.match_score < 0.80:
            # Never enter high-risk matches
            continue

        settlement_risk = "low" if pair.match_score >= 0.90 else "medium"

        km = pair.kalshi_market
        pm = pair.poly_market

        k_yes = _get_price(km, "yes")
        k_no = _get_price(km, "no")
        p_yes = _get_price(pm, "yes")
        p_no = _get_price(pm, "no")

        closes_at = km.starts_at  # use Kalshi expiry as canonical

        for direction, buy_yes_market, buy_yes_price, buy_no_market, buy_no_price in [
            ("kalshi_yes", km, k_yes, pm, p_no),
            ("poly_yes",   pm, p_yes, km, k_no),
        ]:
            if buy_yes_price is None or buy_no_price is None:
                continue
            total_cost = buy_yes_price + buy_no_price
            profit = 100.0 - total_cost
            if profit < min_profit_cents:
                continue

            profit_pct = profit / total_cost if total_cost > 0 else 0.0

            if direction == "kalshi_yes":
                kalshi_leg = {"source": "kalshi", "side": "yes", "price_cents": buy_yes_price}
                poly_leg   = {"source": "polymarket", "side": "no", "price_cents": buy_no_price}
            else:
                kalshi_leg = {"source": "kalshi", "side": "no", "price_cents": buy_no_price}
                poly_leg   = {"source": "polymarket", "side": "yes", "price_cents": buy_yes_price}

            opps.append(CrossArbOpportunity(
                direction=direction,
                kalshi_market=km,
                poly_market=pm,
                kalshi_leg=kalshi_leg,
                poly_leg=poly_leg,
                total_cost_cents=total_cost,
                profit_cents=profit,
                profit_pct=round(profit_pct, 4),
                closes_at=closes_at,
                match_score=pair.match_score,
                settlement_risk=settlement_risk,
            ))

    opps.sort(key=lambda o: o.profit_pct, reverse=True)
    return opps
