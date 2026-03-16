"""
Fetches recent OHLCV price data from the Kraken public API (no key needed).
Used by the agent advisor to give Claude recent price context.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

# Maps Kalshi series_ticker prefix → Kraken pair name
_SERIES_TO_KRAKEN: dict[str, str] = {
    "KXBTC15M": "XBTUSD",
    "KXETH15M": "ETHUSD",
    "KXSOL15M": "SOLUSD",
    "KXXRP15M": "XRPUSD",
    "KXDOGE15M": "DOGEUSD",
}

_KRAKEN_BASE = "https://api.kraken.com/0/public"


def series_ticker_to_kraken(series_ticker: str) -> str | None:
    """Return the Kraken pair for a Kalshi series ticker, or None if unknown."""
    return _SERIES_TO_KRAKEN.get(series_ticker)


def get_recent_candles(kraken_pair: str, limit: int = 30) -> list[dict[str, Any]]:
    """
    Fetch the last `limit` 1-minute OHLCV candles for `kraken_pair` from Kraken.

    Kraken returns up to 720 candles; we take the last `limit`.
    Returns a list of dicts with keys: time, open, high, low, close, volume.
    """
    with httpx.Client(timeout=10) as client:
        resp = client.get(
            f"{_KRAKEN_BASE}/OHLC",
            params={"pair": kraken_pair, "interval": 1},
        )
        resp.raise_for_status()
        data = resp.json()

    if data.get("error"):
        raise ValueError(f"Kraken error: {data['error']}")

    # result key is the pair name (may differ slightly from requested)
    result = data.get("result", {})
    raw = next(v for k, v in result.items() if k != "last")

    candles = []
    for row in raw[-limit:]:
        # [time, open, high, low, close, vwap, volume, count]
        candles.append({
            "time": datetime.fromtimestamp(int(row[0]), tz=timezone.utc).strftime("%H:%M"),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
        })
    return candles


def get_current_price(kraken_pair: str) -> float:
    """Return the latest trade price for `kraken_pair` from Kraken."""
    with httpx.Client(timeout=10) as client:
        resp = client.get(f"{_KRAKEN_BASE}/Ticker", params={"pair": kraken_pair})
        resp.raise_for_status()
        data = resp.json()

    if data.get("error"):
        raise ValueError(f"Kraken error: {data['error']}")

    result = data.get("result", {})
    ticker_data = next(iter(result.values()))
    # 'c' = [last trade price, lot volume]
    return float(ticker_data["c"][0])
