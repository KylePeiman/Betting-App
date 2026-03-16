"""
Claude-powered advisor for 15-minute crypto direction markets on Kalshi.

For each "Will BTC/ETH/SOL/XRP price go up in the next 15 minutes?" market,
fetches recent Binance candles and asks Claude whether to bet YES, NO, or PASS.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.fetchers.base import Market


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def advise_15m_market(
    market: Market,
    anthropic_api_key: str,
    model: str = "claude-sonnet-4-6",
    candle_count: int = 30,
) -> dict[str, Any]:
    """
    Ask Claude whether to bet YES, NO, or PASS on a 15-minute direction market.

    Returns a dict:
      {
        "action":     "yes" | "no" | "pass",
        "confidence": float  (0.0 – 1.0),
        "rationale":  str,
        "symbol":     str,   # e.g. "BTCUSDT"
        "price":      float, # current price at time of call
      }
    """
    from src.fetchers.crypto_prices import (
        series_ticker_to_kraken,
        get_recent_candles,
        get_current_price,
    )

    series_ticker = market.metadata.get("series_ticker", "")
    kraken_pair = series_ticker_to_kraken(series_ticker)
    if not kraken_pair:
        return _pass(f"No Kraken mapping for series_ticker={series_ticker!r}")

    # Fetch price data
    try:
        candles = get_recent_candles(kraken_pair, limit=candle_count)
        current_price = get_current_price(kraken_pair)
    except Exception as exc:
        return _pass(f"Price fetch failed: {exc}")

    # Extract market odds
    yes_sel = next((s for s in market.selections if s.name == "Yes"), None)
    no_sel = next((s for s in market.selections if s.name == "No"), None)
    if not yes_sel or not no_sel:
        return _pass("Missing YES/NO selections")

    yes_ask = yes_sel.metadata.get("yes_ask", 0)
    no_ask = no_sel.metadata.get("no_ask", 0)
    yes_implied = yes_sel.metadata.get("implied_prob", 0.5)

    # Minutes until close
    minutes_left: float | None = None
    if market.starts_at:
        delta = (market.starts_at - datetime.now(timezone.utc)).total_seconds()
        minutes_left = max(0.0, delta / 60)

    # Build candle summary (last 20 most readable)
    candle_lines = "\n".join(
        f"  {c['time']}  O={c['open']:.4f}  H={c['high']:.4f}"
        f"  L={c['low']:.4f}  C={c['close']:.4f}  Vol={c['volume']:.0f}"
        for c in candles[-20:]
    )

    # The reference price is the OPEN of the candle that started the 15-min window.
    # Kalshi resolves "UP" if the close price > the price at market open time.
    # We estimate the reference as the open of the candle 15 minutes ago.
    closes = [c["close"] for c in candles]
    opens  = [c["open"]  for c in candles]
    ref_price = opens[-15] if len(opens) >= 15 else opens[0]
    price_5m_ago = closes[-5] if len(closes) >= 5 else closes[0]
    change_vs_ref  = (current_price - ref_price)  / ref_price  * 100
    change_5m      = (current_price - price_5m_ago) / price_5m_ago * 100

    currently_winning = "YES (price is UP vs reference)" if current_price > ref_price else "NO (price is DOWN vs reference)"

    crypto_name = kraken_pair.replace("USD", "").replace("XBT", "BTC")
    time_str = f"{minutes_left:.1f} minutes" if minutes_left is not None else "unknown"

    prompt = f"""You are analyzing a Kalshi prediction market to decide whether to paper-trade.

MARKET
  Question  : {market.event_name}
  Ticker    : {market.id}
  Closes in : {time_str}

HOW IT RESOLVES
  YES wins if the final price is ABOVE the reference price (price at market open, ~15 min ago).
  NO  wins if the final price is BELOW the reference price.

KEY PRICES
  Reference price (market open, ~15 min ago) : ${ref_price:.4f}
  Current price                              : ${current_price:.4f}
  Change vs reference                        : {change_vs_ref:+.3f}%
  Change last 5 min                          : {change_5m:+.3f}%
  Currently winning side                     : {currently_winning}

MARKET ODDS
  YES ask : {yes_ask}c  (market implies {yes_implied:.0%} probability of closing UP)
  NO ask  : {no_ask}c  (market implies {1 - yes_implied:.0%} probability of closing DOWN)

RECENT 1-MINUTE CANDLES (newest at bottom)
{candle_lines}

TASK
You have {time_str} until close. The price is currently {currently_winning}.

STEP 1 — Estimate the TRUE probability that the final price will be ABOVE the reference at close.
  Give this as p_yes (0.0 to 1.0). Be calibrated — account for last-minute volatility.
  IMPORTANT: Even if price is below reference right now, there is ALWAYS some chance of recovery.
  Typical guideline: if gap is <0.3% and time >20s remaining, p_yes should be at least 5-20%.

STEP 2 — Compare to market implied probability.
  Market says p_yes = {yes_implied:.1%}.
  Your edge = |your p_yes - market p_yes|.
  Only bet if edge ≥ 15% (you see a significant mispricing).

STEP 3 — Decide.
  - If your p_yes > market p_yes + 15% → bet YES
  - If your p_yes < market p_yes - 15% → bet NO
  - Otherwise → PASS

MINIMUM PRICE GAP RULE:
  If |change vs reference| < 0.50% AND time remaining < 2 minutes → PASS.
  Small gaps can easily reverse in seconds. Only bet clear situations.

Respond with valid JSON only, no markdown:
{{"action": "yes or no or pass", "p_yes": 0.0, "rationale": "one sentence: your p_yes estimate, market implied, and why there is or is not edge"}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_api_key)
        response = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if model wraps anyway
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        action = str(result.get("action", "pass")).lower().strip()
        if action not in ("yes", "no", "pass"):
            action = "pass"

        # p_yes is Claude's calibrated probability estimate (0-1)
        p_yes = float(result.get("p_yes", result.get("confidence", 0.5)))
        p_yes = max(0.0, min(1.0, p_yes))

        if action in ("yes", "no"):
            # Hard price-gap filter: tiny gaps reverse too easily
            abs_gap_pct = abs(change_vs_ref)
            if abs_gap_pct < 0.50 and minutes_left is not None and minutes_left < 2.0:
                return _pass(
                    f"Gap too small to bet: {change_vs_ref:+.3f}% vs reference"
                    f" with {minutes_left:.1f}min left (need ≥0.50% gap)"
                )

            # Edge filter: Claude's p_yes must differ from market by ≥15%
            edge = abs(p_yes - yes_implied)
            if edge < 0.15:
                return _pass(
                    f"Insufficient edge: p_yes={p_yes:.0%} market={yes_implied:.0%}"
                    f" gap={edge:.0%} (need ≥15% gap)"
                )

            # Use p_yes (or 1-p_yes) as the confidence for sizing
            confidence = p_yes if action == "yes" else (1.0 - p_yes)
        else:
            confidence = 0.0

        return {
            "action": action,
            "confidence": confidence,
            "rationale": str(result.get("rationale", "")),
            "symbol": kraken_pair,
            "price": current_price,
        }
    except Exception as exc:
        return _pass(f"Claude call failed: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pass(reason: str) -> dict[str, Any]:
    return {"action": "pass", "confidence": 0.0, "rationale": reason, "symbol": "", "price": 0.0}


def is_15m_market(market: Market) -> bool:
    """Return True if this market is a 15-minute direction market."""
    series = market.metadata.get("series_ticker", "")
    return "15M" in series
